"""
本地 OCR。平台无关 —— 图片怎么下载由适配器提供，这里只管识别。

设计要点：
1. OCR 全程在本机跑（RapidOCR，纯 onnxruntime，不需要 GPU、不联网）。
   图片本身绝不外传 —— 只有识别出来的文本才可能往下走到模型那一层。
2. 引擎加载要 1~2 秒，所以懒加载 + 全局复用，只在第一张图时付这个成本。
3. 识别结果拼进消息文本，和文字消息走完全相同的后续流程
   （粗筛 -> 模型判断 -> 回复），规则配置不用为图片单独写一套。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from typing import Callable

log = logging.getLogger("botcore.image")

# 平台适配器提供的下载函数：给图片标识，返回图片字节。失败返回 None。
Downloader = Callable[[str], "bytes | None"]


@dataclass
class OCRSettings:
    enabled: bool = False
    # 识别文本超过这个长度就截断。整张受理单截图可能识别出很多字，
    # 全塞给模型既贵又容易淹没关键信息。
    # OCR 全文的安全上限。提纯（distill）只留十几二十字，真正给模型的长度由它管，
    # 这里只防极端情况把内存撑爆。以前是 500：密集的纸质单照片把底部的水印
    # （工作内容 / 支局 / 时间）整段切掉，判单全废（审查踩出来的）
    max_chars: int = 4000
    # 低于这个置信度的行直接丢掉，减少 OCR 噪声
    min_score: float = 0.5
    # 一条消息最多处理几张图，防止有人一次发几十张
    max_images: int = 3
    # OCR 前把长边缩到这个尺寸。手机拍的现场照动辄 1900x2500，
    # 实测缩到 1280 提速约 40%，关键字段识别不受影响。0 = 不缩。
    max_side: int = 1280


def _shrink(blob: bytes, max_side: int) -> bytes:
    """OCR 前缩图。现场照片普遍 1900x2500 以上，缩到 1280 能省掉近一半时间。"""
    if max_side <= 0:
        return blob
    try:
        from io import BytesIO

        from PIL import Image

        img = Image.open(BytesIO(blob))
        w, h = img.size
        if max(w, h) <= max_side:
            return blob
        ratio = max_side / max(w, h)
        img = img.convert("RGB").resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        buf = BytesIO()
        img.save(buf, format="JPEG", quality=90)
        return buf.getvalue()
    except Exception as exc:
        log.debug("缩图失败，用原图跑 OCR：%s", exc)
        return blob


class OCREngine:
    """懒加载的 RapidOCR 封装。线程安全，全进程复用一个引擎实例。"""

    def __init__(self) -> None:
        self._engine = None
        self._lock = threading.Lock()
        self._failed = False
        self._run_lock = threading.Lock()

    def _ensure(self):
        if self._engine is not None or self._failed:
            return self._engine
        with self._lock:
            if self._engine is not None or self._failed:
                return self._engine
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError:
                log.error(
                    "OCR 已启用但没装依赖。请执行：pip install rapidocr-onnxruntime"
                )
                self._failed = True
                return None
            try:
                log.info("正在加载 OCR 模型（首次约需 1~2 秒）……")
                self._engine = RapidOCR()
                log.info("OCR 模型加载完成")
            except Exception:
                log.exception("OCR 引擎初始化失败")
                self._failed = True
            return self._engine

    def read(self, blob: bytes, settings: OCRSettings) -> str:
        return " ".join(text for _box, text in self.read_items(blob, settings)[1])

    def read_items(self, blob: bytes, settings: OCRSettings) -> tuple[bytes, list[tuple[list, str]]]:
        """
        返回 (缩过的图, [(文字框四角坐标, 文字), ...])。坐标对应的是【缩过的图】。
        打卡核验要用框：送视觉模型前把证件号、验证码那几行涂黑。
        """
        engine = self._ensure()
        blob = _shrink(blob, settings.max_side)
        if engine is None:
            return blob, []

        try:
            # RapidOCR 的封装不保证线程安全，串行执行。单张约 1 秒，
            # 业绩群不会有那么高的并发图片量，够用。
            with self._run_lock:
                result, _elapse = engine(blob)
        except Exception:
            log.exception("OCR 识别过程出错")
            return blob, []

        items: list[tuple[list, str]] = []
        for item in result or ():
            # RapidOCR 返回 [box, text, score]
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            text = str(item[1] or "").strip()
            if not text:
                continue
            score = item[2] if len(item) > 2 else 1.0
            try:
                if float(score) < settings.min_score:
                    continue
            except (TypeError, ValueError):
                pass
            items.append((item[0], text))
        return blob, items


_ENGINE = OCREngine()


def preload(settings: OCRSettings) -> None:
    """
    启动时把模型加载好，省掉群里第一张图的等待。

    实测（i5-9300H，560x400 截图）：
        加载模型   ~1.4 秒   启动时一次性付掉
        第一张图   ~2.2 秒
        之后每张   ~1.15 秒

    试过在这里拿造出来的小图空跑一次预热推理，没用 —— 首张仍是 2.2 秒。
    onnxruntime 会按输入形状做优化，空跑图和真实截图尺寸不同，白热身。
    要真消掉这 1 秒，得拿一张尺寸接近的真实截图来跑，不值当。
    """
    if settings.enabled:
        _ENGINE._ensure()


def recognize(download: Downloader, image_ids: list[str],
              settings: OCRSettings) -> str:
    """
    下载图片并识别，返回拼好的文本。未启用或全部失败时返回空串。

    download 由平台适配器提供 —— 飞书用 message_resource 接口，
    企业微信用 get_msg_media，这里不关心。
    """
    if not settings.enabled or not image_ids:
        return ""

    texts: list[str] = []
    for key in image_ids[:max(1, settings.max_images)]:
        blob = download(key)
        if not blob:
            continue
        text = _ENGINE.read(blob, settings)
        if text:
            log.debug("图片 %s 识别出 %d 个字", key, len(text))
            texts.append(text)
        else:
            log.debug("图片 %s 没识别出文字", key)

    joined = " ".join(texts)
    if settings.max_chars > 0 and len(joined) > settings.max_chars:
        joined = joined[:settings.max_chars]
    return joined
