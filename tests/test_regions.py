"""
多群第 2 块：四张名册 + 支局长名单导入、按人分类（城区 / 农村 × 活动 / 非活动 / 未识别）、
晒单按人记（区域 / 类别 / 落地支局）、区域视图互不混。
"""
import io
import json

from _harness import FakePlatform, check, done, eq, run, setup

tmp = setup()

import openpyxl  # noqa: E402

from botcore import regions  # noqa: E402
from botcore.config import ConfigStore  # noqa: E402
from botcore.message import InboundMessage  # noqa: E402
from botcore.pipeline import Pipeline  # noqa: E402


def xlsx(rows: list[list], title="分组") -> bytes:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = title
    for r in rows:
        ws.append(r)
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


city = json.loads((tmp / "roster.json").read_text(encoding="utf-8"))["people"]
CITY_A = next(iter(city))                      # 城区活动名册里第一个人
CITY_TEAM = city[CITY_A]["team"]
RURAL = [("农甲", "1组", "赶水战队"), ("农乙", "1组", "赶水战队"), ("农丙", "2组", "三江战队"), ("农丁", "2组", "三江战队")]
FILES = {
    "rural_act": (xlsx([["姓名", "分组", "战区"], *[list(r) for r in RURAL]]), "农村活动.xlsx"),
    "city_branch": (xlsx([["姓名", "支局", "小区营销"], *[[n, city[n]["team"].replace("战队", "支局"), ""] for n in city],
                          ["城非活动", "古南支局", "是"]]), "城区支局.xlsx"),
    "rural_branch": (xlsx([["姓名", "支局"], ["农甲", "赶水支局"], ["农乙", "赶水支局"], ["农丙", "三江支局"],
                           ["农丁", "三江支局"], ["农非活动", "赶水支局"]]), "农村支局.xlsx"),
    "chiefs": (xlsx([["类别", "支局", "支局长"], ["一类", "通惠", "翁艺丹"], ["", "", "罗江耀"], ["二类", "赶水", "王超"]],
                    "点检"), "支局长.xlsx"),
}
(tmp / "userid_map.json").write_text(json.dumps({
    "u_city": CITY_A, "u_rural": "农丙", "u_other": "城非活动", "u_rother": "农非活动"}, ensure_ascii=False),
    encoding="utf-8")

store = ConfigStore()
ADMIN = store.backup_settings().admins[0]
plat = FakePlatform(files=FILES)
p = Pipeline(plat, store)
_n = [0]


def send(text="", chat="p2p", sender=ADMIN, files=None) -> str:
    _n[0] += 1
    before = len(plat.replies)
    p._process(InboundMessage(msg_id=f"g{_n[0]}", chat_id=sender if chat == "p2p" else chat, chat_type="p2p" if chat == "p2p" else "group",
                              text=text, sender_id=sender, files=files or [], mentions_bot=True), 0.0)
    return plat.replies[-1] if len(plat.replies) > before else ""


def order(no: str, branch="", collector="") -> str:
    return (f"支局：{branch or '新盛支局'}\n业务类型:新增\n办理套餐：199\n业务号码:{no}\n价值积分:199\n"
            f"是否加载云盒：是\n揽收人:{collector}\n")


def test_multi_on():
    check(regions.multi_enabled(store), "config 里配了农村群 / 攻坚群 → 多群模式")


def test_import():
    for cmd, key in (("农村活动名册", "rural_act"), ("城区支局名册", "city_branch"),
                     ("农村支局名册", "rural_branch"), ("支局长名单", "chiefs")):
        check("2 分钟内" in send(cmd), f"先说「{cmd}」")
        r = send(files=[(key, "")])
        check(r.startswith(f"{cmd}已更新"), f"{cmd} 导入：{r}")
    eq(len(p.rosters.activity["农村"].people), 4, "农村活动名册 4 人")
    eq(p.rosters.branch["城区"].people["城非活动"], {"branch": "古南", "community": True}, "城区支局名册：支局简称 + 小区标记")
    eq([c["name"] for c in p.rosters.chiefs.chiefs], ["翁艺丹", "罗江耀", "王超"], "支局长名单")
    eq(p.rosters.chiefs.of("罗江耀"), {"class": "一类", "branch": "通惠", "name": "罗江耀"}, "合并单元格：类别、支局按同上")
    check("只能管理员" in send("农村活动名册", sender="YangTong"), "非管理员不能导")
    check("单聊" in send("农村活动名册", chat="g_hq"), "群里导提示单聊")


