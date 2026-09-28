"""
发送日志：机器人今天说了什么，都记在这。

两个用途：
    1) 【去重】—— 战报这类群发通报，同样的话一天只发一次。
       实际见过的重复：「恭喜东林支局新增宽带破"1"、新增中高套破"1"！」
       连着发两遍，群里看着像机器人卡了。
    2) 【回溯】—— 出问题时能查「机器人几点发了什么」，
       比翻聊天记录快，也比翻日志文件准。

⚠️ 只对【群发通报】去重，不对【点对点的点赞】去重：
    两个人各报一单 139，模型很可能生成一模一样的回复语。
    按内容去重的话第二个人就收不到回复，他会以为机器人漏了自己。
    所以点赞照发，只是记进日志。

跨天自动清空 —— 文件里存着日期，日期一变就当作全新的一天。
「破1」这种话术天然是按天累计的，昨天发过的今天当然要重发。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import date, datetime
from pathlib import Path

from .paths import DATA

log = logging.getLogger("botcore.sendlog")

# 归一化：去掉空白和 emoji 再比，避免「破1！👍👍」和「破1 ！👍👍👍」
# 被当成两条不同的通报。数字和汉字保留 —— 那才是有信息量的部分。
_NOISE = re.compile(r"[\s​]+|[\U0001F300-\U0001FAFF☀-➿️]")


def normalize(text: str) -> str:
    return _NOISE.sub("", text or "")


class SendLog:
    """按天记录发出去的内容。线程安全。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else DATA / "sendlog.json"
        self._lock = threading.Lock()
        self._day: str = ""
        self._items: list[dict] = []
        self._keys: set[str] = set()
        self._load()

    # ---------- 存取 ----------

    def _load(self) -> None:
        if not self.path.exists():
            self._day = date.today().isoformat()
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("发送日志读不了，当作新的一天：%s", exc)
            self._day = date.today().isoformat()
            return
        if not isinstance(raw, dict):
            log.warning("发送日志不是对象（%s），当作新的一天", type(raw).__name__)
            self._day = date.today().isoformat()
            return
        self._day = str(raw.get("date") or "")
        # 条目不是字典的跳过，别让一条坏记录拖垮整个启动
        self._items = [i for i in (raw.get("items") or []) if isinstance(i, dict)]
        self._keys = {i["key"] for i in self._items if i.get("key")}

    def _save(self) -> None:
        try:
            self.path.write_text(json.dumps(
                {"date": self._day, "items": self._items},
                ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("发送日志写不进去：%s", exc)

    def _roll(self) -> None:
        """跨天就清空。调用方已持锁。"""
        today = date.today().isoformat()
        if self._day != today:
            if self._items:
                log.info("发送日志跨天，清空昨天的 %d 条", len(self._items))
            self._day = today
            self._items = []
            self._keys = set()

    # ---------- 对外 ----------

    def is_duplicate(self, text: str, kind: str) -> bool:
        """今天有没有发过一模一样的（同类型）。"""
        with self._lock:
            self._roll()
            return f"{kind}|{normalize(text)}" in self._keys

    def record(self, text: str, kind: str, chat_id: str = "",
               to: str = "", dedupe: bool = False) -> None:
        """
        记一条。dedupe=True 的才会进去重索引 ——
        点赞不进，否则同样的话术第二个人就收不到了。
        """
        with self._lock:
            self._roll()
            key = f"{kind}|{normalize(text)}" if dedupe else ""
            self._items.append({
                "at": datetime.now().strftime("%H:%M:%S"),
                "kind": kind,
                "to": to,
                "chat": chat_id[-6:] if chat_id else "",
                "text": text,
                "key": key,
            })
            if key:
                self._keys.add(key)
            self._save()

    def today(self) -> list[dict]:
        with self._lock:
            self._roll()
            return list(self._items)

    def summary(self) -> str:
        items = self.today()
        if not items:
            return "今天还没发过消息"
        kinds: dict[str, int] = {}
        for i in items:
            kinds[i.get("kind", "?")] = kinds.get(i.get("kind", "?"), 0) + 1
        parts = "、".join(f"{k} {n} 条" for k, n in kinds.items())
        return f"今天共发 {len(items)} 条：{parts}"
