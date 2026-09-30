"""
表情包自动化：不靠消息触发的那几类。

三种触发方式：
    1) 捷报频传 —— 业绩扎堆时。由 pipeline 在记录业绩时【同步】触发，
       因为它要跟在点赞后面，靠轮询定时器会错位。
    2) 全力以赴 —— 长时间没业绩。后台线程定时检查。
    3) 早上/下午加油 —— 每天固定时刻。后台线程定时检查。

都受【活跃时段】约束（默认周四到周日 8:30-20:30）：
    窗口外一律不发。半夜给群里刷表情包是灾难。

为什么状态要落盘：
    「今天的早上加油发过了吗」必须跨重启记住，否则进程一重启就重发。
    存在 stickers_state.json 里，和图库缓存分开 —— 那个删了只是重传，
    这个删了会重发消息。
"""

from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

from . import activity
from .paths import DATA

log = logging.getLogger("botcore.automation")


def _parse_hhmm(text: str, fallback: dtime) -> dtime:
    try:
        h, m = str(text).split(":")
        return dtime(int(h), int(m))
    except Exception:
        log.warning("时间格式不对：%r，应为 HH:MM。用默认值 %s", text, fallback)
        return fallback


class ActivityTracker:
    """
    记录业绩发生的时刻，用来判断「扎堆」和「冷清」。

    只保留窗口内的时间戳，不会无限增长。
    """

    def __init__(self, window_seconds: float = 3600.0) -> None:
        self._window = window_seconds
        self._stamps: list[float] = []
        self._lock = threading.Lock()
        # 启动时先当作「刚刚有过活动」，否则一起来就判定冷清并发全力以赴
        self._last_any: float = time.time()

    def record(self) -> int:
        """记一笔业绩，返回当前窗口内的累计条数。"""
        now = time.time()
        with self._lock:
            self._stamps.append(now)
            self._trim(now)
            self._last_any = now
            return len(self._stamps)

    def count(self) -> int:
        now = time.time()
        with self._lock:
            self._trim(now)
            return len(self._stamps)

    def idle_seconds(self) -> float:
        with self._lock:
            return time.time() - self._last_any

    def note_activity(self) -> None:
        """有任何值得算作「不冷清」的动静时调用。"""
        with self._lock:
            self._last_any = time.time()

    def _trim(self, now: float) -> None:
        cutoff = now - self._window
        self._stamps = [t for t in self._stamps if t >= cutoff]


@dataclass
class _State:
    """落盘的状态。键是日期字符串，避免跨天误判。"""
    daily_sent: dict[str, str] = field(default_factory=dict)   # 分类 -> 已发的日期
    last_idle_at: float = 0.0
    last_streak_at: float = 0.0