def test_identify():
    rs = p.rosters
    i = rs.identify_name(CITY_A)
    eq((i.region, i.role, i.team), ("城区", "活动", CITY_TEAM), "城区活动人员")
    i = rs.identify_name("农丙")
    eq((i.region, i.role, i.team, i.branch), ("农村", "活动", "三江战队", "三江"), "农村活动人员")
    i = rs.identify_name("城非活动")
    eq((i.region, i.role, i.branch, i.community), ("城区", "非活动", "古南", True), "城区非活动（小区营销）")
    i = rs.identify_name("农非活动")
    eq((i.region, i.role), ("农村", "非活动"), "农村非活动")
    eq(rs.identify_name("路人甲").role, "未识别", "名册外的人")
    eq(rs.identify_sender("u_rural").name, "农丙", "账号按 userid_map 认人")
    eq(rs.identify_members("农甲，农乙").group, "农甲、农乙", "打卡第 2 步：按水印小组成员找组（小组记成成员串）")


def test_orders():
    camp = p.campaign()
    before = len(camp.orders)
    send(order("13900000001"), chat="g_city", sender="u_city")
    send(order("13900000002", "三江支局"), chat="g_hq", sender="u_rural")      # 农村活动人员在攻坚群晒
    send(order("13900000003", "古南支局"), chat="g_city", sender="u_other")    # 非活动人员在活动群晒
    send(order("13900000004", "东溪支局"), chat="g_hq", sender="u_nobody")     # 认不出
    got = camp.orders[before:]
    eq(len(got), 4, "4 单都记（一单只记一次）")
    eq([(o.region, o.role) for o in got],
       [("城区", "活动"), ("农村", "活动"), ("城区", "非活动"), ("", "未识别")], "按人分类")
    eq([o.branch for o in got], ["新盛", "三江", "古南", "东溪"], "落地支局 = 晒单里写的支局")
    eq(got[2].community, True, "小区营销人员的单带上 ×1.1 标记")
    check(got[3].needs_review and "都不在名册" in got[3].note, "认不出的单标黄")
    vc, vr = camp.view("城区"), camp.view("农村")
    check(got[0] in vc.orders and got[1] not in vc.orders, "城区视图只有城区活动人员的单")
    check(got[1] in vr.orders and got[0] not in vr.orders, "农村视图只有农村活动人员的单")
    check(got[2] not in vc.orders, "非活动人员的单不进城区活动视图")
    check(got[3] not in vc.orders and got[3] not in vr.orders, "认不出的单不进任何活动视图")
    eq(vr.roster is p.rosters.activity["农村"], True, "农村视图用农村活动名册出表")
    groups = {g.members for g in vr.group_stats()}
    check(any("农丙" in m for m in groups), "农村视图的小组来自农村活动名册")
    n = len(camp.orders)
    vr.refresh_roster()
    vr._save()
    eq(len(camp.orders), n, "视图只读，不改主台账")


def test_collector_fallback():
    camp = p.campaign()
    before = len(camp.orders)
    txt = order("13900000005").replace("揽收人:\n", "揽收人:农甲\n")
    send(txt, chat="g_rural", sender="u_nobody2")
    o = camp.orders[before]
    eq((o.region, o.role, o.team), ("农村", "活动", "赶水战队"), "晒单人认不出 → 按揽收人归")
    check("按揽收人归组" in o.note, "备注说明按揽收人归")


def test_branch_by_collector():
    """09-29 用户定：总表支局按揽收人；揽收人没写 / 查不到按晒单写的支局。换名册重新归组也不丢。"""
    camp = p.campaign()
    before = len(camp.orders)
    send(order("13900000011", "新盛支局", "城非活动"), chat="g_city", sender="u_city")   # 揽收人是古南的
    send(order("13900000012", "东溪支局", "农丙"), chat="g_hq", sender="u_rural")         # 揽收人是三江的
    send(order("13900000013", "东溪支局", "路人乙"), chat="g_hq", sender="u_rural")       # 揽收人查不到
    got = camp.orders[before:]
    eq([o.branch for o in got], ["古南", "三江", "东溪"], "总表支局 = 揽收人的支局；查不到按晒单写的")
    eq([o.written_branch for o in got], ["新盛", "东溪", "东溪"], "晒单写的支局原样留着")
    eq((got[0].region, got[0].role), ("城区", "活动"), "活动群归属仍按晒单人")
    camp.reassign()
    eq([o.branch for o in got], ["古南", "三江", "东溪"], "换名册重新归组后不变")


run(test_multi_on)
run(test_import)
run(test_identify)
run(test_orders)
run(test_branch_by_collector)
run(test_collector_fallback)
done()
