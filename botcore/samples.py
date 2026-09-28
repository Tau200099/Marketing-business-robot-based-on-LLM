"""
打卡照片样本采集：给「有效打卡判定」攒标注好的照片。

用法（单聊机器人，或只有自己和机器人的测试群）：
    在输入框里粘贴照片，再打一行「样本 板夹」一起发（图文混排）；
    或者先发「样本 板夹」，2 分钟内再发照片（企微单聊图文分开发时用）。
    【单聊里】可以省掉「样本」两个字：直接发「现场拦截」「无效打卡」，再发照片（09-24 同事就是这么发的）。
    回执里带上机器自己的判定：「样本已收 #3 你标：无效 / 机器判：现场拦截（有人坐在床边）」，分歧当场看得见。

类型只认 6 个（2026-09-24 和业务确认）：拦截 / 设备演示 / 板夹 / 算账 / 异网 / 无效，「搬家」按同音错字当「板夹」，
后面带不带「打卡」都行（「样本 拦截打卡」= 「样本 拦截」），「演示」= 「设备演示」。

收到后：
    · 照片存【涂黑缩小版】（和送视觉模型的一样：证件 / 验证码 / 病历 / 长数字行涂黑，缩到 1024）到
      samples/checkin/<日期>/<序号>_<类型>_<时分秒>.jpg；业务单（「业务:…」）不存照片 ——
      它按工作内容文字判、评测用不上，又最常拍到证件（09-28 用户定，原图已删）
    · OCR 出来的文字【脱敏后】追加到 samples/checkin/index.jsonl
    · 回一句「样本已收 #12 板夹」
    · 不点赞、不入清单、不触发通报

⚠️ 涂黑版照片里仍有客户的脸和家里的样子，只留在服务器，不进 git、不进备份；评测时临时拉到本机，用完删。
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime

from .checkin import masked_jpeg
from .distill import distill, redact
from .image import recognize
from .message import InboundMessage
from .paths import DATA

log = logging.getLogger("botcore.samples")

# 2026-09-24 和业务确认的 6 类打卡（「拦截打卡」「异网打卡」…，后缀「打卡」可写可不写）
LABELS = ("拦截", "设备演示", "板夹", "算账", "异网", "无效")
# 「搬家」是「板夹」的同音错字（语音输入 / 拼音打错，09-24 同事就这么标的）
_ALIASES = {"演示": "设备演示", "现场拦截": "拦截", "算账单": "算账", "搬家": "板夹", "云盒": "设备演示"}


def _normalize(label: str) -> str:
    label = label.strip()
    if label.endswith("打卡") and len(label) > 2:
        label = label[:-2]
    return _ALIASES.get(label, label)
_TRIGGER = re.compile(r"^\s*样本\s*[:：]?\s*(\S*)")
_PENDING_SECONDS = 120
# redact() 只管手机号 / 身份证号。现场照还会拍到验证码、住院号、病人姓名 ——
# 样本索引里 4 位以上的数字一律抹掉，「名：」后面的 2~4 个字也抹掉
_LONG_DIGITS = re.compile(r"\d{4,}")
_NAME_AFTER = re.compile(r"(名\s*[:：]\s*)[一-龥]{2,4}")


def _scrub(ocr: str) -> str:
    t = redact(ocr or "")
    t = _NAME_AFTER.sub(lambda m: m.group(1) + "[姓名]", t)
    return _LONG_DIGITS.sub("[数字]", t)


ROOT = DATA / "samples" / "checkin"
INDEX = ROOT / "index.jsonl"


class SampleCollector:
    def __init__(self, platform, ocr_settings_getter, judge=None) -> None:
        self.platform = platform
        self._ocr_settings = ocr_settings_getter
        # judge(blob) -> (格子里的字, 证据) | None：打卡核验的「只判不记」，回执里给出机器判定
        self._judge = judge
        self._lock = threading.Lock()
        # (chat_id, sender_id) -> (类型, 过期时间)：先发标注、后发图的那种
        self._pending: dict[tuple[str, str], tuple[str, float]] = {}

    def handle(self, msg: InboundMessage) -> bool:
        """是样本消息就处理并返回 True，调用方不再往下走点赞 / 记单。"""
        text = (msg.text or "").strip()
        m = _TRIGGER.match(text)
        key = (msg.chat_id, msg.sender_id)
        # 单聊里整条就是一个类型名（「现场拦截」「无效打卡」）也当样本标注
        bare = text.strip(" 。.，,！!") if msg.chat_type == "p2p" else ""
        if not m and bare and _normalize(bare) in LABELS:
            m = re.match(r"(.*)", bare)
        # 单聊里「照片 + 一句很短的标注」，标注不在 6 类里也存下来，一条都不丢（09-24 同事实际这么发的）：
        #   业务写法「新增宽带」「79续签+10」-> 标「业务:原文」，拿来考业务优先规则
        #   规则外的新类型「搬家打卡」     -> 原样当类型，回执里说一声规则里还没有
        if not m and bare and msg.image_ids and len(bare) <= 12:
            from .checkin import business_of
            if business_of(bare) or re.search(r"\d{2,3}", bare):
                self._save(msg, "业务:" + bare)
                return True
            if bare.endswith("打卡"):
                self._save(msg, bare, unknown=True)
                return True

        if m:
            label = _normalize(m.group(1))
            if label not in LABELS:
                self._reply(msg, f"样本类型只认：{' / '.join(LABELS)}（后面带不带「打卡」都行）。例：样本 板夹")
                return True
            if msg.image_ids:
                self._save(msg, label)
            else:
                with self._lock:
                    self._pending[key] = (label, time.time() + _PENDING_SECONDS)
                self._reply(msg, f"收到标注「{label}」，2 分钟内把照片发过来")
            return True

        # 只有图、没字：看是不是刚才先报了类型
        if msg.image_ids and not text:
            with self._lock:
                label, until = self._pending.get(key, ("", 0.0))
                if label and time.time() <= until:
                    del self._pending[key]
                else:
                    label = ""
            if label:
                self._save(msg, label)
                return True
        return False

    # ---------- 存 ----------

    def _save(self, msg: InboundMessage, label: str, unknown: bool = False) -> None:
        now = datetime.now()
        day_dir = ROOT / now.strftime("%Y%m%d")
        day_dir.mkdir(parents=True, exist_ok=True)

        blobs: dict[str, bytes] = {}
        for image_id in msg.image_ids:
            blob = self.platform.download_image(msg, image_id)
            if blob:
                blobs[image_id] = blob
        if not blobs:
            self._reply(msg, "照片没下载下来，再发一次")
            return

        # 同一条消息里的几张图共用一段 OCR：只拿来看水印和画面里的字，不做判定
        ocr = recognize(lambda k: blobs.get(k), list(blobs), self._ocr_settings())

        wm = distill(ocr or "")
        # 机器自己判一遍（和正式打卡同一套：业务优先 → 原样 → 视觉模型 + 本机核对），记进索引、写进回执
        machine: dict[int, tuple[str, str]] = {}
        if self._judge is not None:
            for i, blob in enumerate(blobs.values()):
                try:
                    got = self._judge(blob)
                except Exception:
                    log.exception("样本机器判定出错")
                    got = None
                if got:
                    machine[i] = got
        saved = []
        with self._lock:
            n = self._count()
            for i, blob in enumerate(blobs.values()):
                n += 1
                # 文件名里不能有「:」这类字符（「业务:79续签+10」拉回 Windows 看会出错）
                safe = re.sub(r'[\/:*?"<>|\s]+', "_", label)
                if label.startswith("业务:"):
                    rel = ""                     # 业务单不存照片，只留下面的文字记录
                else:
                    name = f"{n:03d}_{safe}_{now:%H%M%S}.jpg"
                    try:
                        (day_dir / name).write_bytes(masked_jpeg(blob, self._ocr_settings()))
                        rel = f"{day_dir.name}/{name}"
                    except Exception:
                        log.exception("样本涂黑失败，照片不存（只留文字记录）")
                        rel = ""
                rec = {"n": n, "label": label, "file": rel,
                       "at": now.strftime("%Y-%m-%d %H:%M:%S"),
                       "chat_type": msg.chat_type,
                       # 水印字段单独抽出来（时间 / 支局 / 工作内容），原文里的数字要抹掉
                       "branch": wm.branch, "stamp": wm.stamp, "watermark": wm.text,
                       # 脱敏后的 OCR：数字和人名抹掉，字面特征（「居民身份证」「资费」「验证码」）留着
                       "ocr": _scrub(ocr),
                       "machine": machine.get(i, ("", ""))[0], "machine_evidence": machine.get(i, ("", ""))[1]}
                with INDEX.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
                saved.append(n)
        log.info("打卡样本已存 %s 张 | %s | #%s | 机器判：%s", len(saved), label, ",".join(map(str, saved)),
                 "；".join(f"{a}（{b}）" for a, b in machine.values()) or "无")
        verdict = "；".join(f"{a}（{b}）" if b else a for a, b in machine.values())
        self._reply(msg, f"样本已收 #{'、#'.join(map(str, saved))} 你标：{label}"
                         + ("（规则里还没有这个类型）" if unknown else "")
                         + (f" / 机器判：{verdict}" if verdict else ""))

    @staticmethod
    def _count() -> int:
        if not INDEX.exists():
            return 0
        with INDEX.open(encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())

    def _reply(self, msg: InboundMessage, text: str) -> None:
        try:
            self.platform.reply(msg, text, None)
        except Exception:
            log.exception("样本回执发送失败")