class StickerAutomation:
    """后台线程：定时检查冷清和每日定点，该发就发。"""

    def __init__(self, platform, store, pool_getter, state_file: str = "stickers_state.json") -> None:
        self.platform = platform
        self.store = store
        self._get_pool = pool_getter
        self.activity = ActivityTracker()
        self._state_path = DATA / state_file
        self._state = self._load()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # 上一个 tick 是否在活跃时段内。从窗外进窗内那一刻要把冷清计时归零
        self._was_active = False
        self._streak_lock = threading.Lock()

    # ---------- 生命周期 ----------

    def start(self) -> None:
        cfg = self.store.sticker_settings()
        if not cfg.enabled:
            return
        if not self._target_chats():
            log.info("没配 %s_CHAT_IDS，定时表情包不启用"
                     "（点赞和捷报仍然正常，那两个跟着消息走）",
                     self.platform.name.upper())
            return
        self._thread = threading.Thread(target=self._loop, name="stickers",
                                        daemon=True)
        self._thread.start()
        log.info("表情包定时任务已启动：活跃时段 %s", self._window_desc(cfg))

    def close(self) -> None:
        self._stop.set()

    # ---------- 主循环 ----------

    def _loop(self) -> None:
        # 30 秒一轮：定点任务精度够（分钟级），冷清检查也不需要更密
        while not self._stop.wait(30):
            try:
                self._tick()
            except Exception:
                log.exception("表情包定时检查出错，下轮继续")

    def _tick(self) -> None:
        cfg = self.store.sticker_settings()
        if not cfg.enabled:
            return
        now = datetime.now()
        # 定点任务【自己管星期】：每条可以单独配 days，
        # 比如早上加油要天天发，不跟着全局的周四到周日
        self._check_daily(cfg, now)
        # 冷清鼓劲没有固定时刻，必须受全局活跃时段约束
        active = self.in_active_window(cfg, now)
        if active and not self._was_active:
            # 刚进活跃时段：冷清从现在起算。否则周四 08:30 窗口一开，
            # 「上一单」还是周日晚上的，全力以赴和早上加油同一秒刷出来（审查踩出来的）
            self.activity.note_activity()
            log.info("进入活跃时段，冷清计时从现在开始")
        self._was_active = active
        if active:
            self._check_idle(cfg, now)

    # ---------- 活跃时段 ----------

    @staticmethod
    def in_active_window(cfg, now: datetime | None = None) -> bool:
        """
        是否在允许发送的时段内。

        active_days 用 ISO 星期：周一=1 … 周日=7。
        """
        now = now or datetime.now()
        if not activity.allows(now, cfg.active_days):      # 有活动文件就看活动日期（09-28）
            return False
        start = _parse_hhmm(cfg.active_start, dtime(8, 30))
        end = _parse_hhmm(cfg.active_end, dtime(20, 30))
        return start <= now.time() <= end

    @staticmethod
    def _window_desc(cfg) -> str:
        if activity.managed():
            act = activity.load()
            return f"活动期间（{act.span() if act else '未设置'}）{cfg.active_start}-{cfg.active_end}"
        names = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五", 6: "六", 7: "日"}
        days = "".join(names.get(d, "?") for d in sorted(cfg.active_days or ()))
        return f"周{days} {cfg.active_start}-{cfg.active_end}"

    # ---------- 每日定点 ----------

    def _check_daily(self, cfg, now: datetime) -> None:
        """
        每日定点。每条可以单独配 days（ISO 星期，周一=1…周日=7），
        不配就沿用全局 active_days。

        时刻由条目自己的 at 决定，所以全局的 active_start/end 不再叠加限制 ——
        否则「天天 8:30 发」会被全局 8:30 的边界卡掉半分钟。
        """
        today = now.date().isoformat()
        for item in cfg.daily or ():
            cat = item.get("category")
            if not cat:
                continue
            days = item.get("days") or cfg.active_days or ()
            if not activity.allows(now, days):       # 有活动文件时活动期外一律不发（用户 09-28：不照常）
                continue
            if self._state.daily_sent.get(cat) == today:
                continue
            at = _parse_hhmm(item.get("at", ""), dtime(8, 30))
            # 到点后 10 分钟内都算数：进程刚好在那一分钟没跑也能补上，
            # 但不会把上午的任务拖到下午才发
            due = datetime.combine(now.date(), at)
            if not (due <= now <= due + timedelta(minutes=10)):
                continue
            n = self._send(cat, int(item.get("count", 1) or 1))
            if n:
                self._state.daily_sent[cat] = today
                self._save()
                log.info("定点表情包「%s」已发 %d 张", cat, n)

    # ---------- 冷清鼓劲 ----------

    def _check_idle(self, cfg, now: datetime) -> None:
        if not cfg.idle_enabled:
            return
        if self.activity.idle_seconds() < cfg.idle_after_minutes * 60:
            return
        # 距上次发冷清表情包还没到间隔就先不发 —— 否则会每 30 秒刷一次
        since_last = time.time() - self._state.last_idle_at
        if since_last < cfg.idle_repeat_minutes * 60:
            return

        n = self._send("全力以赴", cfg.idle_count)
        if not n:
            return
        self._state.last_idle_at = time.time()
        extra = ""
        if random.random() < cfg.idle_bonus_probability:
            if self._send("晒单", 1):
                extra = " + 晒单"
        self._save()
        log.info("冷清 %.0f 分钟，发了全力以赴 %d 张%s",
                 self.activity.idle_seconds() / 60, n, extra)

    # ---------- 捷报频传（由 pipeline 调用）----------

    def streak_pending(self, cfg) -> bool:
        """
        这一条会不会触发捷报？【纯判断，不发送】。

        pipeline 要先知道结果才能决定点赞图发几张 —— 捷报要发时点赞就少发一张，
        免得一次刷出四条消息。所以判断和发送必须拆开。
        """
        if not cfg.streak_enabled:
            return False
        if self.activity.count() < cfg.streak_threshold:
            return False
        if time.time() - self._state.last_streak_at < cfg.streak_cooldown_minutes * 60:
            return False
        pool = self._get_pool()
        return pool is not None and pool.has("捷报频传")

    def maybe_streak(self, chat_id: str, cfg) -> int:
        """
        业绩扎堆时发捷报。由 pipeline 在点赞之后调用，
        这样捷报能紧跟在点赞后面，而不是等定时器错位半分钟。
        """
        # 判断和占位在一把锁里：两条业绩同一秒到达、都算到第 4 条时，
        # 只有先进来的那个能发（以前各发一张，群里连刷两张捷报）
        with self._streak_lock:
            if not self.streak_pending(cfg):
                return 0
            self._state.last_streak_at = time.time()
        pool = self._get_pool()
        n = self.platform.send_stickers(chat_id, pool, "捷报频传", cfg.streak_count)
        if n:
            self._save()
            log.info("1 小时内已有 %d 条业绩，发捷报频传 %d 张",
                     self.activity.count(), n)
        else:
            with self._streak_lock:
                self._state.last_streak_at = 0.0     # 没发出去就把占位还回去
        return n

    # ---------- 发送 ----------

    def _target_chats(self) -> tuple[str, ...]:
        """定时任务没有「触发它的那条消息」，所以群要显式配。多群模式只发两个活动群（09-29 用户定）。"""
        from . import regions
        if regions.multi_enabled(self.store):
            return regions.role_chats(self.store, "city") + regions.role_chats(self.store, "rural")
        return tuple(getattr(getattr(self.platform, "cfg", None), "chat_ids", ()) or ())

    def _send(self, category: str, count: int) -> int:
        pool = self._get_pool()
        if pool is None or not pool.has(category):
            return 0
        total = 0
        for chat_id in self._target_chats():
            try:
                total += self.platform.send_stickers(chat_id, pool, category, count)
            except Exception:
                log.exception("发「%s」到群失败", category)
        return total

    # ---------- 状态落盘 ----------

    def _load(self) -> _State:
        if not self._state_path.exists():
            return _State()
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            return _State(daily_sent=dict(raw.get("daily_sent") or {}),
                          last_idle_at=float(raw.get("last_idle_at") or 0),
                          last_streak_at=float(raw.get("last_streak_at") or 0))
        except Exception as exc:
            log.warning("表情包状态读不了，当作全新的：%s", exc)
            return _State()

    def _save(self) -> None:
        try:
            self._state_path.write_text(json.dumps({
                "daily_sent": self._state.daily_sent,
                "last_idle_at": self._state.last_idle_at,
                "last_streak_at": self._state.last_streak_at,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("表情包状态写不进去：%s", exc)
