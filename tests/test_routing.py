"""
多群第 3 块：各群发各的 —— 活动群晒单通报 / 打卡通报按区域、攻坚群走总表钩子、跟单通报关掉、
群里喊「通报」「打卡通报」按群、表情包只发活动群、战报不在攻坚群播。
"""
import io
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from _harness import FakePlatform, check, done, eq, run, setup

tmp = setup()

import openpyxl  # noqa: E402

from botcore import regions  # noqa: E402
from botcore.automation import StickerAutomation  # noqa: E402
from botcore.bulletin import Bulletin  # noqa: E402
from botcore.checkin import Checkin, CheckinStore  # noqa: E402
from botcore.checkin_bulletin import CheckinBulletin  # noqa: E402
from botcore.config import ConfigStore  # noqa: E402
from botcore.message import InboundMessage  # noqa: E402
from botcore.paths import DATA  # noqa: E402
from botcore.pipeline import Pipeline  # noqa: E402

store = ConfigStore()
CITY = regions.role_chats(store, "city")[0]
RURAL = regions.role_chats(store, "rural")[0]
HQ = regions.role_chats(store, "hq")[0]

# 农村活动名册（两个人），城区用现有那张
city = json.loads((tmp / "roster.json").read_text(encoding="utf-8"))["people"]
CITY_A = next(iter(city))
(tmp / "roster_rural.json").write_text(json.dumps({"teams": ["赶水战队"], "people": {
    "农甲": {"group": "农甲、农乙", "team": "赶水战队"}, "农乙": {"group": "农甲、农乙", "team": "赶水战队"}}},
    ensure_ascii=False), encoding="utf-8")
(tmp / "userid_map.json").write_text(json.dumps({"u_city": CITY_A, "u_rural": "农甲", "u_other": "城非活动"},
                                                ensure_ascii=False), encoding="utf-8")
(tmp / "branch_city.json").write_text(json.dumps({"people": {"城非活动": {"branch": "东林", "community": False}}},
                                                 ensure_ascii=False), encoding="utf-8")

plat = FakePlatform()
p = Pipeline(plat, store)
pool = ThreadPoolExecutor(2)
bul = Bulletin(plat, store, p.campaign, pool)
hq_calls = []
bul.hq_publish = lambda chats, reason: (hq_calls.append((chats, reason)) or {"ok": list(chats), "failed": []})
chief_calls = []
bul.hq_chiefs = lambda chats, reason: (chief_calls.append((chats, reason)) or {"ok": list(chats), "failed": []})
bul.hq_chief_times = lambda: ["11:50", "18:00", "21:30"]
ckb = CheckinBulletin(plat, store, p.campaign, pool)
ck_hq = []
ckb.hq_command = lambda chat: ck_hq.append(chat)


def order(no):
    return f"支局：新盛支局\n业务类型:新增\n办理套餐：199\n业务号码:{no}\n价值积分:199\n揽收人:\n"


def post(text, chat, sender):
    p._process(InboundMessage(msg_id=f"m{len(plat.replies)}{sender}{no[0]}", chat_id=chat, chat_type="group",
                              text=text, sender_id=sender, mentions_bot=True), 0.0)
    no[0] += 1


no = [0]


def test_bulletins_per_region():
    post(order("13911110001"), CITY, "u_city")
    post(order("13911110002"), HQ, "u_rural")       # 农村活动人员在攻坚群晒
    plat.sent_texts.clear()
    r = bul.publish_all((), "测试定点")
    sent = {c: t for c, t in plat.sent_texts}
    check(CITY in sent and RURAL in sent, "定点：城区群、农村群各收一份")
    check(HQ not in sent, "攻坚群不收活动通报")
    eq(len(hq_calls), 1, "攻坚群走总表钩子")
    eq(sorted(r["ok"]), sorted([CITY, RURAL, HQ]), "三个群都算发成功")
    tc, _png, _x = bul.build(region="城区")
    tr, _png, _x = bul.build(region="农村")
    rural_team = "赶水"
    check(rural_team not in tc, "城区通报里没有农村战队")
    check(rural_team in tr, f"农村通报里有农村战队：{tr[:80]}")
    vr = p.campaign().view("农村")
    eq(len(vr.orders), 1, "农村活动人员在攻坚群晒的单算进农村（按人）")


def test_commands_and_follow():
    plat.sent_texts.clear()
    hq_calls.clear()
    bul._state.last_command_at = 0
    bul.request(RURAL, "命令「通报」")
    pool.submit(lambda: None).result()
    import time
    time.sleep(0.5)
    eq([c for c, _t in plat.sent_texts], [RURAL], "农村群喊通报：只发农村群")
    bul._state.last_command_at = 0
    bul.request(HQ, "命令「通报」")
    time.sleep(0.5)
    eq(len(hq_calls), 1, "攻坚群喊通报：发总表")
    before = len(plat.sent_texts)
    bul.on_order(CITY)
    time.sleep(0.3)
    eq(len(plat.sent_texts), before, "多群模式跟单通报关掉")


