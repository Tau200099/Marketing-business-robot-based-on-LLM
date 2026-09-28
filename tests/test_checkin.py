"""
打卡：单聊打卡照正式计数和回执、样本只认「样本 X」、单聊文字晒单提示去群里、
视觉模型额度用完换模型 / 提醒、待判定存图和补判、3 天放弃、「补判打卡」权限。
照片用样本库里的涂黑版；没有样本库（本机）就跳过照片相关的。视觉模型是假接口。
"""
import io
import json
from datetime import date, datetime, timedelta

from _harness import FakePlatform, check, done, eq, fake_vision, run, setup, skip, wait_pool, watermark_photos

tmp = setup()

from botcore.config import ConfigStore  # noqa: E402
from botcore.message import InboundMessage  # noqa: E402
from botcore.pipeline import Pipeline  # noqa: E402

PHOTOS = watermark_photos(8)


def _png() -> bytes:
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (200, 120), "white").save(b, "PNG")
    return b.getvalue()


images = {f"w{i}": b for i, b in enumerate(PHOTOS)}
images["png"] = _png()
plat = FakePlatform(images=images)
store = ConfigStore()
ADMIN = store.backup_settings().admins[0]
p = Pipeline(plat, store)
rec = p.checkin
ck = store.checkin_settings()
MODELS = [ck.vision_model, *ck.vision_fallback_models]
_n = [0]


def set_activity(active: bool) -> None:
    t = date.today()
    s, e = (t, t + timedelta(days=2)) if active else (t - timedelta(days=5), t - timedelta(days=2))
    (tmp / "activity.json").write_text(json.dumps({"title": "测试活动", "start": s.isoformat(),
                                                   "end": e.isoformat()}), encoding="utf-8")


def send(text="", imgs=(), chat="p2p", sender="YangTong") -> list[str]:
    _n[0] += 1
    before = len(plat.replies)
    p._process(InboundMessage(msg_id=f"c{_n[0]}", chat_id=sender if chat == "p2p" else "g", chat_type=chat,
                              text=text, sender_id=sender, image_ids=list(imgs), mentions_bot=True), 0.0)
    wait_pool(p.checkin_pool)
    return plat.replies[before:]


def records() -> int:
    return len(rec.store.records)


def test_p2p_checkin():
    set_activity(True)
    fake_vision(rec._judge, {})
    r = send(imgs=["w0"])
    check(r and r[-1].startswith("打卡已记：") and "现场拦截" in r[-1], f"单聊打卡照记上并回判定：{r}")
    r = send(imgs=["w1", "w2"])
    check(r and r[-1].count("打卡已记") == 2, f"一次两张回两行：{r}")
    eq(records(), 3, "记了 3 条")
    eq({x.sender for x in rec.store.records}, {"YangTong"}, "按发送人记")
    check("已经记过了" in send(imgs=["w0"])[-1], "同一张再发：已经记过了")
    check("不是今日水印照片" in send(imgs=["png"])[-1], "非水印照：说清楚没记")
    eq(records(), 3, "重复和非水印都不计")


def test_samples_need_prefix():
    n = records()
    send("现场拦截")
    r = send(imgs=["w3"])
    check(r and r[-1].startswith("打卡已记"), f"先打类型名再发照片 = 正式打卡，不被吞成样本：{r}")
    eq(records(), n + 1, "计了打卡")
    r = send("样本 板夹", imgs=["w3"])
    check(r and r[-1].startswith("样本已收"), f"「样本 板夹」+ 照片还是样本：{r}")
    eq(records(), n + 1, "样本不计打卡")
    saved = list((tmp / "samples" / "checkin").rglob("*.jpg"))
    check(len(saved) == 1, "样本存了一张（涂黑版）")


def test_p2p_text_order():
    r = send("支局：新盛支局\n业务类型:新增\n办理套餐：139\n业务号码:13800000001\n价值积分:139\n揽收人:郭静")
    check(r and "群里@我" in r[-1], f"单聊文字晒单提示去群里：{r}")
    eq(len(p.campaign().orders), 0, "单聊晒单不入账")


def test_off_activity():
    set_activity(False)
    n = records()
    r = send(imgs=["w4"], sender=ADMIN)
    check(r and "不在活动期间" in r[-1], f"活动期外单聊打卡回提示：{r}")
    eq(records(), n, "活动期外不计")
    set_activity(True)


