"""
人脸：录入 / 名单 / 删除 / 认人的逻辑和权限，先发命令后发照片，照片不被当成打卡，特征文件权限，不进备份。
不用真人照片：照片 -> 人脸特征这一步用假的（固定向量）顶替；真模型只测能加载、空白图认出 0 张脸。
"""
import io
import json
import os
import random
import zipfile

from _harness import LIVE, FakePlatform, check, done, eq, run, setup, skip, wait_pool

tmp = setup()

from botcore.config import ConfigStore  # noqa: E402
from botcore.faces import THRESHOLD, FaceIndex  # noqa: E402
from botcore.message import InboundMessage  # noqa: E402
from botcore.pipeline import Pipeline  # noqa: E402


def _vec(seed: int) -> list[float]:
    rnd = random.Random(seed)
    v = [rnd.gauss(0, 1) for _ in range(128)]
    n = sum(x * x for x in v) ** 0.5
    return [x / n for x in v]


def _near(v: list[float], seed: int, noise: float = 0.25) -> list[float]:
    """同一个人换一张照片：原向量加点噪声，余弦相似度远高于阈值。"""
    rnd = random.Random(seed)
    w = [x + rnd.gauss(0, noise / 11.3) for x in v]
    n = sum(x * x for x in w) ** 0.5
    return [x / n for x in w]


A, B, C = _vec(1), _vec(2), _vec(3)            # 杨桐、郭静、一个客户
FAKE = {b"yt1": [A], b"yt2": [_near(A, 9)], b"gj": [B], b"none": [], b"two": [A, C],
        b"crowd": [_near(A, 7), C], b"stranger": [C], b"small": [B], b"side": [B], b"slight": [B]}
GOOD = {"width": 380, "yaw": 0.1}
INFO = {b"small": {"width": 70, "yaw": 0.1}, b"side": {"width": 300, "yaw": 1.9}, b"slight": {"width": 300, "yaw": 0.4}}
IMAGES = {k.decode(): k for k in FAKE}

store = ConfigStore()
ADMIN = store.backup_settings().admins[0]
plat = FakePlatform(images=IMAGES)
p = Pipeline(plat, store)
p.faces._detect = lambda blob: [(f, INFO.get(blob, GOOD)) for f in FAKE[blob]]
p.faces.available = lambda: True
_n = [0]


def send(text="", imgs=(), chat="p2p", sender=ADMIN) -> str:
    _n[0] += 1
    before = len(plat.replies)
    p._process(InboundMessage(msg_id=f"f{_n[0]}", chat_id=sender if chat == "p2p" else "g", chat_type=chat,
                              text=text, sender_id=sender, image_ids=list(imgs), mentions_bot=True), 0.0)
    wait_pool(p.checkin_pool, 0.05)
    return plat.replies[-1] if len(plat.replies) > before else ""


def test_permissions():
    check("只有管理员" in send("人脸名单", sender="YangTong"), "非管理员不能用")
    check("单聊" in send("人脸名单", chat="group"), "群里提示单聊")
    eq(send("人脸名单"), "还没有录入任何人", "一开始名单是空的")


def test_enroll_and_identify():
    check("已录入杨桐" in send("录入人脸 杨桐", imgs=["yt1"]), "命令和照片同一条：录入")
    check("好，2 分钟内" in send("录入人脸 杨桐"), "先发命令")
    check("第 2 张" in send(imgs=["yt2"]), "再发照片：录入第 2 张")
    check("没找到人脸" in send("录入人脸 郭静", imgs=["none"]), "没有脸的照片不收")
    check("2 张脸" in send("录入人脸 郭静", imgs=["two"]), "两张脸的照片不收")
    check("脸太小" in send("录入人脸 郭静", imgs=["small"]), "脸太小（离得远）的录入照不收")
    check("太侧了" in send("录入人脸 郭静", imgs=["side"]), "侧身太多的录入照不收")
    r = send("录入人脸 郭静", imgs=["gj"])
    check("已录入郭静" in r and "正脸" in r and "还差 2 张" in r, f"录入郭静：{r}")
    r = send("录入人脸 郭静", imgs=["slight"])
    check("第 2 张，略侧" in r and "还差 1 张" in r, f"略侧的收下：{r}")
    send("删除人脸 郭静")
    send("录入人脸 郭静", imgs=["gj"])
    eq(send("人脸名单"), "已录入：杨桐（2 张）、郭静（1 张）", "名单")

    r = send("认人", imgs=["crowd"])
    check("认出：杨桐" in r and "另有 1 张不在名单上" in r, f"一张认识一张不认识：{r}")
    check("郭静" not in r, "没把客户认成郭静")
    r = send("认人", imgs=["stranger"])
    check("一个都没认出" in r, f"陌生人：{r}")
    send("认人")
    r = send(imgs=["gj"])
    check("认出：郭静" in r, f"先发「认人」再发照片：{r}")

    before = len(p.checkin.store.records)
    send("录入人脸 杨桐", imgs=["yt1"])
    eq(len(p.checkin.store.records), before, "录入用的照片没被当成打卡")


def test_storage_and_privacy():
    d = tmp / "faces"
    files = sorted(x.name for x in d.glob("*.json"))
    eq(files, ["杨桐.json", "郭静.json"], "每人一个特征文件")
    person = json.loads((d / "杨桐.json").read_text(encoding="utf-8"))
    check(set(person) == {"name", "features", "added"} and len(person["features"][0]) == 128,
          "文件里只有特征向量和录入记录，没有照片")
    if os.name == "posix":
        eq(oct((d / "杨桐.json").stat().st_mode & 0o777), "0o600", "特征文件只有 root 能读")
        eq(oct(d.stat().st_mode & 0o777), "0o700", "目录只有 root 能进")
    from botcore.backup import make_zip
    names = zipfile.ZipFile(make_zip(tmp / "b.zip")).namelist()
    check(not any("face" in n or "杨桐" in n for n in names), f"人脸特征不进备份：{names}")
    check("已删除杨桐" in send("删除人脸 杨桐"), "删除")
    check("没有杨桐" in send("删除人脸 杨桐"), "再删：没有了")
    eq(send("人脸名单"), "已录入：郭静（1 张）", "删除后名单")


def test_p2p_photo_still_checkin():
    """没有先发人脸命令的单聊照片，照旧走打卡（不被人脸功能截走）。"""
    eq(p._handle_face_command(InboundMessage(msg_id="x", chat_id="YangTong", chat_type="p2p",
                                             sender_id="YangTong", image_ids=["yt1"])), False,
       "普通单聊照片不归人脸功能管")


def test_real_model():
    models = LIVE / "models"
    fi = FaceIndex(model_dir=models, store_dir=tmp / "faces_real")
    if not fi.available():
        skip("没有人脸模型（服务器 data/models），真模型测试不跑")
        return
    from PIL import Image
    b = io.BytesIO()
    Image.new("RGB", (640, 480), "white").save(b, "JPEG")
    eq(fi.identify(b.getvalue()), {"faces": 0, "matches": [], "unknown": 0}, "真模型加载，空白图 0 张脸")
    check(THRESHOLD == 0.363, "阈值用 SFace 官方的 0.363")


run(test_permissions)
run(test_enroll_and_identify)
run(test_storage_and_privacy)
run(test_p2p_photo_still_checkin)
run(test_real_model)
done()
