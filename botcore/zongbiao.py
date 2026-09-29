"""
攻坚群支局总表（09-29 国庆版）：按用户给的模板「2026年各支局国庆节营销活动通报模板」。

    分类 | 支局 | 当日（五项各 目标 / 完成值 / 进度 + 综合完成进度）| 累计（同左）| 同类排名 | PK激励
    末行合计，下面一行宽带折算注释

- 所有群里晒的单都算（按人记的主台账），算给【落地支局】（晒单里写的「支局」）；跨区域 6:4 暂不做
- 一单五项由 kpi.compute 算；发单人是小区 / 场镇营销人员的，新增宽带、中高套 ×1.1
- 同类排名按【累计】综合完成进度；PK激励按当前名次，综合 >100% 还输的标「（分公司出资）」
- 合计：目标、完成值各自加总，进度用加总的数算
- 目标（附件一）在 config.yaml 的 guoqing.targets；没配的格子显示「-」
- 当日目标：guoqing.daily_targets 配了用配的，没配 = 累计目标 ÷ 活动天数【待领导】
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

from . import kpi

log = logging.getLogger("botcore.zongbiao")

KEYS = ("broadband", "high", "giga", "pack", "card")
LABELS = {"broadband": "新增宽带（25%）", "high": "中高套（30%）", "giga": "千兆/500兆（20%）",
          "pack": "套包（15%）", "card": "单号卡（10%）"}


@dataclass
class GuoqingSettings:
    title: str = "10.1-10.7国庆节营销活动通报"
    classes: dict = field(default_factory=lambda: {
        "一类": ["通惠", "古南", "文龙", "万盛", "东林"],
        "二类": ["打通", "赶水", "东溪", "青年", "新盛", "郭扶"],
        "三类": ["三江", "南桐", "安稳", "永新", "隆盛", "三角"],
    })
    targets: dict = field(default_factory=dict)          # 支局 -> {broadband, high, giga, pack, card}（累计）
    daily_targets: dict = field(default_factory=dict)    # 支局 -> 同上；没配 = 累计 ÷ 活动天数
    pk: dict = field(default_factory=lambda: {
        "一类": [2000, 1500, 0, -1500, -2000],
        "二类": [1500, 1200, 1000, -1000, -1200, -1500],
        "三类": [1200, 800, 500, -500, -800, -1200],
    })
    note: str = ("注：明厨亮灶、780包年宽带按3:1计宽带量，40元及以下列收宽带、酒店宽带、390包年宽带、"
                 "短期宽带按5:1计宽带量，明星机赠送宽带不计量。")

    @classmethod
    def from_dict(cls, raw: dict | None) -> "GuoqingSettings":
        raw = dict(raw or {})
        return cls(**{k: v for k, v in raw.items() if k in cls.__dataclass_fields__ and v is not None})


@dataclass
class Row:
    cls: str
    branch: str
    day_target: dict
    day_done: dict
    cum_target: dict
    cum_done: dict
    day_prog: dict = field(default_factory=dict)
    cum_prog: dict = field(default_factory=dict)
    day_comp: float = 0.0
    cum_comp: float = 0.0
    rank: int = 0
    pk: str = ""


def _zero() -> dict:
    return {k: 0.0 for k in KEYS}


def _period(now: datetime) -> tuple[date, date, int]:
    """(活动开始, 活动结束, 天数)：用当前活动；没设活动就当今天一天。"""
    from . import activity
    act = activity.load()
    if act is not None:
        return act.start_date, act.end_date, (act.end_date - act.start_date).days + 1
    return now.date(), now.date(), 1


def order_kpi(o) -> dict:
    """一单的五项（小区 / 场镇营销人员的新增宽带、中高套 ×1.1）。"""
    k = kpi.compute(o.kind, o.fee, getattr(o, "loaded", ""), o.card or 1,
                    getattr(o, "up_giga", ""), getattr(o, "up_2000", ""))
    d = k.as_dict()
    if getattr(o, "community", False):
        d["broadband"] *= 1.1
        d["high"] *= 1.1
    return d


def _composite(done: dict, target: dict) -> tuple[dict, float]:
    prog = kpi.progress(done, target)
    return prog, kpi.composite(prog)


def build_table(cfg: GuoqingSettings, orders: list, now: datetime) -> dict:
    """算整张表。orders = 主台账（所有人的单）。返回 {rows, total, title, unassigned, day, start}。"""
    start, end, days = _period(now)
    today = now.date()
    day_lo = today.strftime("%Y-%m-%d")
    cum_lo = start.strftime("%Y-%m-%d")
    hi = now.strftime("%Y-%m-%d %H:%M:%S")
    branches = [b for bs in cfg.classes.values() for b in bs]
    day_done = {b: _zero() for b in branches}
    cum_done = {b: _zero() for b in branches}
    unassigned: dict[str, int] = {}
    for o in orders:
        if not o.at or o.at > hi or o.at[:10] < cum_lo or o.at[:10] > end.strftime("%Y-%m-%d"):
            continue
        b = (getattr(o, "branch", "") or "").strip()
        d = order_kpi(o)
        if b not in cum_done:
            if any(d.values()):
                unassigned[b or "（没写支局）"] = unassigned.get(b or "（没写支局）", 0) + 1
            continue
        for k in KEYS:
            cum_done[b][k] += d[k]
            if o.at[:10] == day_lo:
                day_done[b][k] += d[k]

    rows: list[Row] = []
    for cls, bs in cfg.classes.items():
        group: list[Row] = []
        for b in bs:
            cum_t = {k: float((cfg.targets.get(b) or {}).get(k) or 0) for k in KEYS}
            if cfg.daily_targets.get(b):
                day_t = {k: float(cfg.daily_targets[b].get(k) or 0) for k in KEYS}
            else:
                day_t = {k: cum_t[k] / days for k in KEYS}
            r = Row(cls, b, day_t, day_done[b], cum_t, cum_done[b])
            r.day_prog, r.day_comp = _composite(r.day_done, r.day_target)
            r.cum_prog, r.cum_comp = _composite(r.cum_done, r.cum_target)
            group.append(r)
        # 同类排名（09-29 用户定）：按累计综合进度，一样的并列同名次、名次连着排
        #   （只有一个支局有业绩：它第 1，其余并列第 2；两个有业绩：第 1、第 2，其余并列第 3）；一个有业绩的都没有就都不排。
        # PK：这一类里有业绩的支局数 ≥ 支局数 − 1 才开始，之前 PK 一栏都是「-」
        scoring = sum(1 for r in group if r.cum_comp > 0)
        pk_on = scoring > 0 and scoring >= len(group) - 1
        levels = sorted({round(r.cum_comp, 6) for r in group}, reverse=True)
        amounts = list(cfg.pk.get(cls) or [])
        for r in group:
            if not scoring:
                r.rank, r.pk = 0, "-"
                continue
            r.rank = levels.index(round(r.cum_comp, 6)) + 1
            if not pk_on:
                r.pk = "-"
                continue
            # PK 金额按「前面有几个支局比它强」取档（并列的拿它们里最好的那档）：名次连着排只是显示，
            # 不能让没业绩的排在一串并列后面拿到靠前的档
            pos = sum(1 for x in group if round(x.cum_comp, 6) > round(r.cum_comp, 6))
            amt = amounts[pos] if pos < len(amounts) else 0
            r.pk = f"{amt:+d}" if amt else "0"
            if amt < 0 and r.cum_comp > 1.0:
                r.pk += "（分公司出资）"
        rows.extend(group)

    def total(attr: str) -> dict:
        out = _zero()
        for r in rows:
            for k in KEYS:
                out[k] += getattr(r, attr)[k]
        return out

    tot = Row("合计", "", total("day_target"), total("day_done"), total("cum_target"), total("cum_done"))
    tot.day_prog, tot.day_comp = _composite(tot.day_done, tot.day_target)
    tot.cum_prog, tot.cum_comp = _composite(tot.cum_done, tot.cum_target)
    return {"rows": rows, "total": tot, "title": cfg.title, "note": cfg.note, "unassigned": unassigned,
            "now": now, "has_targets": any(any(v.values()) for v in (tot.cum_target,))}


# ---------------- 格式 ----------------

def _num(v: float) -> str:
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _pct(v: float, has_target: bool) -> str:
    return f"{v * 100:.1f}%" if has_target else "-"


def row_cells(r: Row) -> list[str]:
    """一行 34 格（分类、支局之后）：当日 16 + 累计 16 + 排名 + PK。"""
    out = []
    for tgt, done, prog, comp in ((r.day_target, r.day_done, r.day_prog, r.day_comp),
                                  (r.cum_target, r.cum_done, r.cum_prog, r.cum_comp)):
        any_t = any(tgt.values())
        for k in KEYS:
            out += [_num(tgt[k]) if tgt[k] else "-", _num(done[k]), _pct(prog[k], bool(tgt[k]))]
        out.append(_pct(comp, any_t))
    out += [str(r.rank) if r.rank else "-", r.pk]
    return out


# ---------------- 图片 ----------------

def render_png(table: dict) -> bytes:
    from PIL import Image, ImageDraw
    from .bulletin import _font
    f_title, f_head, f_cell = _font(26, True), _font(15, True), _font(15)
    w_cls, w_br, w_c, w_comp, w_rank, w_pk = 60, 64, 62, 86, 60, 150
    block = 15 * w_c + w_comp
    width = w_cls + w_br + 2 * block + w_rank + w_pk
    h_title, h_head, h_row = 52, 34, 32
    rows = table["rows"]
    n = len(rows)
    height = h_title + 3 * h_head + (n + 1) * h_row + 44
    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)
    line = (90, 90, 90)
    head_bg = (221, 235, 247)

    def box(x0, y0, x1, y1, text="", font=f_cell, fill=None, color=(20, 20, 20)):
        d.rectangle([x0, y0, x1, y1], fill=fill, outline=line)
        if text:
            tw = d.textlength(text, font=font)
            size = font.size
            while tw > (x1 - x0) - 6 and size > 10:        # 放不下就缩小字（「1400.0%」这种）
                size -= 1
                font = _font(size, False)
                tw = d.textlength(text, font=font)
            d.text((x0 + (x1 - x0 - tw) / 2, y0 + (y1 - y0 - font.size) / 2 - 1), text, font=font, fill=color)

    now = table["now"]
    box(0, 0, width - 1, h_title, f"{table['title']}（{now.month}.{now.day} 截至 {now:%H:%M}，按晒单统计，以竣工为准）",
        f_title)
    y = h_title
    x = 0
    box(x, y, x + w_cls, y + 3 * h_head, "分类", f_head, head_bg)
    x += w_cls
    box(x, y, x + w_br, y + 3 * h_head, "支局", f_head, head_bg)
    x += w_br
    for part in ("当日", "累计"):
        box(x, y, x + block, y + h_head, part, f_head, head_bg)
        xx = x
        for k in KEYS:
            box(xx, y + h_head, xx + 3 * w_c, y + 2 * h_head, LABELS[k], f_head, head_bg)
            for i, sub in enumerate(("目标", "完成值", "进度")):
                box(xx + i * w_c, y + 2 * h_head, xx + (i + 1) * w_c, y + 3 * h_head, sub, f_head, head_bg)
            xx += 3 * w_c
        box(xx, y + h_head, xx + w_comp, y + 3 * h_head, "综合完成进度", _font(13, True), head_bg)
        x += block
    box(x, y, x + w_rank, y + 3 * h_head, "同类排名", _font(13, True), head_bg)
    x += w_rank
    box(x, y, x + w_pk, y + 3 * h_head, "PK激励", f_head, head_bg)
    y += 3 * h_head

    # 分类列合并
    i = 0
    while i < n:
        j = i
        while j < n and rows[j].cls == rows[i].cls:
            j += 1
        box(0, y + i * h_row, w_cls, y + j * h_row, rows[i].cls, f_head)
        i = j
    widths = [w_c] * 15 + [w_comp] + [w_c] * 15 + [w_comp] + [w_rank, w_pk]
    for r_i, r in enumerate(rows + [table["total"]]):
        yy = y + r_i * h_row
        x = w_cls
        if r is table["total"]:
            box(0, yy, w_cls + w_br, yy + h_row, "合计：", f_head)
        else:
            box(x, yy, x + w_br, yy + h_row, r.branch, f_head)
        x = w_cls + w_br
        for cell, w in zip(row_cells(r), widths):
            fill = None
            color = (20, 20, 20)
            if cell.startswith("-1") or cell.startswith("-2") or cell.startswith("-5") or cell.startswith("-8"):
                color = (192, 0, 0)
            elif cell.startswith("+"):
                color = (0, 128, 0)
            box(x, yy, x + w, yy + h_row, cell, f_cell, fill, color)
            x += w
    yy = y + (n + 1) * h_row
    d.rectangle([0, yy, width - 1, height - 1], outline=line)
    d.text((10, yy + 12), table["note"], font=_font(14), fill=(60, 60, 60))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# ---------------- Excel ----------------

def write_sheet(ws, table: dict) -> None:
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    thin = Side(style="thin", color="999999")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    center = Alignment(horizontal="center", vertical="center", wrap_text=True)
    head = PatternFill("solid", fgColor="DDEBF7")
    now = table["now"]
    ws.cell(1, 1, f"{table['title']}（{now.month}.{now.day} 截至 {now:%H:%M}，按晒单统计，以竣工为准）")
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=36)
    ws.cell(2, 1, "分类")
    ws.merge_cells("A2:A4")
    ws.cell(2, 2, "支局")
    ws.merge_cells("B2:B4")
    col = 3
    for part in ("当日", "累计"):
        ws.cell(2, col, part)
        ws.merge_cells(start_row=2, start_column=col, end_row=2, end_column=col + 15)
        c = col
        for k in KEYS:
            ws.cell(3, c, LABELS[k])
            ws.merge_cells(start_row=3, start_column=c, end_row=3, end_column=c + 2)
            for i, sub in enumerate(("目标", "完成值", "进度")):
                ws.cell(4, c + i, sub)
            c += 3
        ws.cell(3, c, "综合完成进度")
        ws.merge_cells(start_row=3, start_column=c, end_row=4, end_column=c)
        col += 16
    ws.cell(2, 35, "同类排名")
    ws.merge_cells("AI2:AI4")
    ws.cell(2, 36, "PK激励")
    ws.merge_cells("AJ2:AJ4")
    rows = table["rows"]
    r0 = 5
    for i, r in enumerate(rows):
        ws.cell(r0 + i, 2, r.branch)
        for j, v in enumerate(row_cells(r)):
            ws.cell(r0 + i, 3 + j, v)
    i = 0
    while i < len(rows):
        j = i
        while j < len(rows) and rows[j].cls == rows[i].cls:
            j += 1
        ws.cell(r0 + i, 1, rows[i].cls)
        if j - i > 1:
            ws.merge_cells(start_row=r0 + i, start_column=1, end_row=r0 + j - 1, end_column=1)
        i = j
    rt = r0 + len(rows)
    ws.cell(rt, 1, "合计：")
    ws.merge_cells(start_row=rt, start_column=1, end_row=rt, end_column=2)
    for j, v in enumerate(row_cells(table["total"])):
        ws.cell(rt, 3 + j, v)
    ws.cell(rt + 1, 1, table["note"])
    ws.merge_cells(start_row=rt + 1, start_column=1, end_row=rt + 1, end_column=36)
    for row in ws.iter_rows(min_row=1, max_row=rt, max_col=36):
        for c in row:
            c.alignment = center
            c.border = border
            if 2 <= c.row <= 4:
                c.fill = head
                c.font = Font(bold=True)
    ws.cell(1, 1).font = Font(bold=True, size=14)
    ws.column_dimensions["A"].width = 7
    ws.column_dimensions["B"].width = 8
    for c in range(3, 37):
        ws.column_dimensions[get_column_letter(c)].width = 9
    ws.column_dimensions["AJ"].width = 18


def build_excel(store, camp, day: date, out: Path) -> Path:
    """存档 / 单聊要表格：那天结束时（今天就是现在）的总表。"""
    from openpyxl import Workbook
    now = datetime.now()
    at = now if day >= now.date() else datetime.combine(day, dtime(23, 59, 59))
    table = build_table(store.guoqing_settings(), list(camp.orders), at)
    wb = Workbook()
    write_sheet(wb.active, table)
    wb.active.title = "支局总表"
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
    return out


def caption(table: dict) -> str:
    now = table["now"]
    lines = [f"{now.month}.{now.day} 国庆营销活动支局通报（截至 {now:%H:%M}）"]
    for cls in dict.fromkeys(r.cls for r in table["rows"]):
        top = [r for r in table["rows"] if r.cls == cls and r.rank == 1]
        if top:
            lines.append(f"{cls}第一：{top[0].branch}（累计综合 {top[0].cum_comp * 100:.1f}%）")
    if not table["has_targets"]:
        lines.append("（各支局目标还没配，进度暂不计算）")
    if table["unassigned"]:
        lines.append("另有支局认不出的单：" + "、".join(f"{k} {v} 单" for k, v in table["unassigned"].items()))
    return "\n".join(lines)
