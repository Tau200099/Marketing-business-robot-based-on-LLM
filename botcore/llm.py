"""
第二层：模型判断。

设计要点：
1. 只有通过本地粗筛的消息才会走到这里 —— 群里大部分闲聊在上一层就被丢掉了，
   不产生任何 API 调用。这是把成本和延迟压住的关键。
2. 用 OpenAI 兼容接口，国内几家（DeepSeek / 豆包 / 通义 / 智谱 / Kimi）都支持，
   换服务商只改 base_url 和 model，代码不动。
3. 模型出错、超时、返回格式不对时一律返回 None，由上层决定是跳过还是兜底回复。
   默认跳过 —— 漏回一条业绩，好过给报障消息回一句「点赞」。
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field

import requests

from .matcher import Rule

log = logging.getLogger("feishu_bot.llm")

# 模型有时会把 JSON 包在 ```json ``` 里，先剥掉
_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)

SYSTEM_PROMPT = """你是一个销售业绩群的助手。群成员会在群里报单（报告自己刚完成的销售），你要判断每条消息是不是在报单，并生成一句祝贺回复。

{context}

判断标准：
- 是报单：成员说自己办理了、新增了、装了、开通了某个套餐或产品
- 不是报单：报障报修、咨询提问、售后退订、进度汇报、统计数据、闲聊

严格只输出一个 JSON 对象，不要代码块标记，不要任何解释文字：
{{"is_report": true 或 false, "is_new": true 或 false, "package": "套餐档位或产品名", "count": 数量整数, "broadband_count": 整数, "high_count": 整数, "reply": "回复语"}}

字段说明：
- is_report：这条消息是不是在报单
- is_new：是不是「新增」（消息里明确提到新增/新办/新装才算 true）
- package：识别到的主套餐档位或产品名，识别不出填空字符串
- count：单量，看不出来填 1
- broadband_count：本单算几个【新增宽带】。主宽算 1 个，每个副宽再各算 1 个。
    「新增199」        -> 1
    「新增199+副宽」    -> 2      主宽 1 + 副宽 1
    「新增199+2个副宽」  -> 3
    「新增139+10」     -> 1      「+10」是附加项不是副宽
    「新增副宽」        -> 1
  不是新增的（续约/续签/升档/纯预约）一律填 0。
- high_count：本单算几个【新增中高套】。哪些档位算中高套见上面的业务背景。
  副宽没有独立套餐，不单独计中高套。
    「新增199+副宽」    -> 1      两个宽带，但只有一个中高套
    「新增副宽」        -> 0      没有档位
  不是新增的一律填 0。
- reply：给报单人的祝贺语。自然地提到具体套餐或产品，每次措辞不要雷同。
  is_report 为 false 时，reply 必须填空字符串。
  语气按业务类型区分，见下面【回复话术】。

{reply_style}"""

# 按 is_new 分档的话术要求，拼进系统提示词
_STYLE_BLOCK = """【回复话术】
新增单（is_new = true）：
{new}

