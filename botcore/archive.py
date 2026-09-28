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
    camp = Campaign(Roster(), store.campaign_settings().targets)
    return camp


def build_sales(store, camp, day: date, out: Path) -> Path:
    """晒单 Excel：那天的晒单数据 + 那周周一到那天的清单。"""
    from .report import build_workbook, summary_title
    cfg = store.campaign_settings()
    end = datetime.combine(day, dtime(23, 59, 59))
    now = min(end, datetime.now())
    rank = cfg.window("rank", end)
    l_since, l_until = cfg.window("ledger", end)
    ledger = (l_since, min(l_until, end.strftime("%Y-%m-%d %H:%M:%S")))   # 截到那天为止，别带上后面几天的单
    title = summary_title(cfg.period_label("rank", end), cfg.title, now)
    return build_workbook(camp, out, title, rank=rank, ledger=ledger)


def build_checkin(store, camp, day: date, out: Path) -> Path:
    """打卡 Excel：那天上午 / 下午 / 晚上三张表。"""
    from openpyxl import Workbook
    from .checkin import day_sheets, write_checkin_sheet
    ck = store.checkin_settings()
    wb = Workbook()
    wb.remove(wb.active)
    for name, title, records in day_sheets(ck, day.strftime("%Y-%m-%d"), store.campaign_settings().title):
        write_checkin_sheet(wb.create_sheet(name), records, camp.roster, title, ck.columns)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


_BUILDERS = {"晒单": build_sales, "打卡": build_checkin}


def archive_path(kind: str, day: date) -> Path:
    return ARCHIVE_DIR / kind / f"{kind}_{day:%Y-%m-%d}.xlsx"


def archive_day(store, day: date) -> list[Path]:
    """存某天的两份报表（覆盖已有的）。"""
    camp = _campaign(store)
    out = []
    for kind in KINDS:
        if kind == "打卡" and not store.checkin_settings().enabled:
            continue
        p = _BUILDERS[kind](store, camp, day, archive_path(kind, day))
        log.info("报表已存档：%s", p.name)
        out.append(p)
    return out


def prune(keep_days: int, today: date | None = None) -> int:
    """删 keep_days 天以前的存档（按文件名里的日期）和 reports/ 里的表格图片（按修改时间）。"""
    today = today or date.today()
    cutoff = today - timedelta(days=keep_days)
    n = 0
    for kind in KINDS:
        for p in (ARCHIVE_DIR / kind).glob(f"{kind}_*.xlsx"):
            try:
                if datetime.strptime(p.stem[len(kind) + 1:], "%Y-%m-%d").date() < cutoff:
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


def get_excel(store, camp, kind: str, day: date) -> Path | None:
    """
    单聊要表格用：今天的当场生成（放 reports/，不进存档）；以前的优先拿存档，没有就用现在的数据补一份。
    打卡没开就没有打卡表，返回 None。
    """
    if kind == "打卡" and not store.checkin_settings().enabled:
        return None
    if day < date.today():
        p = archive_path(kind, day)
        if p.is_file():
            return p
    out_dir = DATA / store.bulletin_settings().out_dir
    stamp = datetime.now().strftime("%H%M%S")
    return _BUILDERS[kind](store, camp, day, out_dir / f"{kind}_{day:%Y-%m-%d}_{stamp}.xlsx")


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
