"""
查数分区（09-29 改 1、2）：群里问业绩按群分区（城区 / 农村 / 攻坚群全部）；TeleAgent 工具带区域、导名册说哪张、总表和点检表工具。
"""
import io
import json

from _harness import FakePlatform, check, done, eq, run, setup

tmp = setup()

import openpyxl  # noqa: E402

from botcore import regions  # noqa: E402
from botcore.config import ConfigStore  # noqa: E402
from botcore.ledger_tools import LedgerTools  # noqa: E402
from botcore.message import InboundMessage  # noqa: E402
from botcore.pipeline import Pipeline  # noqa: E402

city = json.loads((tmp / "roster.json").read_text(encoding="utf-8"))["people"]
CITY_A = next(iter(city))
CITY_TEAM = city[CITY_A]["team"]
(tmp / "roster_rural.json").write_text(json.dumps({"teams": ["赶水战队"], "people": {
    "农甲": {"group": "农甲、农乙", "team": "赶水战队"}, "农乙": {"group": "农甲、农乙", "team": "赶水战队"}}},
    ensure_ascii=False), encoding="utf-8")
(tmp / "branch_city.json").write_text(json.dumps({"people": {"城非活动": {"branch": "古南", "community": False}}},
                                                 ensure_ascii=False), encoding="utf-8")
(tmp / "userid_map.json").write_text(json.dumps({"u_city": CITY_A, "u_rural": "农甲", "u_other": "城非活动"},
                                                ensure_ascii=False), encoding="utf-8")
store = ConfigStore()
CITY = regions.role_chats(store, "city")[0]
RURAL = regions.role_chats(store, "rural")[0]
HQ = regions.role_chats(store, "hq")[0]
plat = FakePlatform()
p = Pipeline(plat, store)


def post(no, branch, chat, sender):
    txt = f"支局：{branch}\n业务类型:新增\n办理套餐：199\n业务号码:{no}\n价值积分:199\n揽收人:\n"
    p._process(InboundMessage(msg_id=f"s{no}", chat_id=chat, chat_type="group", text=txt, sender_id=sender,
                              mentions_bot=True), 0.0)


post("13922220001", "新盛支局", CITY, "u_city")
post("13922220002", "赶水支局", HQ, "u_rural")
post("13922220003", "古南支局", CITY, "u_other")


def tools_for(scope):
    p._tls.scope = scope
    try:
        return LedgerTools(p._assistant_campaign, store.campaign_settings)
    finally:
        pass


def test_scope_of_chat():
    eq(regions.scope_of_chat(store, "group", CITY), "城区", "城区群问 → 城区")
    eq(regions.scope_of_chat(store, "group", RURAL), "农村", "农村群问 → 农村")
    eq(regions.scope_of_chat(store, "group", HQ), "全部", "攻坚群问 → 全部")
    eq(regions.scope_of_chat(store, "p2p", "someone"), "全部", "单聊问 → 全部")


def test_assistant_views():
    rural = tools_for("农村").team_stats("today")
    teams = [t["战队"] for t in rural["战队"]]
    check("赶水战队" in teams and CITY_TEAM not in teams, f"农村群：只有农村战队 {teams}")
    gs = next(t for t in rural["战队"] if t["战队"] == "赶水战队")
    eq(gs["单数"], 1, "农村活动人员在攻坚群晒的单算进农村")
    cityr = tools_for("城区").team_stats("today")
    ct = {t["战队"]: t["单数"] for t in cityr["战队"]}
    check("赶水战队" not in ct, "城区群：没有农村战队")
    eq(sum(ct.values()), 1, "城区群：只数城区活动人员的单（非活动人员那单不算）")
    allr = tools_for("全部").team_stats("today")
    br = {t["支局"]: t["单数"] for t in allr["支局"]}
    eq((br.get("新盛"), br.get("赶水"), br.get("古南")), (1, 1, 1), "攻坚群：所有人的单按落地支局")
    check("支局总表" in allr["说明"], "攻坚群的说明指向支局总表")
    ps = tools_for("全部").person_stats("农甲", "today")
    eq(ps["单数"], 1, "按人查：晒单人本人也查得到（不只是揽收人）")
    p._tls.scope = None
    check(p._assistant_campaign() is p.campaign(), "不在问答时用主台账")


def test_mcp_tools():
    import mcp_server as m
    rural = m.team_stats("today", region="农村")
    check(any(t["战队"] == "赶水战队" for t in rural["战队"]), "TeleAgent 农村：有赶水战队")
    allr = m.team_stats("today")
    check("支局" in allr and any(t["支局"] == "古南" for t in allr["支局"]), "TeleAgent 默认全部：按支局")
    eq(m.roster("农甲")["类别"], "活动", "TeleAgent 查人：农甲是农村活动人员")
    eq(m.roster("城非活动")["类别"], "非活动", "TeleAgent 查人：非活动人员")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "分组"
    for r in (["姓名", "分组", "战区"], ["农丙", "1组", "三江战队"], ["农丁", "1组", "三江战队"]):
        ws.append(r)
    path = tmp / "农村活动.xlsx"
    wb.save(path)
    r = m.import_roster(str(path), which="农村活动名册")
    check(r.get("ok") and "2 人" in r["名册"], f"TeleAgent 导农村活动名册：{r}")
    check("error" in m.import_roster(str(path), which="随便"), "which 写错说清楚")
    t = m.guoqing_total("today")
    from pathlib import Path
    check(Path(t["png"]).is_file() and Path(t["excel"]).is_file() and len(t["支局"]) == 17, "TeleAgent 支局总表：17 个支局 + 图 + Excel")
    c = m.chief_checkin("today")
    check(Path(c["png"]).is_file() and Path(c["excel"]).is_file(), "TeleAgent 支局长点检表：图 + Excel")
    ck = m.checkin_stats("today", region="农村")
    check("各小组" in ck, "TeleAgent 打卡统计按区域")


run(test_scope_of_chat)
run(test_assistant_views)
run(test_mcp_tools)
done()
