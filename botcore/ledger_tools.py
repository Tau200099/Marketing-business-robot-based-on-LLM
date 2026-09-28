"""
台账查询工具集 —— 问答助手和 MCP 服务器共用的那几个「查数」函数。

放在一个地方是为了口径只有一份：群里 @机器人 问「东林今天几单」，
和主管在 TeleAgent 桌面上问同一句话，查的是同一段代码、同一份台账。

每个方法都返回可直接 json.dumps 的 dict，字段名用中文 —— 调用方是大模型，
中文键它更不容易理解错。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

from .campaign import Campaign, CampaignSettings


class PeriodError(ValueError):
    """时间范围写法认不出。抛出去让模型换个写法重试，别静默当成今天（那样会拿错时段的数答得很肯定）。"""


_DAY_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日")


def _parse_day(s: str, today) -> "date":
    """2026-09-15 / 2026/9/15 / 2026.9.15 / 9.15 / 9月15日 / 9-15 -> date。认不出抛 PeriodError。"""
    from datetime import date
    s = s.strip().replace("－", "-").replace("／", "/")
    for fmt in _DAY_FORMATS:
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2})[./\-月](\d{1,2})日?", s)
    if m:
        try:
            return date(today.year, int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    raise PeriodError(f"时间范围写法不认识：{s!r}。请用 today / yesterday / week / last_week / "
                      f"month / last_month / 2026-09-15 / 2026-09-15~2026-09-21")


def resolve_period(spec: str, now: datetime | None = None) -> tuple[str, str, str]:
    """
    把 today / week / 2026-09-14 / 2026-09-08~2026-09-14 变成 (since, until, 说明)。
    认不出的写法抛 PeriodError —— 以前静默退回「今天」，模型会拿今天的数当所问时段答（审查踩出来的）。
    """
    now = now or datetime.now()
    today = now.date()
    spec = (spec or "today").strip().lower().replace("～", "~").replace("至", "~").replace("到", "~")

    def span(a, b):
        return f"{a} 00:00:00", f"{b} 23:59:59"

    if spec in ("today", "今天", ""):
        return *span(today, today), f"今天（{today.month}.{today.day}）"
    if spec in ("yesterday", "昨天"):
        d = today - timedelta(days=1)
        return *span(d, d), f"昨天（{d.month}.{d.day}）"
    if spec in ("week", "this_week", "本周", "这周"):
        mon = today - timedelta(days=today.weekday())
        sun = mon + timedelta(days=6)
        return *span(mon, sun), f"本周（{mon.month}.{mon.day}-{sun.month}.{sun.day}）"
    if spec in ("last_week", "上周"):
        mon = today - timedelta(days=today.weekday() + 7)
        sun = mon + timedelta(days=6)
        return *span(mon, sun), f"上周（{mon.month}.{mon.day}-{sun.month}.{sun.day}）"
    if spec in ("month", "this_month", "本月", "这个月"):
        first = today.replace(day=1)
        return *span(first, today), f"本月（{first.month}.1-{today.month}.{today.day}）"
    if spec in ("last_month", "上月", "上个月"):
        last_day = today.replace(day=1) - timedelta(days=1)
        first = last_day.replace(day=1)
        return *span(first, last_day), f"上月（{first.month}.1-{last_day.month}.{last_day.day}）"
    if "~" in spec:
        a, b = [_parse_day(x, today) for x in spec.split("~", 1)]
        if a > b:
            a, b = b, a
        return *span(a, b), f"{a.month}.{a.day} 至 {b.month}.{b.day}"
    d = _parse_day(spec, today)
    return *span(d, d), f"{d.month}.{d.day}"


def _split_members(members: str) -> set[str]:
    """「康思宇、王国强」/「康思宇王国强」-> {康思宇, 王国强}。以前用 `name in members`
    子串匹配，问「王兴几单」会算上「王兴国」的。名字连在一起没分隔符的按 2~3 字切不准，
    这种（照片时代的水印）只认整串相等。"""
    raw = (members or "").strip()
    if not raw:
        return set()
    parts = [p for p in re.split(r"[\s、,，/]+", raw) if p]
    return set(parts) if len(parts) > 1 else {raw}


def stem(team: str) -> str:
    """「东林战队」「东林支局」「东林」-> 「东林」。"""
    team = (team or "").strip()
    for suffix in ("战队", "支局", "战区"):
        if team.endswith(suffix):
            return team[: -len(suffix)]
    return team


def order_row(o) -> dict:
    return {"时间": o.at[5:16], "类型": o.kind, "套餐": o.fee, "揽收人": o.collector,
            "战队": o.team, "小组": o.group, "积分": o.points,
            "待核对": o.needs_review, "备注": o.note}


class LedgerTools:
    """
    campaign_getter 每次调用都会被调一次 —— MCP 服务器是独立进程，
    台账文件由机器人进程在写，所以那边每次都重新读文件，这里不做缓存。
    """

    def __init__(self, campaign_getter, campaign_settings_getter) -> None:
        self._campaign = campaign_getter
        self._camp_cfg = campaign_settings_getter

    def team_stats(self, period: str = "today") -> dict:
        since, until, label = resolve_period(period)
        camp: Campaign = self._campaign()
        cfg: CampaignSettings = self._camp_cfg()
        camp.targets = cfg.targets
        rows = []
        for t in camp.stats(since, until):
            # 进度为 0 的不给名次 —— 六个 0 分硬排出 1~6 名，说出去像在点名
            ranked = bool(t.rank) and t.progress > 0
            rows.append({"战队": t.team, "排名": t.rank if ranked else None,
                         "综合进度": f"{t.progress:.1%}" if t.rank else None,
                         "单数": t.orders, "新增宽带": t.broadband, "中高套": t.high,
                         "长续约": t.renew, "千兆": t.giga, "单号卡": t.card,
                         "云盒AI智屏": t.box, "积分": t.points, "待核对": t.pending})
        tg = camp.targets
        return {"范围": label, "活动": cfg.title,
                "目标": {"新增宽带": tg.broadband, "中高套": tg.high,
                         "云盒AI智屏": tg.box, "积分": tg.points},
                "综合进度公式": "新增宽带/目标×25% + 中高套/目标×30% + 云盒AI智屏/目标×15% + 积分/目标×20%",
                "说明": "排名为 null 的战队进度是 0，不分名次", "战队": rows}

    def group_stats(self, period: str = "today", team: str = "", only_zero: bool = False) -> dict:
        since, until, label = resolve_period(period)
        camp: Campaign = self._campaign()
        team = stem(team)
        rows = []
        for g in camp.group_stats(since, until):
            if team and stem(g.team) != team:
                continue
            if only_zero and g.orders:
                continue
            rows.append({"战队": g.team, "小组": g.label, "成员": g.members,
                         "单数": g.orders, "新增宽带": g.broadband, "中高套": g.high,
                         "长续约": g.renew, "单号卡": g.card, "云盒AI智屏": g.box,
                         "积分": g.points})
        return {"范围": label, "小组数": len(rows), "小组": rows}

    def person_stats(self, name: str, period: str = "today") -> dict:
        name = (name or "").strip()
        since, until, label = resolve_period(period)
        camp: Campaign = self._campaign()
        grp, team = camp.roster.lookup(name)
        with camp._lock:
            orders = [o for o in camp.orders
                      if since <= o.at <= until
                      and (o.collector == name or name in _split_members(o.members))]
        return {"姓名": name, "小组": grp or "不在名册", "战队": team or "不在名册",
                "范围": label, "单数": len(orders),
                "积分": sum(o.points for o in orders),
                "新增宽带": sum(o.broadband for o in orders),
                "中高套": sum(o.high for o in orders),
                "明细": [order_row(o) for o in orders[-20:]]}

    def list_orders(self, period: str = "today", team: str = "", limit: int = 20) -> dict:
        since, until, label = resolve_period(period)
        team = stem(team)
        limit = max(1, min(int(limit or 20), 200))
        camp: Campaign = self._campaign()
        with camp._lock:
            orders = [o for o in camp.orders if since <= o.at <= until
                      and (not team or stem(o.team) == team)]
        orders.sort(key=lambda o: o.at, reverse=True)
        return {"范围": label, "总数": len(orders),
                "单": [order_row(o) for o in orders[:limit]]}

    def roster(self, query: str = "") -> dict:
        q = (query or "").strip()
        roster = self._campaign().roster
        if not q:
            return {"战队": roster.teams, "人数": len(roster.people),
                    "小组数": len(roster.groups())}
        grp, team = roster.lookup(q)
        if team:
            return {"姓名": q, "小组": grp, "战队": team}
        s = stem(q)
        groups = [{"小组": g.label, "成员": g.members}
                  for g in roster.groups() if stem(g.team) == s]
        if groups:
            return {"战队": s + "战队", "小组": groups}
        return {"error": f"名册里没有「{q}」这个人或战队"}
