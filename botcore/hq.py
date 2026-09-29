"""
攻坚群发什么（09-29 国庆版）：每个定点、群里喊「通报」→ 支局总表（文字一段 + 表格图）+ 支局长打卡点检表（图）；
群里喊「打卡通报」→ 只发支局长打卡点检表。
"""

from __future__ import annotations

import logging
from datetime import datetime

from .paths import DATA

log = logging.getLogger("botcore.hq")


class HqPublisher:
    def __init__(self, platform, store, campaign_getter, chief_board=None) -> None:
        self.platform = platform
        self.store = store
        self._campaign = campaign_getter
        self.chief_board = chief_board          # chiefs.ChiefBoard（第 6 块）

    def _send_total(self, chat: str, now: datetime) -> bool:
        from . import zongbiao
        camp = self._campaign()
        camp.refresh_roster()
        table = zongbiao.build_table(self.store.guoqing_settings(), list(camp.orders), now)
        png = zongbiao.render_png(table)
        out = DATA / self.store.bulletin_settings().out_dir
        out.mkdir(parents=True, exist_ok=True)
        (out / f"支局总表_{now:%m%d_%H%M}.png").write_bytes(png)
        ok = self.platform.send_text(chat, zongbiao.caption(table))
        if hasattr(self.platform, "send_image"):
            ok = self.platform.send_image(chat, png, f"支局总表_{now:%m%d_%H%M}.png") and ok
        return bool(ok)

    def _send_chiefs(self, chat: str, now: datetime) -> bool:
        if self.chief_board is None:
            return True
        return self.chief_board.send(self.platform, chat, now)

    def chief_times(self) -> list:
        return [str(t) for t in (getattr(self.store.guoqing_settings(), "chief_times", None) or ())]

    def publish(self, chats, reason: str = "", now: datetime | None = None) -> dict:
        """总表每个定点都发；支局长点检表只在 chief_times 那几个定点跟着发（09-30 用户：11:50 / 18:00 / 21:30）。"""
        now = now or datetime.now()
        with_chiefs = reason.startswith("定点") and reason[2:] in self.chief_times()
        result = {"ok": [], "failed": []}
        for chat in chats:
            try:
                ok = self._send_total(chat, now)
                if with_chiefs:
                    ok = self._send_chiefs(chat, now) and ok
            except Exception:
                log.exception("攻坚群通报失败（%s）-> %s", reason, chat)
                ok = False
            (result["ok"] if ok else result["failed"]).append(chat)
            log.info("攻坚群通报%s（%s）-> %s", "已发" if ok else "发送失败", reason, chat)
        return result

    def publish_chiefs_only(self, chats, reason: str = "", now: datetime | None = None) -> dict:
        """只发支局长点检表（21:30 这种不和通报定点重合的时刻）。"""
        now = now or datetime.now()
        result = {"ok": [], "failed": []}
        for chat in chats:
            try:
                ok = self._send_chiefs(chat, now)
            except Exception:
                log.exception("支局长点检表发送失败（%s）-> %s", reason, chat)
                ok = False
            (result["ok"] if ok else result["failed"]).append(chat)
            log.info("支局长点检表%s（%s）-> %s", "已发" if ok else "发送失败", reason, chat)
        return result

    def publish_chiefs(self, chat: str) -> None:
        try:
            self._send_chiefs(chat, datetime.now())
        except Exception:
            log.exception("支局长打卡点检表发送失败 -> %s", chat)
