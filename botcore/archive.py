"""
报表存档（09-28 用户定：JSON 备份和 Excel 报表分开；报表按周期存）。

    晒单_YYYY-MM-DD.xlsx   「晒单数据」（那天各小组 / 战队汇总和排名）+「清单」（那周周一到那天的每一单）
    打卡_YYYY-MM-DD.xlsx   那天上午 / 下午 / 晚上三张打卡表

周期和报表口径一致：晒单数据按天、打卡按天、清单按周（周日那份就是整周的完整清单）。

每天凌晨（cron 02:35）存【前一天】的两份到 data/archive/晒单/、data/archive/打卡/。
存档和 reports/ 里现生成的表格都只留 keep_days 天（和 JSON 备份同一个设置，默认 14），09-28 用户定。
单聊要表格时：要今天的当场生成；要以前的优先拿存档（存档是当天结束时的样子），
没存档就用现在的数据补生成一份（换活动清过台账的话就生成不出来了，所以要靠存档）。

JSON 原始数据的备份在 backup.py，只发给管理员；这里的 Excel 是给人看的报表。
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

from .paths import DATA

log = logging.getLogger("botcore.archive")

ARCHIVE_DIR = DATA / "archive"
KINDS = ("晒单", "打卡")


def _campaign(store):
    from .campaign import Campaign, Roster
    from . import regions
    if regions.multi_enabled(store):
        rs = regions.RosterSet()
        return Campaign(rs.activity[regions.CITY], store.campaign_settings().targets, rosters=rs)
    return Campaign(Roster(), store.campaign_settings().targets)


def _region_camp(store, camp, region: str | None):
    """多群某区：只读视图（这个区活动人员的单 + 这个区的活动名册和目标）。"""
    if region and getattr(camp, "rosters", None) is not None:
        return camp.view(region, store.campaign_settings().targets_for(region))
    return camp


def build_sales(store, camp, day: date, out: Path, region: str | None = None) -> Path:
    """晒单 Excel：那天的晒单数据 + 那周周一到那天的清单。region：多群模式的 城区 / 农村。"""
    from .report import build_workbook, summary_title
    cfg = store.campaign_settings()
    camp = _region_camp(store, camp, region)
    end = datetime.combine(day, dtime(23, 59, 59))
    now = min(end, datetime.now())
    rank = cfg.window("rank", end)
    l_since, l_until = cfg.window("ledger", end)
    ledger = (l_since, min(l_until, end.strftime("%Y-%m-%d %H:%M:%S")))   # 截到那天为止，别带上后面几天的单
    title = summary_title(cfg.period_label("rank", end), cfg.title, now)
    return build_workbook(camp, out, title, rank=rank, ledger=ledger)


def build_checkin(store, camp, day: date, out: Path, region: str | None = None) -> Path:
    """打卡 Excel：那天上午 / 下午 / 晚上三张表。region：多群模式的 城区 / 农村。"""
    from openpyxl import Workbook
    from .checkin import day_sheets, write_checkin_sheet
    ck = store.checkin_settings()
    roster = _region_camp(store, camp, region).roster
    wb = Workbook()
    wb.remove(wb.active)
    for name, title, records in day_sheets(ck, day.strftime("%Y-%m-%d"), store.campaign_settings().title, region):
        write_checkin_sheet(wb.create_sheet(name), records, roster, title, ck.columns)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


_BUILDERS = {"晒单": build_sales, "打卡": build_checkin}


def _hq_builders() -> dict:
    """攻坚群的两张表（支局总表、支局长打卡点检表）：各自模块提供 build_excel(store, camp, day, out)。"""
    out = {}
    try:
        from .zongbiao import build_excel as zb
        out["总表"] = zb
    except ImportError:
        pass
    try:
        from .chiefs import build_excel as cb
        out["支局长"] = cb
    except ImportError:
        pass
    return out


def specs(store) -> list[tuple[str, str | None]]:
    """要存哪些表：单群 [(晒单, None), (打卡, None)]；多群 两区各一份晒单、打卡 + 总表 + 支局长。"""
    from . import regions
    ck = store.checkin_settings().enabled
    if not regions.multi_enabled(store):
        return [(k, None) for k in KINDS if k != "打卡" or ck]
    out = []
    for region in regions.REGIONS:
        out.append(("晒单", region))
        if ck:
            out.append(("打卡", region))
    out += [(k, None) for k in _hq_builders()]
    return out


def _name(kind: str, region: str | None) -> str:
    return f"{kind}_{region}" if region else kind


def archive_path(kind: str, day: date, region: str | None = None) -> Path:
    return ARCHIVE_DIR / kind / f"{_name(kind, region)}_{day:%Y-%m-%d}.xlsx"


def _build(store, camp, kind: str, day: date, out: Path, region: str | None) -> Path:
    if kind in _BUILDERS:
        return _BUILDERS[kind](store, camp, day, out, region)
    return _hq_builders()[kind](store, camp, day, out)


def archive_day(store, day: date) -> list[Path]:
    """存某天的报表（覆盖已有的）。"""
    camp = _campaign(store)
    out = []
    for kind, region in specs(store):
        try:
            p = _build(store, camp, kind, day, archive_path(kind, day, region), region)
        except Exception:
            log.exception("存档 %s 失败", _name(kind, region))
            continue
        log.info("报表已存档：%s", p.name)
        out.append(p)
    return out


def prune(keep_days: int, today: date | None = None) -> int:
    """删 keep_days 天以前的存档（按文件名末尾的日期）和 reports/ 里的表格图片（按修改时间）。"""
    today = today or date.today()
    cutoff = today - timedelta(days=keep_days)
    n = 0
    for p in ARCHIVE_DIR.glob("*/*.xlsx") if ARCHIVE_DIR.is_dir() else ():
        try:
            if datetime.strptime(p.stem[-10:], "%Y-%m-%d").date() < cutoff:
                p.unlink()
                n += 1
        except ValueError:
            continue
    reports = DATA / "reports"
    limit = datetime.combine(cutoff, dtime()).timestamp()
    for p in reports.glob("*"):
        if p.is_file() and p.suffix.lower() in (".xlsx", ".png") and p.stat().st_mtime < limit:
            p.unlink()
            n += 1
    if n:
        log.info("已清理 %d 天前的报表 %d 个", keep_days, n)
    return n


def get_excel(store, camp, kind: str, day: date, region: str | None = None) -> Path | None:
    """
    单聊要表格用：今天的当场生成（放 reports/，不进存档）；以前的优先拿存档，没有就用现在的数据补一份。
    打卡没开就没有打卡表、没有那个模块就没有那张表，返回 None。
    """
    if kind == "打卡" and not store.checkin_settings().enabled:
        return None
    if kind not in _BUILDERS and kind not in _hq_builders():
        return None
    if day < date.today():
        p = archive_path(kind, day, region)
        if p.is_file():
            return p
    out_dir = DATA / store.bulletin_settings().out_dir
    stamp = datetime.now().strftime("%H%M%S")
    return _build(store, camp, kind, day, out_dir / f"{_name(kind, region)}_{day:%Y-%m-%d}_{stamp}.xlsx", region)


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s | %(message)s")
    from .config import ConfigStore, load_env
    load_env()
    store = ConfigStore()
    # 默认存昨天；也可以 python -m botcore.archive 2026-09-25 补存某天
    day = datetime.strptime(argv[0], "%Y-%m-%d").date() if argv else date.today() - timedelta(days=1)
    archive_day(store, day)
    try:
        keep = store.backup_settings().keep_days
    except Exception:
        keep = 14
    prune(keep)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