def test_model_switch_and_pending():
    plat.sent_texts.clear()
    calls = []
    rec._judge._down.clear()
    rec._judge._alerted.clear()
    fake_vision(rec._judge, {MODELS[0]: "quota"}, calls)
    r = send(imgs=["w4"])
    check(r and r[-1].startswith("打卡已记"), f"主模型没额度自动换备用照样判：{r}")
    eq(calls, MODELS[:2], "先试主模型再换第一个备用")
    eq(rec.store.records[-1].model, MODELS[1], "记下是哪个模型判的")
    check(any("已自动换成" in t for _c, t in plat.sent_texts), "单聊提醒管理员换了模型")

    rec._judge._down.clear()
    fake_vision(rec._judge, {m: "quota" for m in MODELS}, calls)
    r = send(imgs=["w5"])
    check(r and "类型稍后判定" in r[-1], f"全没额度：先记下、类型稍后判定：{r}")
    eq(rec.pending_count(), 1, "存了一张待补判")
    meta = json.loads(next((tmp / "checkin_pending").glob("*.json")).read_text(encoding="utf-8"))
    check(all("身份" not in x for x in meta["lines"]), "待补判的文字去掉了敏感行")
    check(any("额度全用完" in t for _c, t in plat.sent_texts), "提醒管理员额度全用完")
    n_alerts = len(plat.sent_texts)
    rec._judge._down.clear()
    send(imgs=["w5"])            # 同一张，已记过，不再提醒
    eq(len(plat.sent_texts), n_alerts, "同一种提醒 6 小时只发一次")

    check("只有管理员" in send("补判打卡")[-1], "非管理员不能补判")
    check("单聊" in send("补判打卡", chat="group", sender=ADMIN)[-1], "群里补判提示去单聊")
    rec._judge._down.clear()
    r = send("补判打卡", sender=ADMIN)
    check("还有 1 张" in r[-1] and "额度全用完" in r[-1], f"额度还没恢复：补判不出来并说原因：{r}")

    rec._judge._down.clear()
    fake_vision(rec._judge, {})
    r = send("补判打卡", sender=ADMIN)
    check("判出 1 张" in r[-1] and "还有 0 张" in r[-1], f"额度恢复后补判：{r}")
    eq(rec.store.records[-1].source, "视觉", "记录改成视觉判定")
    eq(rec.pending_count(), 0, "补判后删了存图")
    eq(send("补判打卡", sender=ADMIN)[-1], "没有待判定的打卡", "没有待判定时直说")


def test_auth_rate_and_drop():
    calls = []
    rec._judge._down.clear()
    fake_vision(rec._judge, {m: "auth" for m in MODELS}, calls)
    photos = PHOTOS[6:8]
    plat.images.update({"w6": photos[0], "w7": photos[1]})
    send(imgs=["w6"])
    eq(calls, MODELS[:1], "key 失效不换模型")
    fake_vision(rec._judge, {MODELS[0]: "rate"}, calls)
    calls.clear()
    send(imgs=["w7"])
    eq(calls, [MODELS[0]] * 2, "限流只重试主模型，不当额度用完")
    check(MODELS[0] not in rec._judge._down, "限流不让主模型歇着")
    eq(rec.pending_count(), 2, "两张都待补判")
    for m in (tmp / "checkin_pending").glob("*.json"):
        d = json.loads(m.read_text(encoding="utf-8"))
        d["created"] = (datetime.now() - timedelta(days=4)).strftime("%Y-%m-%d %H:%M:%S")
        m.write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    fake_vision(rec._judge, {})
    r = rec.retry_now()
    eq((r["done"], r["dropped"], r["left"]), (0, 2, 0), "超过 3 天的放弃并删图")


if len(PHOTOS) < 8:
    skip("没有样本库（服务器 data/samples/checkin），照片相关测试不跑")
    run(test_p2p_text_order)
else:
    run(test_p2p_checkin)
    run(test_samples_need_prefix)
    run(test_p2p_text_order)
    run(test_off_activity)
    run(test_model_switch_and_pending)
    run(test_auth_rate_and_drop)
done()