def test_checkin_bulletins():
    s = store.checkin_settings()
    cs = CheckinStore(DATA / s.store_file)
    day = datetime.now().strftime("%Y-%m-%d")
    for i, (region, sender) in enumerate((("城区", "u_city"), ("农村", "u_rural"))):
        cs.add(Checkin(at=f"{day} 10:0{i}", slot="上午", branch="", members="", work="上门", label="现场拦截",
                       color="", source="视觉", photo=f"ph{i}", sender=sender, code=f"code{i}", region=region))
    targets = ckb._targets()
    eq(sorted(r for _c, r in targets), ["农村", "城区"], "打卡通报：城区、农村各一份（攻坚群没有）")
    now = datetime.now().replace(hour=10, minute=30)
    tc, _png, _t = ckb.build(now, "城区")
    tr, _png, _t = ckb.build(now, "农村")
    check("赶水" not in tc and "赶水" in tr, f"农村打卡只进农村通报：城区「{tc[:60]}」农村「{tr[:60]}」")
    ckb._last_command = 0
    handled = ckb.handle_command(InboundMessage(msg_id="x1", chat_id=HQ, chat_type="group", text="打卡通报"))
    check(handled and ck_hq == [HQ], "攻坚群喊打卡通报 → 支局长点检表钩子")


def test_stickers_and_broadcast():
    auto = StickerAutomation(plat, store, lambda: None)
    eq(sorted(auto._target_chats()), sorted([CITY, RURAL]), "表情包只发两个活动群")
    before = len(plat.replies)
    msg = InboundMessage(msg_id="b1", chat_id=HQ, chat_type="group", text="x", sender_id="u_city")
    p._broadcast(msg, {"is_new": True, "broadband_count": 1, "high_count": 1}, "新盛支局")
    eq(len(plat.replies), before, "攻坚群不播战报")
    msg = InboundMessage(msg_id="b2", chat_id=CITY, chat_type="group", text="x", sender_id="u_city")
    p._broadcast(msg, {"is_new": True, "broadband_count": 1, "high_count": 1}, "新盛支局")
    check(len(plat.replies) == before + 1 and "新盛" in plat.replies[-1], "活动群照常播战报")


def test_broadcast_only_activity():
    """09-29：活动群战报只数本群活动人员；非活动人员照常点赞、进总表，提醒去攻坚群晒单。"""
    before = len(plat.replies)
    post(order("13977770001"), CITY, "u_other")
    got = plat.replies[before:]
    check(any("点赞" in x for x in got), f"非活动人员：照常点赞 {got}")
    check(any("不是本次活动人员" in x and "已计入总表" in x and "攻坚群" in x for x in got), f"提醒去攻坚群晒单 {got}")
    check(not any("恭喜" in x for x in got), f"不播战报 {got}")
    o = p.campaign().orders[-1]
    eq((o.role, o.branch), ("非活动", "新盛"), "这单照样进台账（总表按晒单写的支局）")
    before = len(plat.replies)
    post(order("13977770002"), CITY, "u_city")
    got = plat.replies[before:]
    check(any("恭喜" in x for x in got) and not any("不是本次活动人员" in x for x in got), f"活动人员：照常播战报 {got}")
    before = len(plat.replies)
    post(order("13977770003"), CITY, "u_rural")
    got = plat.replies[before:]
    check(not any("恭喜" in x for x in got), f"农村活动人员在城区群晒单：城区群不播战报 {got}")


run(test_bulletins_per_region)
run(test_broadcast_only_activity)
run(test_commands_and_follow)
run(test_checkin_bulletins)


def test_chief_times():
    """09-30 用户：支局长点检表只在 11:50 / 18:00 / 21:30 发；21:30 不是通报定点，单独发一次、只发点检表。"""
    import time as _t
    cfg = store.bulletin_settings()
    assert "21:30" not in cfg.fixed_times
    hq_calls.clear()
    chief_calls.clear()
    bul._check_fixed(cfg, datetime(2026, 10, 1, 21, 31))
    for _ in range(50):
        if chief_calls:
            break
        _t.sleep(0.1)
    eq([r for _c, r in chief_calls], ["定点21:30"], "21:31：发一次点检表")
    eq(hq_calls, [], "21:31：不发总表 / 活动通报")
    bul._check_fixed(cfg, datetime(2026, 10, 1, 21, 35))
    _t.sleep(0.3)
    eq(len(chief_calls), 1, "同一天不重复发")
    bul._check_fixed(cfg, datetime(2026, 10, 1, 21, 45))
    _t.sleep(0.3)
    eq(len(chief_calls), 1, "过了 10 分钟窗口不补发")

run(test_stickers_and_broadcast)
run(test_chief_times)
done()
