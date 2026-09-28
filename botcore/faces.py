"""
人脸识别（09-28 用户定：只认少数几个人，本人已同意录入）。

模型：OpenCV 官方 YuNet（找人脸）+ SFace（人脸特征），CPU 本机跑，放在 data/models/：
    face_detection_yunet_2023mar.onnx      sha256 8f2383e4…2552fa4
    face_recognition_sface_2021dec.onnx    sha256 0ba9fbfa…c34e79
（从 github.com/opencv/opencv_zoo 下载，按仓库 LFS 记录的 sha256 核对过；服务器连不上 GitHub 的文件服务，
 是本机下好 scp 上去的。模型不进 git。）

隐私：
  · 全程在自己服务器上算，人脸不发给任何外部模型
  · 录入只存特征向量（SFace 128 维，还原不出照片），照片不落盘；存 data/faces/<姓名>.json，只有 root 能读，
    不进 git、不进每日备份（backup 只打包 data 根目录的 *.json）；「删除人脸 X」连文件一起删
  · 识别时照片里不在名单上的脸（客户、路人）比完就丢，不存、不记日志
  · 结果只当参考：拍照的人常不在画面里、侧脸背影认不出，「没认出」不等于「不在」

用法（管理员单聊，pipeline 里接）：
    录入人脸 杨桐  + 照片（同一条，或先发这句、2 分钟内再发照片）   每人最多 5 张
    人脸名单 / 删除人脸 杨桐
    认人 + 照片                                                     试用识别
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path

from .paths import DATA

log = logging.getLogger("botcore.faces")

MODEL_DIR = DATA / "models"
FACE_DIR = DATA / "faces"
DETECTOR = "face_detection_yunet_2023mar.onnx"
RECOGNIZER = "face_recognition_sface_2021dec.onnx"
# SFace 官方给的余弦相似度阈值：≥ 0.363 算同一个人
THRESHOLD = 0.363
MAX_PER_PERSON = 5
MAX_SIDE = 1280           # 检测前把大图缩到这么大，手机原图太大又慢又占内存
MIN_SCORE = 0.8           # 人脸检测置信度


class FaceError(Exception):
    """给人看的错误（模型没装、照片打不开）。"""


class FaceIndex:
    def __init__(self, model_dir: Path = MODEL_DIR, store_dir: Path = FACE_DIR) -> None:
        self.model_dir = Path(model_dir)
        self.store_dir = Path(store_dir)
        self._lock = threading.Lock()          # OpenCV 的检测 / 识别对象不保证线程安全
        self._det = self._rec = None

    # ---------- 模型 ----------

    def available(self) -> bool:
        return (self.model_dir / DETECTOR).is_file() and (self.model_dir / RECOGNIZER).is_file()

    def _load(self) -> None:
        if self._rec is not None:
            return
        if not self.available():
            raise FaceError(f"人脸模型没装好（{self.model_dir} 下要有 {DETECTOR} 和 {RECOGNIZER}）")
        import cv2
        self._det = cv2.FaceDetectorYN.create(str(self.model_dir / DETECTOR), "", (320, 320), MIN_SCORE)
        self._rec = cv2.FaceRecognizerSF.create(str(self.model_dir / RECOGNIZER), "")
        log.info("人脸模型已加载")

    def _features_of(self, blob: bytes) -> list[list[float]]:
        """照片里每张脸的特征（已归一化）。没有脸返回空列表。"""
        import cv2
        import numpy as np
        img = cv2.imdecode(np.frombuffer(blob, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise FaceError("照片打不开")
        h, w = img.shape[:2]
        scale = min(1.0, MAX_SIDE / max(h, w))
        if scale < 1.0:
            img = cv2.resize(img, (int(w * scale), int(h * scale)))
            h, w = img.shape[:2]
        with self._lock:
            self._load()
            self._det.setInputSize((w, h))
            _n, faces = self._det.detect(img)
            out = []
            for face in (faces if faces is not None else ()):
                feat = self._rec.feature(self._rec.alignCrop(img, face)).flatten()
                norm = float(np.linalg.norm(feat)) or 1.0
                out.append([float(x) / norm for x in feat])
        return out

    # ---------- 名单 ----------

    def _path(self, name: str) -> Path:
        return self.store_dir / f"{name}.json"

    def _load_person(self, name: str) -> dict:
        try:
            return json.loads(self._path(name).read_text(encoding="utf-8"))
        except Exception:
            return {"name": name, "features": [], "added": []}

    def names(self) -> dict[str, int]:
        """已录入的人 -> 几张。"""
        out = {}
        for p in sorted(self.store_dir.glob("*.json")) if self.store_dir.is_dir() else ():
            try:
                out[p.stem] = len(json.loads(p.read_text(encoding="utf-8")).get("features") or [])
            except Exception:
                continue
        return out

    def enroll(self, name: str, blob: bytes, by: str = "") -> tuple[bool, str]:
        name = name.strip()
        if not name or any(c in name for c in r'\/:*?"<>|') or len(name) > 12:
            return False, "名字不对，这样发：录入人脸 杨桐"
        feats = self._features_of(blob)
        if not feats:
            return False, "照片里没找到人脸，换一张正脸、光线好一点的"
        if len(feats) > 1:
            return False, f"照片里有 {len(feats)} 张脸，请发只有{name}本人一张脸的照片"
        person = self._load_person(name)
        if len(person["features"]) >= MAX_PER_PERSON:
            return False, f"{name}已经录了 {MAX_PER_PERSON} 张，够了；要重录先发「删除人脸 {name}」"
        person["features"].append(feats[0])
        person["added"].append({"at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "by": by})
        self._save(name, person)
        log.info("录入人脸：%s（第 %d 张，%s 录入）", name, len(person["features"]), by)
        return True, f"已录入{name}的人脸（第 {len(person['features'])} 张，只存特征、不存照片）"

    def _save(self, name: str, person: dict) -> None:
        self.store_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.store_dir, 0o700)
        except OSError:
            pass
        tmp = self._path(name).with_suffix(".tmp")
        tmp.write_text(json.dumps(person, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        tmp.replace(self._path(name))

    def remove(self, name: str) -> bool:
        p = self._path(name.strip())
        if not p.is_file():
            return False
        p.unlink()
        log.info("删除人脸：%s", name)
        return True

    # ---------- 识别 ----------

    def identify(self, blob: bytes) -> dict:
        """
        {"faces": 照片里几张脸, "matches": [(姓名, 相似度)], "unknown": 不在名单上的几张}
        一张脸只认最像的那个人；同一个人被认出多次只留最高分。不在名单上的脸比完就丢。
        """
        import numpy as np
        feats = self._features_of(blob)
        people = {n: np.array(self._load_person(n)["features"], dtype=float) for n in self.names()}
        best: dict[str, float] = {}
        unknown = 0
        for f in feats:
            v = np.array(f, dtype=float)
            top_name, top = "", -1.0
            for n, arr in people.items():
                if len(arr):
                    s = float(np.max(arr @ v))
                    if s > top:
                        top_name, top = n, s
            if top >= THRESHOLD:
                best[top_name] = max(best.get(top_name, -1.0), top)
            else:
                unknown += 1
        matches = sorted(best.items(), key=lambda x: -x[1])
        return {"faces": len(feats), "matches": [(n, round(s, 2)) for n, s in matches], "unknown": unknown}
