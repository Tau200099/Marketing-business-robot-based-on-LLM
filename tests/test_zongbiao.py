"""
多群第 5 块：攻坚群支局总表 —— 按落地支局汇总、小区营销 ×1.1、当日 / 累计、认不出的支局单列、
同类排名、PK激励（综合 >100% 还输的标「分公司出资」）、合计、出图、Excel、发到攻坚群。目标是编的。
"""
import json
from datetime import date, datetime, timedelta

from _harness import FakePlatform, check, done, eq, run, setup

tmp = setup()

from botcore import zongbiao  # noqa: E402
from botcore.campaign import Order  # noqa: E402
from botcore.config import ConfigStore  # noqa: E402
from botcore.zongbiao import GuoqingSettings, build_table  # noqa: E402

TODAY = date.today()
Y = TODAY - timedelta(days=1)
(tmp / "activity.json").write_text(json.dumps({"title": "国庆营销", "start": Y.isoformat(),
                                               "end": (TODAY + timedelta(days=5)).isoformat()}), encoding="utf-8")
NOW = datetime.combine(TODAY, datetime.min.time()).replace(hour=15, minute=30)


def o(day, branch, kind, fee, loaded="", community=False, card=0):
    return Order(at=f"{day} 10:00:00", kind=kind, fee=fee, loaded=loaded, branch=branch, community=community,
                 card=card)


ORDERS = [
    o(TODAY, "通惠", "新增", "199+10", "云盒"),
    o(TODAY, "通惠", "新增", "199+10", "云盒"),
    o(TODAY, "通惠", "单C", "19山城卡", card=1),
    o(TODAY, "通惠", "新增", "139", community=True),       # 小区营销人员：宽带、中高套 ×1.1
    o(Y, "古南", "新增", "199"),                            # 昨天的：进累计不进当日
    o(TODAY, "文龙", "新增", "199"),
    o(TODAY, "万盛", "新增", "199"),
    o(TODAY, "东林", "新增", "199"),
    o(TODAY, "某某", "新增", "199"),                        # 支局认不出
    o(TODAY - timedelta(days=5), "通惠", "新增", "199"),    # 活动开始前：不算
]
T10 = {k: 10 for k in zongbiao.KEYS}
T1 = {"broadband": 0.5, "high": 0.5, "giga": 0.5, "pack": 1, "card": 1}   # 一单就超 100%
CFG = GuoqingSettings(targets={"通惠": T10, "古南": T1, "文龙": T1, "万盛": T1, "东林": T1})


def row(table, b):
    return next(r for r in table["rows"] if r.branch == b)


def test_sum_and_progress():
    t = build_table(CFG, ORDERS, NOW)
    th = row(t, "通惠")
    eq({k: round(v, 3) for k, v in th.cum_done.items()},
       {"broadband": 3.1, "high": 3.1, "giga": 3.0, "pack": 2.02, "card": 1.0}, "通惠累计：按落地支局加，小区单 ×1.1")
    eq(round(th.cum_comp * 100, 2), 27.08, "通惠综合 = 31%×25% + 31%×30% + 30%×20% + 20.2%×15% + 10%×10%")
    gn = row(t, "古南")
    eq(gn.day_done["broadband"], 0.0, "古南昨天的单不进当日")
    eq(gn.cum_done["broadband"], 1.0, "古南昨天的单进累计")
    eq(round(th.day_target["broadband"], 3), round(10 / 7, 3), "当日目标没配 = 累计目标 ÷ 活动天数（7 天）【待领导】")
    eq(t["unassigned"], {"某某": 1}, "认不出的支局单列出来")


def test_rank_and_pk():
    t = build_table(CFG, ORDERS, NOW)
    one = sorted((r for r in t["rows"] if r.cls == "一类"), key=lambda r: r.rank)
    eq(one[-1].branch, "通惠", "通惠综合最低，一类第 5")
    eq(one[-1].pk, "-2000", "第 5 名 −2000（综合 27% 不到 100%，不是分公司出资）")
    eq(one[0].pk, "+2000", "第 1 名 +2000")
    eq(one[2].pk, "0", "一类第 3 名持平")
    eq(one[3].pk, "-1500（分公司出资）", "第 4 名综合 >100% 还输了：分公司出资")
    two = [r for r in t["rows"] if r.cls == "二类"]
    check(all(r.rank == 0 and r.pk == "-" for r in two), "二类一单都没有：不排名、PK 写「-」")


def test_total():
    t = build_table(CFG, ORDERS, NOW)
    tot = t["total"]
    eq(tot.cum_target["broadband"], 12.0, "合计目标 = 各支局加总")
    eq(round(tot.cum_done["broadband"], 2), 7.1, "合计完成 = 各支局加总（认不出的支局不算）")
    eq(round(tot.cum_prog["broadband"], 4), round(7.1 / 12, 4), "合计进度用加总的数算")


def test_outputs():
    t = build_table(CFG, ORDERS, NOW)
    png = zongbiao.render_png(t)
    check(png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 20000, f"出图（{len(png)} 字节）")
    from PIL import Image
    import io
    w, h = Image.open(io.BytesIO(png)).size
    check(w > 2000 and h > 600, f"图够大、看得清：{w}×{h}")
    cells = zongbiao.row_cells(row(t, "通惠"))
    eq(len(cells), 34, "每行 34 格（当日 16 + 累计 16 + 排名 + PK），加上分类、支局 = 模板的 36 列")
    import openpyxl
    wb = openpyxl.Workbook()
    zongbiao.write_sheet(wb.active, t)
    ws = wb.active
    eq(ws.cell(2, 3).value, "当日", "Excel 表头：当日")
    eq(ws.cell(2, 19).value, "累计", "Excel 表头：累计")
    eq(ws.cell(5, 2).value, "通惠", "第一行是通惠")
    eq(ws.cell(5 + 17, 1).value, "合计：", "17 个支局后是合计")
    cap = zongbiao.caption(t)
    check("一类第一" in cap and "某某 1 单" in cap, f"文字说明：{cap}")
    empty = build_table(GuoqingSettings(), ORDERS, NOW)
    check("目标还没配" in zongbiao.caption(empty), "目标没配时说清楚")
    eq(zongbiao.row_cells(row(empty, "通惠"))[2], "-", "没目标的进度格写「-」")


def test_publish():
    from botcore.hq import HqPublisher
    store = ConfigStore()

    class Camp:
        orders = ORDERS

        def refresh_roster(self):
            pass

    plat = FakePlatform()
    hq = HqPublisher(plat, store, lambda: Camp())
    r = hq.publish(("g_hq",), "测试", NOW)
    eq(r["ok"], ["g_hq"], "攻坚群发成功")
    check(plat.sent_texts and plat.sent_texts[0][0] == "g_hq", "发了文字说明（再发表格图）")


run(test_sum_and_progress)
run(test_rank_and_pk)
run(test_total)
run(test_outputs)
run(test_publish)
done()
