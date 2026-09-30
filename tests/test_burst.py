"""
同时来一堆照片（09-29 用户担心「同时收到几张只分析一张、漏掉其他的」）：
走真实的收消息路径（on_message → 线程池 → 打卡队列 → 人脸队列），6 个人同时各发一张 + 1 个人一条消息 3 张（其中 1 张下载失败），共 8 张好的，
检查：每条消息都有回执、每张下载成功的都记上、下载失败的在回执里点名、人脸队列每张都看过。
"""
import threading
import time

from _harness import FakePlatform, check, done, eq, fake_vision, run, setup, skip, watermark_photos

tmp = setup()

from botcore.config import ConfigStore  # noqa: E402
from botcore.message import InboundMessage  # noqa: E402
from botcore.pipeline import Pipeline  # noqa: E402

PHOTOS = watermark_photos(8)


class SlowPlatform(FakePlatform):
    """下载慢一点、reply 加锁：更像真实的并发。第 x_bad 张永远下载失败。"""
    def __init__(self, images):
        super().__init__(images=images)
        self._lk = threading.Lock()
        self.by_chat: dict[str, list[str]] = {}

    def download_image(self, msg, key):
        time.sleep(0.05)
        return self.images.get(key)

    def reply(self, msg, text, at=None):
        with self._lk:
            self.replies.append(text)
            self.by_chat.setdefault(msg.chat_id, []).append(text)
        return True


def test_burst():
    images = {f"b{i}": b for i, b in enumerate(PHOTOS)}
    plat = SlowPlatform(images)
    store = ConfigStore()
    p = Pipeline(plat, store)
    fake_vision(p.checkin._judge, {})
    faced = []
    lk = threading.Lock()
    real = p.chief_board.on_photo

    def spy(blob, wm, photo):
        with lk:
            faced.append(photo)
        return real(blob, wm, photo)
    p.chief_board.on_photo = spy

    msgs = [InboundMessage(msg_id=f"burst{i}", chat_id=f"u{i}", chat_type="p2p", sender_id=f"u{i}",
                           image_ids=[f"b{i}"], mentions_bot=True) for i in range(6)]
    # 一条消息 3 张：两张好的（b6、b7）+ 一张下载失败的
    msgs.append(InboundMessage(msg_id="burst_multi", chat_id="u_multi", chat_type="p2p", sender_id="u_multi",
                               image_ids=["b6", "missing", "b7"], mentions_bot=True))
    threads = [threading.Thread(target=p.on_message, args=(m,)) for m in msgs]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    deadline = time.time() + 180
    while time.time() < deadline and len(plat.by_chat) < len(msgs):
        time.sleep(0.5)
    p.checkin_pool.submit(lambda: None).result(timeout=60)
    p.chief_board.wait_idle(60)
    print(f"  （{len(msgs)} 条消息 {sum(len(m.image_ids) for m in msgs)} 张照片，{time.time() - t0:.1f} 秒全部处理完）")
    eq(sorted(plat.by_chat), sorted(m.chat_id for m in msgs), "每条消息都有回执")
    eq(len(p.checkin.store.records), 8, "8 张下载成功的都记上了")
    multi = plat.by_chat.get("u_multi", [""])[-1].split("\n")
    eq(len(multi), 3, f"一条消息 3 张：回执 3 行 {multi}")
    check(multi[0].startswith("第1张：打卡已记") and multi[2].startswith("第3张：打卡已记"), "好的两张记上")
    eq(multi[1], "第2张：照片没下载下来，请重发这一张", "下载失败的那张点名是第几张")
    eq(len(set(faced)), 8, "人脸队列 8 张都看过")


def test_restart_notice():
    """正常关机时，还没回执的单聊照片挨个回「请重发」；已经回过的不再打扰。"""
    plat = SlowPlatform({f"b{i}": b for i, b in enumerate(PHOTOS)})
    store = ConfigStore()
    p = Pipeline(plat, store)
    gate = threading.Event()
    p.checkin_pool.submit(gate.wait, 30)          # 把打卡队列堵住：模拟排队中
    msgs = [InboundMessage(msg_id=f"rs{i}", chat_id=f"r{i}", chat_type="p2p", sender_id=f"r{i}",
                           image_ids=[f"b{i}"], mentions_bot=True) for i in range(3)]
    for m in msgs:
        p.on_message(m)
    time.sleep(3)
    eq(len(p._inflight_photos), 3, "3 条照片消息在途（下载了、排队等判定）")
    n = p.notify_unfinished()
    eq(n, 3, "关机前 3 条都提醒了")
    check(all("请 1 分钟后重新发一次" in plat.by_chat.get(m.chat_id, [""])[-1] for m in msgs), "每个人都收到「请重发」")
    gate.set()


if len(PHOTOS) < 8:
    skip("没有样本库，照片相关测试不跑")
else:
    run(test_burst)
    run(test_restart_notice)
done()
