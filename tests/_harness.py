"""
测试公用：临时数据目录、假平台、假视觉接口、断言记录。

每个 test_*.py 由 run_all.py 单独起一个进程跑 —— BOT_DATA_DIR 在 import botcore 时就定了，
所以【先调 setup()，再 import botcore】。

不联网、不花钱：
  · 数据目录是临时的，只复制 config.yaml 和 roster.json 过来（正式数据一个字节都不碰）
  · 没有 .env：文字模型拿不到 key 自动不调；视觉模型用 fake_vision() 顶替
  · 不连企业微信：FakePlatform 把回复、发文件都记在列表里
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# 真实数据只用来复制配置和名册；服务器上是正式目录，本机退回仓库里的模板
LIVE = Path(os.getenv("TEST_SOURCE_DATA") or "/opt/feishu-bot/data")
SAMPLES = LIVE / "samples" / "checkin"

_failures: list[str] = []
_checks = [0]


def setup(copy=("config.yaml", "roster.json")) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="bot_test_"))
    for name in copy:
        src = LIVE / name if (LIVE / name).is_file() else REPO / name
        if src.is_file():
            shutil.copy(src, tmp / name)
    os.environ["BOT_DATA_DIR"] = str(tmp)
    os.environ["VISION_API_KEY"] = "fake"
    os.environ.pop("LLM_API_KEY", None)
    sys.path.insert(0, str(REPO))
    import logging
    logging.basicConfig(level=logging.ERROR, format="    %(levelname)s %(name)s %(message)s")
    return tmp


def check(cond, what: str) -> bool:
    _checks[0] += 1
    if not cond:
        _failures.append(what)
        print(f"  ✗ {what}")
    return bool(cond)


def eq(got, want, what: str) -> bool:
    return check(got == want, f"{what}：期望 {want!r}，实际 {got!r}")


def done() -> None:
    """每个测试文件最后调：打印结果，有失败退出码 1。"""
    if _failures:
        print(f"  失败 {len(_failures)} / {_checks[0]} 项")
        sys.exit(1)
    print(f"  通过 {_checks[0]} 项")
    sys.exit(0)


def run(fn) -> None:
    """跑一个测试函数，抛异常算失败，不影响后面的。"""
    try:
        fn()
    except Exception:
        _failures.append(f"{fn.__name__} 抛异常")
        print(f"  ✗ {fn.__name__} 抛异常：")
        traceback.print_exc()


class FakePlatform:
    """记下机器人说了什么、发了什么文件，不连网。"""
    name = "wecom"

    def __init__(self, images: dict | None = None, files: dict | None = None) -> None:
        self.cfg = type("C", (), {"chat_ids": ()})()
        self.replies: list[str] = []
        self.sent_texts: list[tuple[str, str]] = []
        self.sent_files: list[tuple[str, str]] = []
        self.images = images or {}
        self.files = files or {}

    def reply(self, msg, text, at=None):
        self.replies.append(text)
        return True

    def send_text(self, chat_id, text):
        self.sent_texts.append((chat_id, text))
        return True

    def send_file(self, chat_id, path):
        self.sent_files.append((chat_id, str(path)))
        return True

    def send_image(self, *a, **k):
        return True

    def download_image(self, msg, key):
        return self.images.get(key)

    def download_file(self, msg, key):
        return self.files.get(key, (None, ""))[0]

    def download_file_named(self, msg, key):
        return self.files.get(key, (None, ""))

    def start(self, cb):
        pass

    def close(self):
        pass

    def last(self) -> str:
        return self.replies[-1] if self.replies else ""


# ---------- 假视觉接口 ----------

VISION_OK = {"people": True, "place": "户外", "tv": "没有电视", "installing": False, "talking": True,
             "folder": False, "label": "拦截", "evidence": "户外交谈"}


class _Resp:
    def __init__(self, code: int, text: str = "", body=None) -> None:
        self.status_code, self.text, self._body = code, text, body

    def json(self):
        return self._body


def fake_vision(judge, mode: dict, calls: list | None = None, answer: dict | None = None):
    """
    把 VisionJudge 的 HTTP 换成假的。mode[模型名] = ok / quota / auth / rate（没写的算 ok）。
    calls 记下每次调了哪个模型。
    """
    body = {"choices": [{"message": {"content": json.dumps(answer or VISION_OK, ensure_ascii=False)}}]}

    def post(url, headers=None, json=None, timeout=None):
        m = json["model"]
        if calls is not None:
            calls.append(m)
        kind = mode.get(m, "ok")
        if kind == "ok":
            return _Resp(200, "", body)
        if kind == "quota":
            return _Resp(403, '{"error":{"code":"AllocationQuota.FreeTierOnly","message":"Free quota exhausted."}}')
        if kind == "auth":
            return _Resp(401, '{"error":{"code":"invalid_api_key"}}')
        return _Resp(429, '{"error":{"code":"Throttling.RateQuota"}}')

    judge._session.post = post


def watermark_photos(n: int = 4) -> list[bytes]:
    """
    样本库里的涂黑版水印照，只挑【要走视觉模型】的（工作内容里没写业务、也不是「异网打卡」这种原样照写的）。
    没有样本库就返回空，调用方跳过照片相关测试。要在 setup() 之后调。
    """
    if not SAMPLES.is_dir():
        return []
    from botcore.checkin import _ENGINE, CheckinSettings, is_watermark, parse_watermark, rule_label
    from botcore.config import ConfigStore
    keep = CheckinSettings().keep_as_is
    ocr = ConfigStore().ocr_settings()
    out = []
    for line in (SAMPLES / "index.jsonl").read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if not r.get("file") or r["label"].startswith("业务") or not (SAMPLES / r["file"]).is_file():
            continue
        if rule_label(parse_watermark(r.get("watermark") or "")["work"], keep):
            continue
        blob = (SAMPLES / r["file"]).read_bytes()
        # 涂黑版有两张把水印时间也涂掉了（#8 #26），机器人会回「没认出拍摄时间」—— 测试不用这种
        _shrunk, items = _ENGINE.read_items(blob, ocr)
        text = " ".join(t for _b, t in items)
        if not is_watermark(text) or not parse_watermark(text)["stamp"]:
            continue
        out.append(blob)
        if len(out) >= n:
            break
    return out


def wait_pool(pool, seconds: float = 0.3) -> None:
    """等打卡单线程池把排队的活做完（单聊打卡是异步判、判完回执）。"""
    time.sleep(seconds)
    pool.submit(lambda: None).result(timeout=120)
    time.sleep(seconds)


def skip(reason: str) -> None:
    print(f"  - 跳过：{reason}")
