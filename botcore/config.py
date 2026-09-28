"""配置加载：.env 读凭证和密钥，config.yaml 读业务规则，支持热加载。

平台凭证不在这里读 —— 各平台适配器自己从环境变量取（见 platforms/*.from_env），
这样加新平台不用动这个文件。这里只负责把 .env 加载进进程环境。
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass, replace
from pathlib import Path

import yaml
from dotenv import load_dotenv

from . import activity
from .assistant import AssistantSettings
from .bulletin import BulletinSettings
from .backup import BackupSettings
from .campaign import CampaignSettings
from .checkin import CheckinSettings
from .extract import ExtractSettings
from .image import OCRSettings
from .llm import LLMSettings
from .stickers import StickerSettings
from .tally import DedupeSettings, TallySettings
from .matcher import Matcher, Rule

from .paths import ROOT, DATA, CONFIG_PATH   # noqa: F401  转出去给其它模块用


@dataclass
class GlobalSettings:
    # 用哪个平台：feishu | wecom
    platform: str = "feishu"
    require_at: bool = True
    enable_p2p: bool = True
    enable_group: bool = True
    ignore_self: bool = True
    default_cooldown: float = 3.0
    hot_reload: float = 5.0
    match_strategy: str = "first"


def load_env() -> str:
    """把 .env 加载进进程环境，返回日志级别。

    平台凭证不在这里读 —— 各平台适配器自己从环境变量取
    （见 platforms/feishu.py 和 platforms/wecom.py 的 from_env），
    这样加新平台不用动这个文件。
    """
    load_dotenv(DATA / ".env")
    return os.getenv("LOG_LEVEL", "INFO").strip().upper()


@dataclass
class Bundle:
    """config.yaml 解析出来的全部设置。加新段就在这里加一个字段。"""
    settings: GlobalSettings
    matcher: Matcher
    llm: LLMSettings
    ocr: OCRSettings
    tally: TallySettings
    dedupe: DedupeSettings
    stickers: StickerSettings
    campaign: CampaignSettings
    bulletin: BulletinSettings
    assistant: AssistantSettings
    extract: ExtractSettings
    checkin: CheckinSettings
    backup: BackupSettings


def _as_list(v, where: str) -> list:
    """keywords / exclude / prefilter 写成字符串时 Rule 会按单个汉字逐字匹配，对几乎所有消息都命中。"""
    if v is None:
        return []
    if isinstance(v, str):
        raise ValueError(f"config.yaml 里 {where} 要写成列表（- 一项一行 或 [a, b]），现在是字符串：{v!r}")
    return list(v)


def _as_hhmm(v, where: str):
    """
    HH:MM 不加引号会被 YAML 按六十进制解析成整数：11:50 -> 710、21:00 -> 1260。
    定点通报 / 定时表情包就全部对不上时刻。这里转回来，顺便在日志里提醒。
    """
    if isinstance(v, int) and not isinstance(v, bool):
        hhmm = f"{v // 60:02d}:{v % 60:02d}"
        logging.getLogger("botcore.config").warning(
            "config.yaml 里 %s 写成了 %s（没加引号被当成数字），按 %s 处理。请加引号：\"%s\"",
            where, v, hhmm, hhmm)
        return hhmm
    return v


def _build(raw: dict) -> Bundle:
    g = GlobalSettings(**{k: v for k, v in (raw.get("global") or {}).items()
                          if k in GlobalSettings.__dataclass_fields__})

    rules: list[Rule] = []
    for item in raw.get("rules") or []:
        item = dict(item)
        item.setdefault("cooldown", g.default_cooldown)
        for key in ("keywords", "exclude"):
            if key in item:
                item[key] = _as_list(item[key], f"rules[{item.get('name')}].{key}")
        known = {k: v for k, v in item.items() if k in Rule.__dataclass_fields__ and not k.startswith("_")}
        rules.append(Rule(**known))

    agent_raw = dict(raw.get("agent") or {})
    if "prefilter" in agent_raw:
        agent_raw["prefilter"] = _as_list(agent_raw["prefilter"], "agent.prefilter")
    llm = LLMSettings(**{k: v for k, v in agent_raw.items()
                         if k in LLMSettings.__dataclass_fields__})

    ocr = OCRSettings(**{k: v for k, v in (raw.get("ocr") or {}).items()
                         if k in OCRSettings.__dataclass_fields__})

    tally = TallySettings(**{k: v for k, v in (raw.get("tally") or {}).items()
                             if k in TallySettings.__dataclass_fields__})

    dedupe = DedupeSettings(**{k: v for k, v in (raw.get("dedupe") or {}).items()
                               if k in DedupeSettings.__dataclass_fields__})

    st_raw = dict(raw.get("stickers") or {})
    for key in ("active_start", "active_end"):
        if key in st_raw:
            st_raw[key] = _as_hhmm(st_raw[key], f"stickers.{key}")
    st_raw["daily"] = [dict(d, at=_as_hhmm(d.get("at"), "stickers.daily.at"))
                       for d in (st_raw.get("daily") or []) if isinstance(d, dict)] or None
    if st_raw["daily"] is None:
        st_raw.pop("daily")
    stickers = StickerSettings(**{k: v for k, v in st_raw.items()
                                  if k in StickerSettings.__dataclass_fields__})

    campaign = CampaignSettings.from_dict(raw.get("campaign") or {})

    bl_raw = dict(raw.get("bulletin") or {})
    if "fixed_times" in bl_raw:
        bl_raw["fixed_times"] = [_as_hhmm(t, "bulletin.fixed_times") for t in (bl_raw["fixed_times"] or [])]
    bulletin = BulletinSettings(**{k: v for k, v in bl_raw.items()
                                   if k in BulletinSettings.__dataclass_fields__})

    assistant = AssistantSettings(**{k: v for k, v in (raw.get("assistant") or {}).items()
                                     if k in AssistantSettings.__dataclass_fields__})

    extract = ExtractSettings(**{k: v for k, v in (raw.get("extract") or {}).items()
                                 if k in ExtractSettings.__dataclass_fields__})

    checkin_raw = raw.get("checkin") or {}
    checkin = CheckinSettings(**{k: v for k, v in checkin_raw.items()
                                 if k in CheckinSettings.__dataclass_fields__})
    checkin.keep_as_is = _as_list(checkin.keep_as_is, "checkin.keep_as_is")
    checkin.bulletin_times = [_as_hhmm(t, "checkin.bulletin_times")
                              for t in _as_list(checkin.bulletin_times, "checkin.bulletin_times")]
    checkin.bulletin_days = _as_list(checkin.bulletin_days, "checkin.bulletin_days")
    checkin.bulletin_commands = _as_list(checkin.bulletin_commands, "checkin.bulletin_commands")

    backup = BackupSettings(**{k: v for k, v in (raw.get("backup") or {}).items()
                               if k in BackupSettings.__dataclass_fields__})
    backup.admins = [str(a) for a in _as_list(backup.admins, "backup.admins")]
    for slot in checkin.slots or ():
        for k in ("start", "end"):
            slot[k] = _as_hhmm(slot.get(k, ""), f"checkin.slots.{slot.get('name', '')}.{k}")

    if not rules and not llm.enabled:
        raise ValueError("config.yaml 里既没有启用的规则，agent 也没开启")

    return Bundle(g, Matcher(rules, strategy=g.match_strategy), llm, ocr, tally,
                  dedupe, stickers, campaign, bulletin, assistant, extract, checkin, backup)


class ConfigStore:
    """线程安全地持有当前配置，并在 config.yaml 变更时热加载。"""

    def __init__(self, path: Path = CONFIG_PATH) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._mtime = 0.0
        self._overlay: dict = {}
        self._bundle = self._read()
        self._mtime = self.path.stat().st_mtime

    def _read(self) -> Bundle:
        if not self.path.exists():
            raise SystemExit(f"找不到配置文件：{self.path}")
        with self.path.open(encoding="utf-8") as f:
            return _build(yaml.safe_load(f) or {})

    # 各段的读取都返回【同一个对象】直到下次热加载 —— 调用方可以拿对象
    # 身份当缓存键（pipeline 里 LLMJudge / StickerPool 就是这么做的）

    def snapshot(self) -> tuple[GlobalSettings, Matcher]:
        with self._lock:
            return self._bundle.settings, self._bundle.matcher

    def llm_settings(self) -> LLMSettings:
        with self._lock:
            return self._bundle.llm

    def ocr_settings(self) -> OCRSettings:
        with self._lock:
            return self._bundle.ocr

    def tally_settings(self) -> TallySettings:
        with self._lock:
            return self._bundle.tally

    def sticker_settings(self) -> StickerSettings:
        with self._lock:
            return self._bundle.stickers

    def dedupe_settings(self) -> DedupeSettings:
        with self._lock:
            return self._bundle.dedupe

    def _with_activity(self, key: str, base, **fields):
        """
        data/activity.json 里的当前活动盖到配置上（活动名 -> campaign.title，活动日期 -> checkin.span）。
        按（配置对象, 活动文件 mtime）缓存：没变就返回同一个对象，别的地方拿对象身份当缓存键不受影响。
        """
        stamp = (id(base), activity.mtime())
        hit = self._overlay.get(key)
        if hit and hit[0] == stamp:
            return hit[1]
        out = replace(base, **fields) if fields else base
        self._overlay[key] = (stamp, out)
        return out

    def campaign_settings(self) -> CampaignSettings:
        with self._lock:
            base = self._bundle.campaign
        act = activity.load()
        return self._with_activity("campaign", base, **({"title": act.title} if act and act.title else {}))

    def bulletin_settings(self) -> BulletinSettings:
        with self._lock:
            return self._bundle.bulletin

    def assistant_settings(self) -> AssistantSettings:
        with self._lock:
            return self._bundle.assistant

    def extract_settings(self) -> ExtractSettings:
        with self._lock:
            return self._bundle.extract

    def checkin_settings(self) -> CheckinSettings:
        with self._lock:
            base = self._bundle.checkin
        act = activity.load()
        return self._with_activity("checkin", base, **({"span": act.span()} if act else {}))

    def backup_settings(self) -> BackupSettings:
        with self._lock:
            return self._bundle.backup

    def maybe_reload(self) -> bool:
        """文件 mtime 变了就重新解析。解析失败保留旧配置，返回 False。"""
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return False
        if mtime == self._mtime:
            return False
        try:
            bundle = self._read()
        except Exception:
            self._mtime = mtime          # 别对同一份坏配置反复报错
            raise
        with self._lock:
            self._bundle = bundle
            self._mtime = mtime
        return True
