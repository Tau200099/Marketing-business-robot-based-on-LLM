"""
无效打卡兜底（09-29 用户定）：判成无效 / 判不出来 → 回执提醒 10 分钟内重拍；10 分钟内同一个人发来有效的 → 替换最近那条
（用旧的时间 / 时段）；超过 10 分钟照常另记；10 分钟内再发还是无效 → 照常另记；别人的有效照片不替换你的无效。
视觉模型是假的：一会儿让它判无效、一会儿判有效。
"""
import time

from _harness import FakePlatform, check, done, eq, fake_vision, run, setup, skip, wait_pool, watermark_photos

tmp = setup()

from botcore.checkin import DISPLAY  # noqa: E402
from botcore.config import ConfigStore  # noqa: E402
from botcore.message import InboundMessage  # noqa: E402
from botcore.pipeline import Pipeline  # noqa: E402

PHOTOS = watermark_photos(10)
INVALID = {"people": False, "place": "其他", "tv": "没有电视", "installing": False, "talking": False,
           "folder": False, "label": "无效", "evidence": "空房间"}
store = ConfigStore()
plat = FakePlatform(images={f"w{i}": b for i, b in enumerate(PHOTOS)})
p = Pipeline(plat, store)
rec = p.checkin
MODELS = [store.checkin_settings().vision_model, *store.checkin_settings().vision_fallback_models]
_n = [0]


def judge(mode: str):
    rec._judge._down.clear()
    if mode == "invalid":
        fake_vision(rec._judge, {}, answer=INVALID)
    elif mode == "down":
        fake_vision(rec._judge, {m: "quota" for m in MODELS})
    else:
        fake_vision(rec._judge, {})


def send(img, sender):
    _n[0] += 1
    before = len(plat.replies)
    p._process(InboundMessage(msg_id=f"iv{_n[0]}", chat_id=sender, chat_type="p2p", sender_id=sender,
                              image_ids=[img], mentions_bot=True), 0.0)
    wait_pool(p.checkin_pool)
    return plat.replies[-1] if len(plat.replies) > before else ""


def records():
    return rec.store.records


def test_replace_within_10min():
    judge("invalid")
    r = send("w0", "uA")
    check("无效打卡" in r and "请在 10 分钟内" in r and "有效的会替换这条" in r, f"无效：提醒 10 分钟内重拍 {r}")
    old = records()[-1]
    old_at, old_slot, n = old.at, old.slot, len(records())
    eq(old.label, DISPLAY["无效"], "先记成无效")
    judge("valid")
    r = send("w1", "uA")
    check("已替换" in r and f"已替换 {old_at[11:16]}" in r, f"10 分钟内有效：替换 {r}")
    eq(len(records()), n, "条数不变（替换，不是多一条）")
    new = next(x for x in records() if x.at == old_at and x.slot == old_slot and x.sender == "uA")
    eq((new.label, new.at, new.slot), ("现场拦截", old_at, old_slot), "用旧的时间和时段，类型换成新的")


def test_after_10min_new_record():
    judge("invalid")
    send("w2", "uB")
    with rec._win_lock:                    # 模拟过了 10 分钟
        rec._invalid_windows["uB"] = [(time.time() - 1, ph) for _d, ph in rec._invalid_windows["uB"]]
    n = len(records())
    judge("valid")
    r = send("w3", "uB")
    check(r.startswith("打卡已记") and "已替换" not in r, f"超过 10 分钟：照常另记 {r}")
    eq(len(records()), n + 1, "多一条")
    eq(sum(1 for x in records() if x.sender == "uB" and x.label == DISPLAY["无效"]), 1, "那条无效还在")


def test_invalid_again():
    judge("invalid")
    n = len(records())
    r1 = send("w4", "uC")
    r2 = send("w5", "uC")
    eq(len(records()), n + 2, "10 分钟内再发还是无效：照常另记（不特殊处理）")
    check("10 分钟内" in r1 and "10 分钟内" in r2, "两条都提醒重拍")


def test_undecided_counts_as_invalid():
    judge("down")
    r = send("w6", "uD")
    check("判不出" in r and "先按无效打卡处理" in r and "10 分钟内" in r, f"判不出来：当无效，提醒重拍 {r}")
    eq(rec.pending_count(), 1, "存了图等补判")
    judge("valid")
    r = send("w7", "uD")
    check("已替换" in r, f"重拍有效：替换那条判不出来的 {r}")
    eq(rec.pending_count(), 0, "替换后不用再补判")


def test_other_person_not_replace():
    judge("invalid")
    send("w8", "uE")
    judge("valid")
    r = send("w9", "uF")
    check("已替换" not in r, "别人的有效照片不替换你的无效")
    eq(sum(1 for x in records() if x.sender == "uE" and x.label == DISPLAY["无效"]), 1, "uE 的无效还在")


if len(PHOTOS) < 10:
    skip(f"样本库可用照片不够 10 张（{len(PHOTOS)}）")
else:
    run(test_replace_within_10min)
    run(test_after_10min_new_record)
    run(test_invalid_again)
    run(test_undecided_counts_as_invalid)
    run(test_other_person_not_replace)
done()