续约 / 续签 / 升档单（is_new = false）：
{renew}"""


def as_bool(v) -> bool:
    """
    模型偶尔把布尔写成字符串："false" / "否" / "no"。bool("false") 是 True，要按字面判。
    容错解析（extract.py）的 是否加载 字段也用这个。
    """
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "y", "是", "有", "已加载", "加载")
    return bool(v)


def _as_int(v) -> int:
    """模型偶尔会把数字写成字符串或小数，宽松地转一下。转不了当 0。"""
    try:
        return max(0, int(float(v)))
    except (TypeError, ValueError):
        return 0


@dataclass
class LLMSettings:
    enabled: bool = False
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-chat"
    api_key_env: str = "LLM_API_KEY"
    timeout: float = 8.0
    temperature: float = 0.8
    max_tokens: int = 300

    # 额外请求头。标准 OpenAI 兼容接口用不上，留空即可。
    # 有些服务商在 Authorization 之外还要自己的头（例如电信星辰的
    # /aipaas 那条路要 X-APP-ID 和 Order-Num）。
    # 值里可以写 ${环境变量名}，运行时从 .env 取 —— 别把凭证写进 config.yaml。
    extra_headers: dict = field(default_factory=dict)

    # token 上限的字段名。标准是 max_tokens，个别服务商叫 max_completion_tokens。
    # 填错的表现是模型不按长度截断，或者直接报参数错误。
    max_tokens_field: str = "max_tokens"

    context: str = ""
    # 按业务类型分档的回复话术。新增单含金量最高，语气要更足；
    # 续约/升档也算业绩但分量不同，语气要克制一些。
    # 键：new（is_new=true）/ renew（is_new=false）
    reply_style: dict = field(default_factory=dict)
    # 第一层粗筛：出现任意一个词才会调模型。宽一点没关系，宁可多筛进来让模型判，
    # 也别在这一层做精细过滤 —— 精细判断正是模型的活。
    prefilter: list[str] = field(default_factory=list)
    # 模型不可用时：skip = 不回复（默认，安全）；fallback = 回复 fallback_reply
    on_error: str = "skip"
    fallback_reply: str = ""
    at_sender: bool = True
    # 每分钟最多调用多少次。防止群里突然刷屏把 API 费用打上去，超出后本分钟内
    # 剩余消息直接跳过（会打日志）。设 0 表示不限。
    max_calls_per_minute: int = 60


class LLMJudge:
    def __init__(self, settings: LLMSettings, api_key: str) -> None:
        self.s = settings
        self.api_key = api_key
        self._prefilter: Rule | None = None
        if settings.prefilter:
            self._prefilter = Rule(
                name="__prefilter__", mode="any",
                keywords=list(settings.prefilter), reply="",
            ).compile()
        # 复用连接，省掉每次 TLS 握手
        self._session = requests.Session()
        self._lock = threading.Lock()
        self._window_start = 0.0
        self._calls_in_window = 0

    def prefilter(self, text: str) -> bool:
        """本地粗筛。没配 prefilter 就一律放行。"""
        if self._prefilter is None:
            return True
        return self._prefilter.match(text)

    def take_budget(self) -> bool:
        """每分钟调用次数上限。超出返回 False，让上层跳过这条消息。"""
        limit = self.s.max_calls_per_minute
        if limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            if now - self._window_start >= 60:
                self._window_start = now
                self._calls_in_window = 0
            if self._calls_in_window >= limit:
                return False
            self._calls_in_window += 1
            return True

    def judge(self, text: str) -> dict | None:
        """
        让模型判断并生成回复。
        返回 None 表示「判断失败」，返回 dict 表示判断成功（可能 is_report=False）。
        """
        style = _STYLE_BLOCK.format(
            new=((self.s.reply_style or {}).get("new") or "简短热情，带 1~2 个 emoji。").strip(),
            renew=((self.s.reply_style or {}).get("renew") or "简短认可，带 1 个 emoji。").strip(),
        )
        # yaml 里 context: / reply_style: 留空时值是 None，不判空每条消息都 AttributeError
        system = SYSTEM_PROMPT.format(context=(self.s.context or "").strip(), reply_style=style)
        payload = {
            "model": self.s.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            "temperature": self.s.temperature,
            self.s.max_tokens_field: self.s.max_tokens,
            "stream": False,
        }

        try:
            resp = self._session.post(
                f"{self.s.base_url.rstrip('/')}/chat/completions",
                headers=self._headers(),
                json=payload,
                timeout=self.s.timeout,
            )
        except requests.RequestException as exc:
            log.warning("调用模型失败（网络）：%s", exc)
            return None

        if resp.status_code != 200:
            log.warning("调用模型失败 HTTP %s：%s", resp.status_code, resp.text[:200])
            return None

        try:
            content = resp.json()["choices"][0]["message"]["content"]
        except (KeyError, IndexError, ValueError) as exc:
            log.warning("模型响应结构异常：%s", exc)
            return None

        return self._parse(content)

    def _headers(self) -> dict[str, str]:
        """
        标准头 + config.yaml 里配的额外头。
        额外头的值支持 ${环境变量名}，从环境变量取，凭证不落配置文件。
        """
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        for key, raw in (self.s.extra_headers or {}).items():
            value = str(raw)
            m = _ENV_REF.fullmatch(value.strip())
            if m:
                value = os.getenv(m.group(1), "")
                if not value:
                    log.warning("请求头 %s 引用的环境变量 %s 是空的", key, m.group(1))
            headers[str(key)] = value
        return headers

    @staticmethod
    def _parse(content: str) -> dict | None:
        raw = _FENCE.sub("", content or "").strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            # 兜底：从文本里抠出第一个 {...}
            start, end = raw.find("{"), raw.rfind("}")
            if start < 0 or end <= start:
                log.warning("模型没有返回 JSON：%s", raw[:200])
                return None
            try:
                data = json.loads(raw[start:end + 1])
            except json.JSONDecodeError:
                log.warning("模型返回的 JSON 解析失败：%s", raw[:200])
                return None

        if not isinstance(data, dict) or "is_report" not in data:
            log.warning("模型返回缺少 is_report 字段：%s", str(data)[:200])
            return None

        return {
            "is_report": as_bool(data.get("is_report")),
            "is_new": as_bool(data.get("is_new")),
            "package": str(data.get("package") or ""),
            "count": data.get("count") or 1,
            "broadband_count": _as_int(data.get("broadband_count")),
            "high_count": _as_int(data.get("high_count")),
            "reply": str(data.get("reply") or "").strip(),
        }
