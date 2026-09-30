"""
晒单容错解析：固定格式的正则抓不到字段时，让模型把字段抽出来。

什么时候用：
    群里的「业务晒单时刻」是人手打的，字段名会写歪 ——「套餐:139」「揽收：郭静」
    「业务类型 新增」（没冒号）、字段顺序调换、两个字段挤一行 …… 严格解析要么
    抓不到、要么抓错。以前这些单会静默丢掉，清单里就少一单。

怎么用：
    pipeline 先跑严格解析（campaign.parse_order）；失败且文字「像晒单」
    （命中 ≥ N 个模板字段名）才调这里。抽出来的字段走和严格解析【同一个】
    build_order —— 换算规则只有一份，模型只负责「认字段」。
    记进清单时 source 标「文字晒单·模型解析」并 needs_review，Excel 里标黄，
    让人扫一眼。

边界：
    · 发给模型之前先 redact()：业务号码、手机号一律抹掉。号码哈希由 build_order
      从原文本地算，不出网。
    · 模型只许抽【文字里明确写了的】，没写就留空；缺业务类型或套餐就放弃。
    · 一条消息里报了多单的，模型标 multiple=true，这里放弃 —— 拆单容易错，
      宁可让人补。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass

import requests

from .campaign import normalize_kind
from .distill import redact
from .llm import as_bool

log = logging.getLogger("botcore.extract")

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def _yes_no(v) -> str:
    """模型给的「是否升级」：是 / 否 / 空串（没这一栏）。"""
    if v is True:
        return "是"
    s = str(v or "").strip()
    if not s or s.lower() in ("none", "null"):
        return ""
    return "是" if s == "是" or s.lower() in ("true", "1", "yes") else "否"


@dataclass
class ExtractSettings:
    enabled: bool = False
    # 文字至少命中几个模板字段名才值得调模型（支局/业务类型/套餐/积分/揽收人/是否加载…）
    min_field_hits: int = 2
    timeout: float = 12.0
    temperature: float = 0.0
    max_tokens: int = 300
    max_calls_per_minute: int = 30
    # 留空复用 agent 段的服务商和模型
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""


SYSTEM_PROMPT = """你从中国电信营销群的「晒单」文字里抽取字段。这些文字本来有固定模板，但发的人常把字段名写歪、漏冒号、调换顺序。

只输出一个 JSON 对象，不要代码块、不要解释：
{"is_order": true或false, "multiple": true或false, "kind": "", "fee": "", "branch": "", "collector": "", "points": "", "cards": "", "ai": false, "box": false, "scrn": false, "fttr": false, "up_giga": "", "up_2000": ""}

字段说明（【只抽文字里明确写了的，没写就留空串或 false，绝不推测】）：
- is_order：这段文字是不是在报告【一笔已办成】的业务。问问题、转发模板、催晒单、闲聊都是 false。
- multiple：一段文字里报了两笔及以上业务时 true。
- kind：业务类型，只能是 新增 / 存量 / 单C 三者之一。新装、新办、携转 → 新增；续约、续签、改档、升档、平迁、加装 → 存量；号卡、山城卡、单C、橙分期 → 单C。文字里完全看不出就留空。
- fee：办理的套餐或资费，照原文抄，如「139+10」「149改169」「19山城卡」「59返30橙分期」「138单宽」。看不到就留空。
- branch：支局 / 战队 / 战区名，如「新盛支局」。
- collector：揽收人姓名（「揽收」「揽收人」「经办」「办理人」后面的名字）。
- points：价值积分，只填数字。文字里没写就留空 —— 不要按套餐算。
- cards：号卡数量，只填数字。
- ai / box / scrn / fttr：是否加载 AI视联网 / 云盒 / 智屏 / FTTR。写了「是」「1」「已加载」「有」才 true；留空、无、否 都是 false。
- up_giga / up_2000：模板里「是否升级千兆/500M」「是否升级2000M」两栏。写了「是」填 "是"；写了否或空着填 "否"；文字里根本没有这一栏填 ""。

注意：「[号码]」「[手机号]」是脱敏占位符，忽略即可。"""


class FieldExtractor:
    def __init__(self, settings: ExtractSettings, llm_settings, api_key: str) -> None:
        self.s = settings
        self.base_url = (settings.base_url or llm_settings.base_url).rstrip("/")
        self.model = settings.model or llm_settings.model
        self.api_key = api_key
        self._session = requests.Session()
        self._lock = threading.Lock()
        self._window_start = 0.0
        self._calls = 0

    def take_budget(self) -> bool:
        limit = self.s.max_calls_per_minute
        if limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            if now - self._window_start >= 60:
                self._window_start, self._calls = now, 0
            if self._calls >= limit:
                return False
            self._calls += 1
            return True

    def extract(self, text: str) -> dict | None:
        """
        返回 build_order 能吃的字段字典；判定不是单、多单、缺关键字段、出错都返回 None
        （原因打在日志里）。
        """
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": redact(text)},
            ],
            "temperature": self.s.temperature,
            "max_tokens": self.s.max_tokens,
            "stream": False,
        }
        try:
            resp = self._session.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json=payload, timeout=self.s.timeout)
        except requests.RequestException as exc:
            log.warning("容错解析调用模型失败（网络）：%s", exc)
            return None
        if resp.status_code != 200:
            log.warning("容错解析调用模型失败 HTTP %s：%s", resp.status_code, resp.text[:200])
            return None
        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            log.warning("容错解析模型响应结构异常：%s", exc)
            return None
        return self._parse(content)

    @staticmethod
    def _parse(content: str) -> dict | None:
        raw = _FENCE.sub("", content or "").strip()
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            log.warning("容错解析：模型没返回 JSON：%s", raw[:120])
            return None
        try:
            data = json.loads(raw[start:end + 1])
        except json.JSONDecodeError:
            log.warning("容错解析：JSON 解析失败：%s", raw[:120])
            return None
        if not isinstance(data, dict):
            return None
        if not as_bool(data.get("is_order")):
            log.info("容错解析：模型判定不是晒单")
            return None
        if as_bool(data.get("multiple")):
            log.info("容错解析：一条消息里报了多单，不自动拆，留给人补")
            return None
        kind = normalize_kind(str(data.get("kind") or ""))
        fee = str(data.get("fee") or "").strip()
        if kind not in ("新增", "存量", "单C") or not fee:
            log.info("容错解析：缺业务类型或套餐（kind=%r fee=%r），放弃", kind, fee)
            return None

        def digits(v) -> str:
            m = re.search(r"\d+", str(v or ""))
            return m.group(0) if m else ""

        return {
            "kind": kind, "fee": fee,
            "branch": str(data.get("branch") or "").strip(),
            "collector": str(data.get("collector") or "").strip(),
            "points": digits(data.get("points")),
            "cards": digits(data.get("cards")),
            "ai": as_bool(data.get("ai")), "box": as_bool(data.get("box")),
            "scrn": as_bool(data.get("scrn")), "fttr": as_bool(data.get("fttr")),
            "up_giga": _yes_no(data.get("up_giga")), "up_2000": _yes_no(data.get("up_2000")),
        }
