"""
多群第 4 块：打卡只记活动人员 —— ① 发送人账号 ② 认不出看水印小组成员 ③ 都不行不记，两句回复；
改好信息重发会重新检查；每张有水印时间的照片都过支局长点检钩子（记不记打卡都过）。
"""
import json

from _harness import FakePlatform, check, done, eq, fake_vision, run, setup, skip, wait_pool, watermark_photos

tmp = setup()

from botcore import regions  # noqa: E402
from botcore.config import ConfigStore  # noqa: E402
from botcore.message import InboundMessage  # noqa: E402
from botcore.pipeline import Pipeline  # noqa: E402

PHOTOS = watermark_photos(5)
city = json.loads((tmp / "roster.json").read_text(encoding="utf-8"))["people"]
CITY_A = next(iter(city))
(tmp / "branch_city.json").write_text(json.dumps({"people": {"城非活动": {"branch": "古南", "community": False}}},
                                                 ensure_ascii=False), encoding="utf-8")
UMAP = tmp / "userid_map.json"
UMAP.write_text(json.dumps({"u_city": CITY_A, "u_other": "城非活动"}, ensure_ascii=False), encoding="utf-8")

store = ConfigStore()
plat = FakePlatform(images={f"w{i}": b for i, b in enumerate(PHOTOS)})
p = Pipeline(plat, store)
fake_vision(p.checkin._judge, {})
seen_photos = []
p.checkin.on_photo = lambda blob, wm, photo: seen_photos.append(wm["stamp"])
t = [0]


def send(img, sender):
    t[0] += 1
    before = len(plat.replies)
    p._process(InboundMessage(msg_id=f"k{t[0]}", chat_id=sender, chat_type="p2p", sender_id=sender,
                              image_ids=[img], mentions_bot=True), 0.0)
    wait_pool(p.checkin_pool)
    return plat.replies[before:]


def records():
    return p.checkin.store.records


def test_step1_activity():
    r = send("w0", "u_city")
    check(r and r[-1].startswith("打卡已记"), f"城区活动人员：记上 {r}")
    eq(records()[-1].region, "城区", "记到城区")


def test_step1_non_activity():
    n = len(records())
    r = send("w1", "u_other")
    eq(r[-1] if r else "", "你不在活动名单里，请核实自己账号信息并重新发送", "非活动人员：不记，回这句")
    eq(len(records()), n, "没记")


def test_step2_members():
    n = len(records())
    r = send("w2", "u_nobody")          # 账号认不出，但水印小组成员是城区活动名册里的人（样本都是城区的）
    check(r and r[-1].startswith("打卡已记"), f"账号认不出、水印成员对上：记上 {r}")
    eq(len(records()), n + 1, "记了")


def test_step3_unknown_and_resend():
    real = p.rosters.identify_members
    p.rosters.identify_members = lambda members: regions.Identity()     # 水印成员也对不上
    try:
        n = len(records())
        r = send("w3", "u_nobody2")
        eq(r[-1] if r else "", "无法查询到您的账号姓名信息，请修改账号信息或打卡照人员信息后重新发送",
           "三步都认不出：不记，回这句")
        eq(len(records()), n, "没记")
        # 他改好账号（管理员补了对照）再发同一张 → 重新检查，这次记上
        UMAP.write_text(json.dumps({"u_city": CITY_A, "u_other": "城非活动", "u_nobody2": CITY_A},
                                   ensure_ascii=False), encoding="utf-8")
        import botcore.checkin as ck
        ck._map_cache["mtime"] = None
        r = send("w3", "u_nobody2")
        check(r and r[-1].startswith("打卡已记"), f"改好信息重发：记上 {r}")
    finally:
        p.rosters.identify_members = real


def test_hook_every_photo():
    eq(len(seen_photos), t[0], "每张有水印时间的照片都过支局长点检（非活动、认不出的也过）")


if len(PHOTOS) < 5:
    skip("没有样本库，照片相关测试不跑")
else:
    run(test_step1_activity)
    run(test_step1_non_activity)
    run(test_step2_members)
    run(test_step3_unknown_and_resend)
    run(test_hook_every_photo)
done()
