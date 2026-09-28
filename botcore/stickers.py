"""
表情包图库：按分类管理本地图片、上传拿 media_id、随机不重样地挑。

为什么单独一个模块：
    企微发图要 media_id，而 media_id 只能通过【长连接】的分片上传拿到，
    且【有效期只有 3 天】。所以不能上传一次就完事，得有缓存 + 到期重传。

两条通道的分工（改动前先看懂）：
    · MCP（apikey）        —— 读消息、下载照片、发文字
    · 长连接（BotID+Secret）—— 上传表情包、发图片
    长连接上传的 media_id【MCP 通道不认】（实测 errcode 40073 非法的 media_id），
    所以图片必须用产生它的那条长连接发出去。

分类靠文件名前缀，不靠子目录 —— 直接把图丢进 assets/stickers/ 就行，
文件名以分类名开头即可，后面接什么都行：
    点赞1.gif  点赞2.GIF  捷报频传1.GIF  全力以赴3.GIF  晒单.GIF
不认识的前缀会被忽略并打日志，免得图放错名字后静默不生效。

缓存文件按「图片路径 + 文件内容指纹」索引，图片换了内容会自动重传。
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import random
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .paths import DATA

log = logging.getLogger("botcore.stickers")

# 企微给的有效期是 3 天。留足余量，到 2.5 天就重传 ——
# 卡着 3 天用容易在边界上撞到「已过期」。
_TTL_SECONDS = 2.5 * 24 * 3600

_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif"}

# 分类名 -> 用途。前缀匹配，按长度从长到短匹配，避免短前缀抢走长的。
CATEGORIES = {
    "点赞": "识别到业绩后跟在文字点赞后面",
    "捷报频传": "短时间内业绩扎堆时",
    "全力以赴": "长时间没有业绩时的鼓劲",
    "晒单": "配合全力以赴，按概率追加",
    "早上加油": "每天早上定时",
    "下午加油": "每天下午定时",
}


@dataclass
class StickerSettings:
    enabled: bool = False
    # 图片目录，相对项目根目录
    directory: str = "assets/stickers"
    # media_id 缓存，相对项目根目录
    cache_file: str = "stickers_cache.json"
    # 同一分类里，最近用过的这么多张不再重复挑
    avoid_last: int = 3
    # 点赞时跟几张
    praise_count: int = 2
    # 这一条同时触发捷报时，点赞图减到几张 —— 否则一次刷出
    # 「文字 + 2 张点赞 + 1 张捷报」共 4 条消息，太密
    praise_count_on_streak: int = 1

    # ---- 活跃时段：自动发图只在这个窗口内，窗口外一律不发 ----
    # ISO 星期：周一=1 … 周日=7
    active_days: tuple = (4, 5, 6, 7)
    active_start: str = "08:30"
    active_end: str = "20:30"

    # ---- 捷报频传：业绩扎堆时 ----
    streak_enabled: bool = True
    # 1 小时窗口内累计到第几条时触发
    streak_threshold: int = 4
    streak_count: int = 1
    # 触发后多久内不再发，避免业绩连来时每条都发
    streak_cooldown_minutes: int = 60

    # ---- 全力以赴：长时间没业绩时 ----
    idle_enabled: bool = True
    idle_after_minutes: int = 60
    # 持续冷清时每隔多久再发一次
    idle_repeat_minutes: int = 60
    idle_count: int = 2
    # 按这个概率追加一张晒单
    idle_bonus_probability: float = 0.5

    # ---- 每日定点 ----
    daily: tuple = (
        # 早上加油天天发，不跟全局的周四到周日
        {"at": "08:30", "category": "早上加油", "count": 1,
         "days": [1, 2, 3, 4, 5, 6, 7]},
        {"at": "14:00", "category": "下午加油", "count": 1},
    )


@dataclass
class _Entry:
    media_id: str
    uploaded_at: float
    digest: str


class StickerPool:
    """
    按分类的图库 + media_id 缓存。

    线程安全性：pick() 会被工作线程调用，内部只读列表和每类一个 deque，
    上传走平台层的跨线程桥接，本身不碰事件循环。
    """

    def __init__(self, cfg: StickerSettings, root: Path | None = None,
                 namespace: str = "") -> None:
        self.cfg = cfg
        self.root = root or DATA
        # 缓存键带平台前缀：企微的 media_id 和飞书的 image_key 互不相认，
        # 切换平台时不加前缀会拿着对方的 key 去发，每张都要失败一次再重传
        self.namespace = namespace
        self._cache: dict[str, _Entry] = {}
        # remember() / forget() 会在不同 worker 线程里同时改 _cache，
        # _save_cache 遍历时没锁会抛 dictionary changed size（审查踩出来的）
        self._cache_lock = threading.Lock()
        self._files: dict[str, list[Path]] = {}
        self._recent: dict[str, deque[str]] = {}
        self._load_cache()
        self.rescan()

    # ---------- 图库 ----------

    def rescan(self) -> None:
        d = self.root / self.cfg.directory
        self._files = {k: [] for k in CATEGORIES}
        if not d.is_dir():
            log.info("表情包目录不存在：%s（表情包功能不生效）", d)
            return

        # 前缀按长度倒序匹配：否则「早上加油」会被更短的前缀抢走
        prefixes = sorted(CATEGORIES, key=len, reverse=True)
        unknown: list[str] = []
        for p in sorted(d.iterdir()):
            if not p.is_file() or p.suffix.lower() not in _SUFFIXES:
                continue
            cat = next((c for c in prefixes if p.stem.startswith(c)), None)
            if cat is None:
                unknown.append(p.name)
                continue
            self._files[cat].append(p)

        for cat, files in self._files.items():
            if files:
                log.info("表情包「%s」：%d 张", cat, len(files))
            else:
                log.warning("表情包「%s」一张都没有，用到时会跳过", cat)
        if unknown:
            log.warning("这些图片文件名不以已知分类开头，已忽略：%s（可用分类：%s）",
                        "、".join(unknown[:6]), "、".join(CATEGORIES))

    def has(self, category: str) -> bool:
        return bool(self._files.get(category))

    @property
    def available(self) -> bool:
        return any(self._files.values())

    # ---------- 挑图 ----------

    def pick(self, category: str, count: int = 1) -> list[Path]:
        """
        从某个分类里随机挑 count 张，本次挑出的互不重复，
        且尽量避开该分类最近用过的几张。

        可选的图不够时会放宽：先清最近记录，还不够就有几张给几张。
        """
        files = self._files.get(category) or []
        if not files:
            return []

        recent = self._recent.setdefault(
            category, deque(maxlen=max(0, self.cfg.avoid_last)))
        pool = [p for p in files if str(p) not in recent]
        if len(pool) < count:
            # candidates 不够就把最近记录清掉重来 —— 图少的时候不至于挑不出
            recent.clear()
            pool = list(files)

        chosen = random.sample(pool, min(count, len(pool)))
        for p in chosen:
            recent.append(str(p))
        return chosen

    # ---------- 缓存 ----------

    def _cache_path(self) -> Path:
        return self.root / self.cfg.cache_file

    def _load_cache(self) -> None:
        p = self._cache_path()
        if not p.exists():
            return
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("表情包缓存读不了，当作空的重来：%s", exc)
            return
        for k, v in (raw or {}).items():
            try:
                self._cache[k] = _Entry(v["media_id"], float(v["uploaded_at"]),
                                        v.get("digest", ""))
            except Exception:
                continue

    def _save_cache(self) -> None:
        with self._cache_lock:
            data = {k: {"media_id": e.media_id, "uploaded_at": e.uploaded_at,
                        "digest": e.digest}
                    for k, e in self._cache.items()}
        try:
            self._cache_path().write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("表情包缓存写不进去：%s", exc)

    def _key(self, path: Path) -> str:
        return f"{self.namespace}:{path}" if self.namespace else str(path)

    def cached_media_id(self, path: Path) -> str | None:
        """缓存里有、没过期、且图片内容没变，才直接用。"""
        e = self._cache.get(self._key(path))
        if e is None:
            return None
        if time.time() - e.uploaded_at > _TTL_SECONDS:
            log.debug("%s 的 media_id 过期了，要重传", path.name)
            return None
        if e.digest and e.digest != _digest(path):
            log.info("%s 内容变了，要重传", path.name)
            return None
        return e.media_id

    def remember(self, path: Path, media_id: str) -> None:
        with self._cache_lock:
            self._cache[self._key(path)] = _Entry(media_id, time.time(), _digest(path))
        self._save_cache()

    def forget(self, path: Path) -> None:
        """发送被拒（media_id 失效）时调用，下次会重传。"""
        with self._cache_lock:
            gone = self._cache.pop(self._key(path), None) is not None
        if gone:
            self._save_cache()


def _digest(path: Path) -> str:
    """图片内容指纹，用来发现「文件换了但名字没变」。"""
    try:
        return hashlib.md5(path.read_bytes()).hexdigest()
    except Exception:
        return ""


def read_for_upload(path: Path) -> tuple[bytes, str, str]:
    """返回 (原始字节, md5, base64)。上传三步都要用。"""
    data = path.read_bytes()
    return data, hashlib.md5(data).hexdigest(), base64.b64encode(data).decode()
