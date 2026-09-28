"""
支局日计数 + 战报播报。

每来一单新增，按（当天日期 × 支局）累计两个数：
    broadband  新增宽带数 —— 主宽算 1 个，每个副宽再各算 1 个
    high       新增中高套数 —— 档位 139 及以上的算，副宽不单独计

这两个数由模型从工作内容里数出来（见 llm.py 的 broadband_count / high_count），
不在这里做文本解析 —— 「+副宽」「带个副宽」「2个副宽」写法太多，正则数不准。
然后播报「恭喜xx支局新增宽带破"N"、新增中高套破"M"！」

为什么要落盘：进程重启、断网重连、电脑重启都不能丢数。
播报一个错的数字，比不播报更难看 —— 群里的人会当场发现对不上。

同一个文件还存当天的【判重指纹】：水印时间 + 工作内容。
两者都一致就认定是重复发送 —— 不计数、不点赞，只发一条警告。
跨天和计数一起清零。
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

log = logging.getLogger("feishu_bot.tally")

@dataclass
class DedupeSettings:
    enabled: bool = False
    template: str = (
        "⚠️ 这条和今天发过的重复了（水印时间 {stamp}），本单不计入统计。"
        "如果确实是新单，请重新拍照上传。"
    )


@dataclass
class TallySettings:
    enabled: bool = False
    # 计数落盘位置，相对项目根目录
    state_file: str = "tally.json"
    # 战报整体话术。{parts} 会被下面两个分段拼接后填进来。
    # 仍然支持 {broadband}/{high}，老配置不会坏。
    template: str = "恭喜{branch}{parts}！👍👍👍👍"
    # 分段话术，各自的 {count} 是该项当天累计。
    # 累计为 0 的项【不会出现在战报里】—— 播「破0」读起来很怪。
    part_broadband: str = '新增宽带破"{count}"'
    part_high: str = '新增中高套破"{count}"'
    part_sep: str = "、"


@dataclass
class Counts:
    broadband: int = 0
    high: int = 0


class Tally:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._date = ""
        self._branches: dict[str, Counts] = {}
        self._seen: set[str] = set()
        self._load()

    # ---------- 持久化 ----------

    def _today(self) -> str:
        return datetime.now().strftime("%Y-%m-%d")

    def _load(self) -> None:
        if not self.path.exists():
            self._date = self._today()
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("计数文件读不出来，按全新的一天开始：%s", exc)
            self._date = self._today()
            return

        if not isinstance(data, dict):
            # 合法 JSON 但不是对象（手改坏了）：以前这里 AttributeError，机器人起不来
            log.warning("计数文件不是对象（%s），按全新的一天开始", type(data).__name__)
            self._date = self._today()
            return
        saved_date = str(data.get("date") or "")
        if saved_date != self._today():
            # 存的是昨天（或更早）的数，直接丢弃 —— 跨天本来就该清零
            log.info("计数文件是 %s 的，今天重新开始计数", saved_date or "未知日期")
            self._date = self._today()
            return

        self._date = saved_date
        self._seen = set(data.get("seen") or [])
        for name, c in (data.get("branches") or {}).items():
            self._branches[name] = Counts(
                broadband=int(c.get("broadband", 0)),
                high=int(c.get("high", 0)),
            )
        if self._branches or self._seen:
            log.info("已恢复今日状态：%s，判重指纹 %d 条",
                     self.summary(), len(self._seen))

    def _save(self) -> None:
        """先写临时文件再替换，避免写一半崩了留下坏文件。"""
        payload = {
            "date": self._date,
            "branches": {n: {"broadband": c.broadband, "high": c.high}
                         for n, c in self._branches.items()},
            "seen": sorted(self._seen),
        }
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, self.path)
        except Exception as exc:
            log.warning("计数落盘失败（内存里的数还在）：%s", exc)

    # ---------- 计数 ----------

    def record(self, branch: str, broadband: int, high: int) -> Counts:
        """
        记一单，返回该支局当天的最新累计。
        broadband / high 由模型数出来，这里只负责累加和落盘。
        """
        with self._lock:
            self._roll_over_if_needed()
            counts = self._branches.setdefault(branch, Counts())
            counts.broadband += max(0, broadband)
            counts.high += max(0, high)
            self._save()
            return Counts(counts.broadband, counts.high)

    def claim(self, key: str) -> bool:
        """
        登记一个判重指纹。第一次见返回 True；已经见过返回 False（即重复）。
        检查和登记在同一把锁里完成，避免两条消息同时进来都判成「第一次」。
        """
        with self._lock:
            self._roll_over_if_needed()
            if key in self._seen:
                return False
            self._seen.add(key)
            self._save()
            return True

    def peek(self, branch: str) -> Counts:
        with self._lock:
            self._roll_over_if_needed()
            c = self._branches.get(branch, Counts())
            return Counts(c.broadband, c.high)

    def _roll_over_if_needed(self) -> None:
        """调用方必须已持有锁。"""
        today = self._today()
        if today != self._date:
            log.info("跨天了（%s -> %s），计数清零", self._date, today)
            self._date = today
            self._branches.clear()
            self._seen.clear()

    def summary(self) -> str:
        return "，".join(
            f"{n} 宽带{c.broadband}/中高套{c.high}" for n, c in self._branches.items()
        ) or "（无）"
