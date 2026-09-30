"""
当前活动（09-28 用户定：换活动不再手改配置，管理员单聊一句话切换）。

    新活动 秋促专项营销活动 10.1-10.4    开新活动（名称可省，省了沿用上一个）
    当前活动                            看名称、日期、进行到第几天、名册人数
    结束活动                            提前结束

活动存在 data/activity.json，config.yaml 不动。有这个文件就进入「按活动日期」模式：
  · 活动日期以外【全部停】（用户定：不照常）：群里晒单、打卡照片不记，回一句「不在活动期间」；
    业绩定点通报、打卡通报、表情包都不发。单聊（测试 / 样本通道）不受影响，
    「通报」「打卡通报」「表格发我」「备份」这些手动要的照常
  · 活动期间哪天都发，不再看 config 里按星期配的 days / bulletin_days / active_days
    （国庆这种 7 天的活动不用再改星期）
  · 活动名换掉 campaign.title，活动日期换掉 checkin.span（打卡表标题）
没有这个文件（本机调试、新装）就还是按 config 里的星期走，和以前一样。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .paths import DATA

log = logging.getLogger("botcore.activity")

PATH = DATA / "activity.json"
MAX_DAYS = 31


@dataclass
class Activity:
    title: str
    start: str                  # YYYY-MM-DD
    end: str                    # YYYY-MM-DD，含这天
    ended_at: str = ""          # 「结束活动」提前结束的时刻 YYYY-MM-DD HH:MM:SS
    set_at: str = ""
    set_by: str = ""
    history: list = field(default_factory=list)     # 以前的活动，最近 20 个

    @property
    def start_date(self) -> date:
        return date.fromisoformat(self.start)

    @property
    def end_date(self) -> date:
        return date.fromisoformat(self.end)

    def span(self) -> str:
        """打卡表标题用：9.24-9.27；一天的活动就 10.1。"""
        s, e = self.start_date, self.end_date
        return f"{s.month}.{s.day}" if s == e else f"{s.month}.{s.day}-{e.month}.{e.day}"

    def status(self, now: datetime | None = None) -> str:
        """进行中 / 未开始 / 已结束"""
        now = now or datetime.now()
        if self.ended_at and now.strftime("%Y-%m-%d %H:%M:%S") >= self.ended_at:
            return "已结束"
        if now.date() < self.start_date:
            return "未开始"
        if now.date() > self.end_date:
            return "已结束"
        return "进行中"


_lock = threading.Lock()
_cache: tuple[tuple, Activity | None] = ((), None)


def _stamp() -> tuple:
    """
    文件的「版本」：修改时间（纳秒）+ 大小 + inode。
    只看 mtime 不够：同一个时钟刻度里连着改两次（两条「新活动」挨着发），mtime 一样，缓存不换，
    还在用旧活动（09-29 测试 1/6 概率踩到）。_save 是写临时文件再替换，每次 inode 都变，加上它就稳了。
    """
    st = PATH.stat()
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def load() -> Activity | None:
    """当前活动；没有 activity.json 返回 None。按文件版本缓存，改了文件马上生效。"""
    global _cache
    try:
        mtime = _stamp()
    except OSError:
        return None
    with _lock:
        if _cache[0] == mtime:
            return _cache[1]
        try:
            raw = json.loads(PATH.read_text(encoding="utf-8"))
            act = Activity(**{k: v for k, v in raw.items() if k in Activity.__dataclass_fields__})
            act.start_date, act.end_date          # 日期写坏了当没有
        except Exception as exc:
            log.error("activity.json 读不了，按没有活动处理：%s", exc)
            act = None
        _cache = (mtime, act)
        return act


def mtime() -> tuple:
    """活动文件的版本（配置覆盖层拿它当缓存键）。没有文件返回空元组。"""
    try:
        return _stamp()
    except OSError:
        return ()


def managed() -> bool:
    """有没有 activity.json —— 有就按活动日期管开关。"""
    return PATH.is_file()


def is_active(now: datetime | None = None) -> bool:
    act = load()
    return act is not None and act.status(now) == "进行中"


def allows(now: datetime, weekdays) -> bool:
    """
    定时任务今天能不能跑：有活动文件就看是不是在活动期间；
    没有就按老办法看星期（weekdays 是 config 里配的 ISO 星期）。
    """
    if managed():
        return is_active(now)
    return now.isoweekday() in set(weekdays or ())


def _save(act: Activity) -> None:
    from .backup import keep_version
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(act), ensure_ascii=False, indent=2), encoding="utf-8")
    keep_version(PATH)
    tmp.replace(PATH)
    global _cache
    with _lock:
        _cache = ((), None)          # 刚写过：下次 load 一定重读


# ---------------- 解析「新活动 秋促专项营销活动 10.1-10.4」 ----------------

_D = r"(\d{1,2})\s*[.．/月]\s*(\d{1,2})\s*[日号]?"
_RANGE = re.compile(_D + r"(?:\s*(?:-|－|~|～|—+|至|到)\s*(?:(\d{1,2})\s*[.．/月]\s*)?(\d{1,2})\s*[日号]?)?")
_CMD_NEW = re.compile(r"^\s*(?:新活动|开始活动|开新活动|换活动)")
_CMD_SHOW = re.compile(r"^\s*(?:当前活动|活动情况|现在什么活动|活动状态)\s*[?？]?\s*$")
_CMD_END = re.compile(r"^\s*(?:结束活动|活动结束|停止活动)\s*$")


def _pick_year(month: int, day: int, today: date) -> date:
    """没写年份：默认今年；月份比现在早半年以上的算明年（12 月开 1 月的活动）。"""
    year = today.year + (1 if month < today.month - 6 else 0)
    return date(year, month, day)


def parse_new(text: str, today: date | None = None) -> tuple[str, date, date]:
    """返回 (名称, 开始, 结束)。名称可能为空。日期不对抛 ValueError（带给人看的原因）。"""
    today = today or date.today()
    body = _CMD_NEW.sub("", text or "", count=1)
    m = _RANGE.search(body)
    if not m:
        raise ValueError("没看到日期，这样发：新活动 秋促专项营销活动 10.1-10.4")
    m1, d1, m2, d2 = m.group(1), m.group(2), m.group(3), m.group(4)
    try:
        start = _pick_year(int(m1), int(d1), today)
        if d2:
            end = date(start.year, int(m2 or m1), int(d2))
            if end < start:                     # 12.30-1.2
                end = date(start.year + 1, end.month, end.day)
        else:
            end = start
    except ValueError:
        raise ValueError(f"日期「{m.group(0).strip()}」不对")
    if end < today:
        raise ValueError(f"结束日期 {end.month}.{end.day} 已经过了")
    if (end - start).days + 1 > MAX_DAYS:
        raise ValueError(f"活动 {(end - start).days + 1} 天，超过 {MAX_DAYS} 天了，日期是不是写错了")
    title = (body[:m.start()] + " " + body[m.end():])
    title = re.sub(r"[，,。:：\s]+", " ", title).strip()
    return title, start, end


def start_new(title: str, start: date, end: date, by: str, default_title: str) -> Activity:
    old = load()
    history = list(old.history) if old else []
    if old:
        prev = asdict(old)
        prev.pop("history", None)
        history = (history + [prev])[-20:]
    act = Activity(title=title or (old.title if old else "") or default_title,
                   start=start.isoformat(), end=end.isoformat(),
                   set_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), set_by=by, history=history)
    _save(act)
    log.info("新活动：%s %s（%s 设置）", act.title, act.span(), by)
    return act


def end_now(by: str) -> Activity | None:
    act = load()
    if act is None or act.status() != "进行中":
        return act
    act.ended_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    act.set_by = by
    _save(act)
    log.info("活动提前结束：%s（%s）", act.title, by)
    return act


_WEEK = "一二三四五六日"


def describe(act: Activity | None, now: datetime | None = None) -> str:
    """给人看的一段：名称、日期、星期、状态。"""
    if act is None:
        return "现在没有设置活动"
    now = now or datetime.now()
    s, e = act.start_date, act.end_date
    days = (e - s).days + 1
    head = f"「{act.title}」{act.span()}（周{_WEEK[s.weekday()]}到周{_WEEK[e.weekday()]}，共 {days} 天）"
    st = act.status(now)
    if st == "进行中":
        return f"{head}，进行中，今天是第 {(now.date() - s).days + 1} 天"
    if st == "未开始":
        return f"{head}，还没开始，{(s - now.date()).days} 天后开始"
    if act.ended_at:
        return f"{head}，已在 {act.ended_at[5:16]} 提前结束"
    return f"{head}，已结束"


def off_notice(now: datetime | None = None) -> str:
    """活动期外群里发晒单 / 打卡时回的话。"""
    act = load()
    now = now or datetime.now()
    if act is not None and act.status(now) == "未开始":
        s = act.start_date
        return f"现在不在活动期间，晒单和打卡没有记录。「{act.title}」{s.month}.{s.day} 开始"
    return "现在不在活动期间，晒单和打卡没有记录"
