"""
业务主流程，平台无关。

    收消息 -> 去重 -> 过滤 -> OCR -> 提纯 -> 规则 -> 模型 -> 判重 -> 回复 -> 战报 -> 记单

记单（进清单 / 排名 / 通报）【只认固定格式的文字晒单】，照片和口语报单只点赞不记。

线程划分是「响应快」的关键：
  平台回调线程  只做去重和几个布尔判断，全是微秒级，做完立刻返回
  线程池        OCR（3~5 秒）、模型调用（1~3 秒）、发送请求全在这里

OCR 和模型调用绝不能放在回调线程里 —— 会阻塞平台的事件循环（飞书）或者
拖慢下一轮轮询（企微），一条慢消息拖垮整个群。
"""

from __future__ import annotations

import io
import json
import re
import logging
import os
import threading
import time
from datetime import datetime
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import activity
from .assistant import NOT_A_QUESTION, Assistant, AssistantSettings, looks_like_question
from .config import ROOT, ConfigStore
from .distill import distill, distill_text, redact
from .image import preload, recognize
from .llm import LLMJudge, LLMSettings
from .campaign import (Campaign, NotRosterSheet, Roster, RosterImportError, build_order,
                       has_multiple_orders, looks_like_order, parse_order)
from .extract import ExtractSettings, FieldExtractor
from .ledger_tools import resolve_period
from .paths import DATA
from .checkin import CheckinRecorder
from .samples import SampleCollector
from .sendlog import SendLog
from .message import InboundMessage
from .platforms.base import Platform
from .stickers import StickerPool
from .tally import Tally

log = logging.getLogger("botcore.pipeline")


def _looks_like_xlsx(data: bytes) -> bool:
    """没文件名时按内容认 Excel：xlsx 是 zip，里面有 xl/workbook.xml。"""
    if data[:2] != b"PK":
        return False
    try:
        import zipfile
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return "xl/workbook.xml" in z.namelist()
    except Exception:
        return False


# 「9月25日的备份发我」-> 「9月25日」：去掉「备份」和客套话，剩下的交给 resolve_period 认日期
# 「备份」「json」都是要原始数据（JSON 备份包，只给管理员）；要看的报表说「表格」
_BACKUP_WORD = re.compile(r"备份|json", re.I)
_BACKUP_NOISE = re.compile(r"备份|json|的|要|发我|发给我|给我|发一下|一下|数据|包|文件|请|麻烦|[，,。.!！?？\s]+$", re.I)
# 「打卡表格 9.25 发我」-> 「9.25」：去掉报表类型和客套话，剩下的交给 resolve_period 认日期
_EXCEL_NOISE = re.compile(r"晒单|打卡|业绩|表格|excel|xlsx|清单|台账|报表|的|发给我|发我|发一下|发|要|给我|给|导出|"
                          r"一下|来一份|拿|请|麻烦|文件|[，,。!！?？\s]+", re.I)
# 「晒单表格发一下」：同时有「表格 / Excel / 清单」和「发 / 要 / 给 / 导出」
_EXCEL_WORD = re.compile(r"表格|excel|xlsx|清单|台账|报表", re.I)
_EXCEL_VERB = re.compile(r"发|要|给|导出|来一份|拿")


class _Dedupe:
    """
    平台会重推/重复拉到同一条消息，只处理一次。固定容量，O(1)。

    落盘到 dedupe.json（只留最近 24 小时的 msgid）：以前只在内存，重启后
    平台重投的消息会被再处理一次 —— 晒单被误报「重复」、口语报单二次点赞。
    """

    def __init__(self, capacity: int = 4096, path: Path | None = None,
                 ttl_seconds: float = 24 * 3600) -> None:
        self.capacity = capacity
        self.path = path
        self.ttl = ttl_seconds
        self._seen: OrderedDict[str, float] = OrderedDict()
        self._lock = threading.Lock()
        self._load()

    def is_new(self, key: str) -> bool:
        if not key:
            return True
        with self._lock:
            if key in self._seen:
                return False
            self._seen[key] = time.time()
            if len(self._seen) > self.capacity:
                self._seen.popitem(last=False)
            self._save()
            return True

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            cutoff = time.time() - self.ttl
            for k, ts in (raw or {}).items():
                if isinstance(ts, (int, float)) and ts >= cutoff:
                    self._seen[str(k)] = float(ts)
            if self._seen:
                log.info("去重记录：读回 %d 条最近 24 小时的消息 ID", len(self._seen))
        except Exception as exc:
            log.warning("去重记录读不了，当作空的：%s", exc)

    def _save(self) -> None:
        """在锁内调用。每条新消息写一次；文件几十 KB，几十毫秒一次，消息量小无所谓。"""
        if not self.path:
            return
        cutoff = time.time() - self.ttl
        try:
            self.path.write_text(json.dumps(
                {k: ts for k, ts in self._seen.items() if ts >= cutoff}), encoding="utf-8")
        except Exception as exc:
            log.warning("去重记录写不进去：%s", exc)


class Pipeline:
    def __init__(self, platform: Platform, store: ConfigStore, workers: int = 8) -> None:
        self.platform = platform
        self.store = store
        self.dedupe = _Dedupe(path=DATA / "dedupe.json")
        # 表情包图库，第一次用到时才建（没配就一直是 None）
        self._stickers: StickerPool | None = None
        self._sticker_key = None
        # 定时表情包。bot.py 起来后注入；没注入就只有点赞那一路
        self.automation = None
        # 业绩通报。同样由 bot.py 注入；没注入就不通报，清单照记
        self.bulletin = None
        # 活动台账。第一次记单时才建
        self._campaign: Campaign | None = None
        self._campaign_lock = threading.Lock()
        # 发送日志：记机器人说过什么，并给战报去重。跨天自动清。
        self.sendlog = SendLog()
        # 打卡照片样本采集（「样本 板夹」+ 照片），给有效打卡判定攒标注数据
        # 打卡核验：每张今日水印照片记一次打卡。要调视觉模型（几秒），单独一个线程慢慢做，不耽误点赞
        self.checkin = CheckinRecorder(store.checkin_settings, store.ocr_settings)
        self.samples = SampleCollector(platform, store.ocr_settings, judge=self.checkin.judge_blob)
        self.checkin_bulletin = None          # bot.py 里挂上（打卡通报）
        self._excel_last: dict[str, float] = {}  # 单聊要表格的冷却
        self.checkin_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="checkin")
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="work")
        # 图片单独一个池。OCR 是串行的（引擎不保证线程安全），一张 3~5 秒，
        # 有人连发几张就会占满 worker。分开之后纯文字消息不会排在图片后面。
        # OCR 并发。【内存敏感】：每个 worker 跑一张图峰值会涨 200~300 MB，
        # 2 个并发在小内存机器上很容易触顶被 OOM 杀掉（实测 1.6G 的机器
        # 配 MemoryMax=900M 时被杀了两次）。
        # 从环境变量读，小机器设 1，内存宽裕的可以设 2~4。
        self.ocr_pool = ThreadPoolExecutor(
            max_workers=int(os.getenv("OCR_WORKERS", "2") or 2),
            thread_name_prefix="ocr")
        self.tally = Tally(DATA / store.tally_settings().state_file)
        # LLMJudge 按配置对象缓存：config.yaml 热加载后 settings 是新对象，自动重建
        self._judge: LLMJudge | None = None
        self._judge_key: object = None
        # 问答助手，同样按配置对象缓存
        self._assistant: Assistant | None = None
        self._assistant_key: object = None
        # 晒单容错解析（严格解析失败时让模型抽字段）
        self._extractor: FieldExtractor | None = None
        self._extractor_key: object = None

    def warmup(self) -> None:
        """启动时预热 OCR 模型，省掉第一张图的等待。"""
        ocr = self.store.ocr_settings()
        if ocr.enabled:
            preload(ocr)

    # ---------- 入口 ----------

    def on_message(self, msg: InboundMessage) -> None:
        t0 = time.perf_counter()
        try:
            self._accept(msg, t0)
        except Exception:
            log.exception("接收消息时抛异常")

    def _accept(self, msg: InboundMessage, t0: float) -> None:
        """这里全是微秒级判断，做完立刻把重活丢进线程池。"""
        # 收到就打一行：排查「群里喊了没反应」时先分清是没送到还是被下面的门挡了
        log.info("收到消息 | %s | %s | 图%d 文件%d @我=%s 机器人=%s | %s",
                 msg.chat_type, msg.sender_name or msg.sender_id, len(msg.image_ids or ()),
                 len(msg.files or ()), msg.mentions_bot, msg.from_bot,
                 redact(msg.text or "")[:40].replace(chr(10), " "))
        if not self.dedupe.is_new(msg.msg_id):
            log.info("重复消息 %s，跳过", msg.msg_id)
            return

        settings, _matcher = self.store.snapshot()

        # 只放行真人发的消息：挡掉自己和其它机器人，杜绝互相触发的死循环
        if settings.ignore_self and msg.from_bot:
            return

        if msg.chat_type == "group" and not settings.enable_group:
            return
        if msg.chat_type == "p2p" and not settings.enable_p2p:
            return

        if msg.chat_type == "group" and settings.require_at and not msg.mentions_bot:
            return

        pool = self.ocr_pool if msg.has_image else self.pool
        try:
            pool.submit(self._process, msg, t0)
        except RuntimeError:
            # 线程池已关闭（进程正在退出）。安静丢弃即可 ——
            # 这条消息下次启动会被重叠窗重新拉到，不会丢。
            log.debug("正在退出，丢弃 %s", msg.msg_id)

    # ---------- 重活 ----------

    def _process(self, msg: InboundMessage, t0: float) -> None:
        try:
            _settings, matcher = self.store.snapshot()

            # 群里喊「通报」：直接出报表，不走规则和模型
            if self.bulletin is not None and self.bulletin.handle_command(msg):
                return
            # 群里喊「打卡通报」：发当前时段的打卡情况
            if self.checkin_bulletin is not None and self.checkin_bulletin.handle_command(msg):
                return
            # 管理员单聊「新活动 名称 10.1-10.4」「当前活动」「结束活动」
            if self._handle_activity_command(msg):
                return
            # 管理员单聊「备份」「备份 9.25」：发那天的数据备份包
            if self._handle_backup_request(msg):
                return
            # 「晒单表格发一下」「Excel 发我」：单聊直接发文件，群里提示去单聊要
            if self._handle_excel_request(msg):
                return
            # 打卡样本：「样本 板夹」+ 照片，只存不点赞不记单
            if self.samples.handle(msg):
                return
            # 文件消息只有一个用途：上传名册 Excel 更新分组
            if msg.has_file:
                self._handle_file(msg)
                return

            # 活动期外群里的晒单、打卡照片：不记、不点赞，回一句（09-28 用户定：活动外不照常）
            if self._off_activity_post(msg):
                return

            text, branch, stamp = self._collect_text(msg)
            if not text:
                return

            # 第一层：确定性规则。命中就回，不走模型。
            fired = matcher.match(text, msg.chat_id)
            if fired:
                for rule in fired:
                    log.info("命中规则 %s | chat=%s | %s", rule.name, msg.chat_id, text[:60])
                    # 规则命中会直接 return，不走模型那条路 ——
                    # 所以要在这里补记一笔，否则这单永远进不了清单和排名。
                    # 只有配了 count_as 的规则才记（「链路自检」那种就不记）。
                    # 【先记单再回复】：回复那一步网络抖一下抛异常，单也已经在清单里了
                    if getattr(rule, "count_as", ""):
                        self._record_rule_order(msg, text, rule)
                        # 山城卡也是业绩：让「冷清多久」和「扎堆」的计数把它算进去，
                        # 否则连发几张山城卡群里还会收到「全力以赴」
                        if self.automation is not None and self._counts(msg):
                            self.automation.activity.record()
                    self._reply(msg, rule.reply, rule.at_sender, rule.name, t0)
                return

            # @ 机器人 / 私聊 的纯文字，先当提问交给助手。
            # 助手看出来是报单（比如「@机器人 搞定一单199」）会退回来，继续走模型点赞
            if self._ask_assistant(msg, text, t0):
                return

            # 固定格式晒单：【先入清单，再问模型】。
            # 清单不能押在模型身上 —— 模型超时 / 限流 / 抽风时单照记；
            # 模型复核说「不是报单」也只是标黄不删（规则和模型互相兜底，见 _ask_agent）。
            if not msg.has_image and parse_order(msg.text or ""):
                if self._is_duplicate(msg, text, stamp):
                    return
                if not self._record_order(msg, text, {}):
                    log.info("和上一条是同一单，不重复点赞")
                    return
                self._ask_agent(msg, text, branch, stamp, t0, recorded=True)
                return

            # 第二层：规则没命中时才交给模型判断（照片、口语报单、写歪的晒单）
            self._ask_agent(msg, text, branch, stamp, t0)
        except Exception:
            log.exception("处理消息时抛异常 msg_id=%s", msg.msg_id)

    # ---------- 活动 ----------

    def _is_admin(self, msg: InboundMessage) -> bool:
        return bool(msg.sender_id) and msg.sender_id in set(self.store.backup_settings().admins or ())

    def _handle_activity_command(self, msg: InboundMessage) -> bool:
        """
        「新活动 秋促专项营销活动 10.1-10.4」「当前活动」「结束活动」。
        看当前活动谁都行；开 / 结束只给管理员，而且只在单聊（群里说了提示去单聊）。
        """
        text = (msg.text or "").strip()
        if msg.has_image or msg.has_file or not text or len(text) > 60:
            return False
        if activity._CMD_SHOW.match(text):
            act = activity.load()
            reply = activity.describe(act)
            if act is None and not activity.managed():
                reply += "（按 config 里的星期发通报）"
            reply += f"。名册 {len(self.campaign().roster.people)} 人"
            self.platform.reply(msg, reply, None)
            return True
        is_new, is_end = bool(activity._CMD_NEW.match(text)), bool(activity._CMD_END.match(text))
        if not (is_new or is_end):
            return False
        if msg.chat_type != "p2p":
            self.platform.reply(msg, "切换活动请管理员单聊我", msg.sender_id or None)
            return True
        if not self._is_admin(msg):
            log.warning("非管理员 %s 想切换活动，已拒绝：%s", msg.sender_id, text[:40])
            self.platform.reply(msg, "只有管理员能切换活动", None)
            return True
        if is_end:
            act = activity.load()
            if act is None:
                reply = "现在没有设置活动"
            elif act.status() != "进行中":
                reply = f"{activity.describe(act)}，不用再结束"
            else:
                act = activity.end_now(msg.sender_id)
                reply = (f"{activity.describe(act)}。从现在起群里的晒单、打卡不记录，通报和表情包都停了。"
                         f"开下一个活动发：新活动 名称 10.1-10.4")
            self.platform.reply(msg, reply, None)
            return True
        try:
            title, start, end = activity.parse_new(text)
        except ValueError as exc:
            self.platform.reply(msg, f"活动没切换：{exc}", None)
            return True
        # 切换前留底：打一份现在的数据备份
        try:
            from .backup import BACKUP_DIR, make_zip
            d = BACKUP_DIR / "切换活动前"
            make_zip(d / f"备份_{datetime.now():%Y-%m-%d_%H%M%S}.zip")
            for old in sorted(d.glob("备份_*.zip"))[:-10]:
                old.unlink()
        except Exception:
            log.exception("切换活动前备份失败（不影响切换）")
        act = activity.start_new(title, start, end, msg.sender_id, self.store.campaign_settings().title)
        self.store.checkin_settings()          # 让打卡表标题马上用新日期
        n = len(self.campaign().roster.people)
        self.platform.reply(msg, (
            f"已切换：{activity.describe(act)}。活动期间记晒单和打卡、发通报和表情包，活动外全停。"
            f"切换前已备份。现在名册 {n} 人，要换名册就接着单聊发新名册 Excel"), None)
        return True

    def _off_activity_post(self, msg: InboundMessage) -> bool:
        """群里、活动期外、像晒单或打卡（有图 / 晒单格式）-> 回一句不记，返回 True。"""
        if msg.chat_type == "p2p" or not activity.managed() or activity.is_active():
            return False
        text = msg.text or ""
        if not (msg.has_image or parse_order(text) or looks_like_order(text) >= 2):
            return False
        log.info("活动期外的晒单 / 打卡，不记：%s", redact(text)[:40].replace("\n", " ") or "（图片）")
        self.platform.reply(msg, activity.off_notice(), msg.sender_id or None)
        return True

    # ---------- 单聊取备份 ----------

    def _handle_backup_request(self, msg: InboundMessage) -> bool:
        """
        管理员单聊：「备份」（现在的）/「备份 9.25」/「9月25日的备份」/「昨天的备份」-> 发那天的备份包（zip）。
        只给 config.yaml 里 backup.admins 列的人 —— 台账是全员业绩明细。群里说「备份」不理，别把文件发到群里。
        """
        text = (msg.text or "").strip()
        if msg.has_image or msg.has_file or not _BACKUP_WORD.search(text) or len(text) > 20:
            return False
        if msg.chat_type != "p2p":
            return False
        cfg = self.store.backup_settings()
        if msg.sender_id not in set(cfg.admins or ()):
            log.warning("非管理员 %s 单聊要备份，已拒绝", msg.sender_id)
            self.platform.reply(msg, "备份只发给管理员", None)
            return True
        spec = _BACKUP_NOISE.sub("", text).strip() or "today"
        try:
            since, _until, label = resolve_period(spec)
        except Exception:
            self.platform.reply(msg, "没看懂要哪天的备份，这样发：备份 / 备份 9.25 / 昨天的备份", None)
            return True
        from .backup import archive_for, available_days
        day = datetime.strptime(since[:10], "%Y-%m-%d").date()
        path = archive_for(day)
        if path is None:
            days = available_days()[-7:]
            self.platform.reply(msg, f"没有 {day.month}.{day.day} 的备份。现有：" +
                                ("、".join(f"{int(d[5:7])}.{int(d[8:10])}" for d in days) or "还没有"), None)
            return True
        ok = hasattr(self.platform, "send_file") and self.platform.send_file(msg.chat_id, path)
        log.info("备份%s -> %s | %s", "已发" if ok else "发送失败", msg.sender_id, path.name)
        if not ok:
            self.platform.reply(msg, "备份发送失败了，稍后再试", None)
        return True

    # ---------- 要晒单表格 ----------

    def _handle_excel_request(self, msg: InboundMessage) -> bool:
        """
        单聊要 Excel 报表（09-28 用户定：和 JSON 备份分开，报表按天存档）：
          「晒单表格发我」-> 晒单（晒单数据 + 本周清单）   「打卡表格发我」-> 打卡（三时段）
          「表格发我」「Excel 发我」-> 两个都发          后面带日期（「打卡表格 9.25」）就发那天存档的
        群里要不发文件（清单有全员明细），提示单聊。以前这类话被当闲聊默默跳过（09-24 实测）。
        """
        text = (msg.text or "").strip()
        if msg.has_image or msg.has_file or not text or len(text) > 30:
            return False
        # 要有报表词，再加上「发 / 要 / 给」、日期（9.25、昨天）之一；「打卡表格」这种短句本身就是在要
        if not (_EXCEL_WORD.search(text) and (_EXCEL_VERB.search(text) or re.search(r"\d|今天|昨天|前天|本周|这周|上周", text)
                                              or len(text) <= 8)):
            return False
        if msg.chat_type != "p2p":
            self.platform.reply(msg, "表格不在群里发，请单聊我（或单聊「业绩助手」）说「晒单表格发我」「打卡表格发我」",
                                msg.sender_id or None)
            log.info("群里要表格，已提示去单聊")
            return True
        if not hasattr(self.platform, "send_file"):
            self.platform.reply(msg, "这会儿发不了表格，稍后再试", None)
            return True

        want_ck = "打卡" in text
        want_sales = bool(re.search(r"晒单|业绩|清单|台账", text))
        kinds = (["打卡"] if want_ck and not want_sales else
                 ["晒单"] if want_sales and not want_ck else ["晒单", "打卡"])
        spec = _EXCEL_NOISE.sub("", text).strip() or "today"
        try:
            _since, until, _label = resolve_period(spec)
        except Exception:
            self.platform.reply(msg, "没看懂要哪天的，这样发：晒单表格发我 / 打卡表格 9.25 / 昨天的表格", None)
            return True
        # 要的是一段（「上周的清单」）就取最后一天，那份的清单是整周的；不超过今天
        day = min(datetime.strptime(until[:10], "%Y-%m-%d").date(), datetime.now().date())

        key = f"{msg.chat_id}|{'+'.join(kinds)}|{day}"
        now = time.time()
        if now - self._excel_last.get(key, 0) < 30:
            log.info("同样的表格 30 秒内要过了，忽略")
            return True
        self._excel_last[key] = now

        from .archive import get_excel
        camp = self.campaign()
        camp.refresh_roster()
        for kind in kinds:
            try:
                path = get_excel(self.store, camp, kind, day)
            except Exception:
                log.exception("生成%s表格失败", kind)
                self.platform.reply(msg, f"{kind}表格生成失败了，稍后再试", None)
                continue
            if path is None:
                self.platform.reply(msg, f"没有{kind}表格（{kind}功能没开）", None)
                continue
            ok = self.platform.send_file(msg.chat_id, path)
            log.info("%s表格%s -> %s | %s", kind, "已发" if ok else "发送失败", msg.sender_id, path.name)
            if not ok:
                self.platform.reply(msg, f"{kind}表格发送失败了，稍后再试", None)
        return True

    # ---------- 问答助手 ----------

    def _ask_assistant(self, msg: InboundMessage, text: str, t0: float) -> bool:
        """
        群里 @ 机器人（或私聊）的纯文字消息交给问答助手。返回 True 表示已处理完。

        不处理的情况（返回 False，继续走点赞流程）：
          · 助手没开 / 没 @ 机器人（群里）/ 带图片 / 固定格式晒单
          · 助手判定这条其实是报单
        """
        cfg = self.store.assistant_settings()
        if not cfg.enabled or msg.has_image or not (msg.text or "").strip():
            return False
        if msg.chat_type == "group" and cfg.only_when_mentioned and not msg.mentions_bot:
            return False
        if parse_order(msg.text):
            return False          # 固定格式晒单，哪怕 @ 了机器人也是报单
        if cfg.question_only and not looks_like_question(msg.text):
            return False          # 不像提问（口语报单、写歪的晒单）直接走点赞流程

        assistant = self._get_assistant(cfg)
        if assistant is None:
            return False
        if not assistant.take_budget():
            log.warning("助手每分钟上限 %d 次已满，跳过：%s", cfg.max_calls_per_minute, text[:40])
            return False

        log.info("助手收到提问 | %s | %s", msg.sender_name or msg.sender_id, text[:60])
        answer = assistant.ask(msg.text.strip(), msg.chat_id, msg.sender_name)
        if answer is None:
            if cfg.error_reply:
                self.platform.reply(msg, cfg.error_reply, msg.sender_id if cfg.at_sender else None)
            return True
        if NOT_A_QUESTION in answer[:20]:
            # 模型偶尔多带个标点「#报单#。」，以前严格比较就把它原样发到群里了
            log.info("助手判定不是提问，转回点赞流程")
            return False
        ok = self.platform.reply(msg, answer, msg.sender_id if cfg.at_sender else None)
        if ok:
            self.sendlog.record(answer, "问答", msg.chat_id, to=msg.sender_name or "", dedupe=False)
            log.info("助手已回答，端到端耗时 %.0f ms | %s",
                     (time.perf_counter() - t0) * 1000, answer[:60].replace("\n", " "))
        return True

    def _get_assistant(self, cfg: AssistantSettings) -> Assistant | None:
        if self._assistant is not None and self._assistant_key is cfg:
            return self._assistant
        llm = self.store.llm_settings()
        api_key = os.getenv(cfg.api_key_env or llm.api_key_env, "").strip()
        if not api_key:
            log.error("assistant 已开启，但环境变量 %s 为空", cfg.api_key_env or llm.api_key_env)
            self._assistant, self._assistant_key = None, cfg
            return None
        self._assistant = Assistant(cfg, llm, api_key, self.campaign,
                                    self.store.campaign_settings, self.bulletin)
        self._assistant_key = cfg
        log.info("问答助手已启用：%s @ %s", self._assistant.model, self._assistant.base_url)
        return self._assistant

    def _collect_text(self, msg: InboundMessage) -> tuple[str, str, str]:
        """
        返回 (给后续流程用的文本, 支局, 判重键)。

        文字部分【不管有没有图】都先过 distill_text 脱敏 —— 企微 @机器人 带图时
        文字和图打包成一条（mixed），文字里常常就是带客户号码的晒单模板。
        以前只有纯文字那条路脱敏，图文混排的文字原样拼进模型请求和日志（审查踩出来的）。
        """
        text = msg.text or ""
        ocr_settings = self.store.ocr_settings()

        # 1) 文字：脱敏、抽支局、造判重键
        branch, stamp = "", ""
        if text:
            d = distill_text(text)
            text, branch, stamp = d.text, d.branch, d.stamp
            if not msg.image_ids and (branch or stamp):
                log.info("文字报单 | 支局=%s 判重键=%s | %s",
                         branch or "未识别", stamp or "无", text[:60].replace("\n", " "))

        if not msg.image_ids:
            return text, branch, stamp
        if not ocr_settings.enabled:
            return text, branch, stamp

        # 2) 图片：OCR + 提纯（只留业务字段和支局，抹掉手机号/身份证号）。
        #    既是隐私闸门（现场照可能拍到客户证件），也大幅提升判断准确率
        t = time.perf_counter()
        blobs: dict[str, bytes] = {}

        def download(image_id: str):
            blob = self.platform.download_image(msg, image_id)
            if blob:
                blobs[image_id] = blob
            return blob

        ocr = recognize(download, msg.image_ids, ocr_settings)
        dt = (time.perf_counter() - t) * 1000
        if blobs and self.store.checkin_settings().enabled and self._counts(msg):
            try:
                self.checkin_pool.submit(self.checkin.record, msg.msg_id, list(blobs.values()), msg.sender_id)
            except RuntimeError:
                pass              # 正在退出

        if not ocr:
            log.info("OCR %d 张图没识别出文字，耗时 %.0f ms", len(msg.image_ids), dt)
            return text, branch, stamp

        d = distill(ocr)
        log.info("OCR %d 张图，%d 字 -> 提纯 %d 字 [%s] 支局=%s 时间=%s，耗时 %.0f ms | %s",
                 len(msg.image_ids), len(ocr), len(d.text), d.source,
                 d.branch or "未识别", d.stamp or "未识别", dt, d.text[:60])
        # 支局 / 判重键：文字里写了的优先，没写再用照片水印的
        return f"{text} {d.text}".strip(), branch or d.branch, stamp or d.stamp

    # ---------- 模型 ----------

    def _ask_agent(self, msg: InboundMessage, text: str,
                   branch: str, stamp: str, t0: float, recorded: bool = False) -> None:
        """
        模型这一步。两种进法：
          recorded=False  照片 / 口语报单 / 写歪的晒单：模型判是不是报单，是才入清单、点赞
          recorded=True   固定格式晒单，【已经入清单】：模型只做两件事 —— 复核 + 写点赞话。
                          模型不可用就用固定话术点赞；模型说「不是报单」就不点赞，
                          但单留着标黄「请核对」，不删。规则和模型互为兜底，哪边失手都不丢单。
        """
        settings = self.store.llm_settings()
        judge = self._get_judge(settings) if settings.enabled else None
        result = None

        if judge is not None:
            # 本地粗筛，零成本。绝大多数闲聊在这里就被丢掉，不产生 API 调用。
            # 固定格式晒单不用筛 —— 它已经确定是单了
            if not recorded and not judge.prefilter(text):
                return
            if judge.take_budget():
                log.info("%s，交给模型 | chat=%s | %s",
                         "固定格式晒单" if recorded else "粗筛命中", msg.chat_id, text[:60])
                result = judge.judge(text)
            else:
                log.warning("模型调用已达每分钟上限 %d 次%s：%s",
                            settings.max_calls_per_minute,
                            "，固定格式晒单用固定话术" if recorded else "，跳过", text[:40])
                if not recorded:
                    return
        elif not recorded:
            return          # 模型没开，照片 / 口语报单没人能判，照旧不回

        if recorded:
            order = self.campaign().find(msg.msg_id)
            if result is None:
                reply = settings.fallback_reply or "点赞！！！👍👍"
                log.warning("模型不可用，固定格式晒单按固定话术点赞：%s", text[:40])
            elif not result["is_report"]:
                # 规则认成单、模型说不是：不点赞，单留着标黄让人看一眼
                self.campaign().flag(msg.msg_id, "模型认为不是晒单，请核对")
                log.warning("固定格式晒单被模型判为「不是报单」，已标待核对不点赞：%s", text[:40])
                return
            else:
                reply = result["reply"] or settings.fallback_reply or "点赞！！！👍👍"
            # 战报的计数以解析出来的单为准，比模型数的准
            result = {
                "is_report": True,
                "is_new": (order.kind == "新增") if order else bool(result and result.get("is_new")),
                "package": order.fee if order else "",
                "broadband_count": order.broadband if order else 0,
                "high_count": order.high if order else 0,
                "reply": reply,
            }
        else:
            if result is None:
                # 默认跳过 —— 漏回一条业绩，好过给报障消息回一句「点赞」
                if settings.on_error == "fallback" and settings.fallback_reply:
                    log.warning("模型不可用，使用兜底回复")
                    self._reply(msg, settings.fallback_reply, settings.at_sender, "兜底回复", t0)
                else:
                    log.warning("模型不可用，跳过本条：%s", text[:40])
                return

            if not result["is_report"]:
                log.info("模型判定「不是报单」，跳过：%s", text[:40])
                return

            if not result["reply"]:
                log.warning("模型判定是报单但没给回复语，跳过：%s", text[:40])
                return

            # 判重要在点赞之前拦 —— 重复的既不点赞也不计数，只发警告
            if self._is_duplicate(msg, text, stamp):
                return

            # 入清单（写歪的晒单走容错解析）。放在判重【之后】—— 重复的单不该进清单。
            # 返回 False 表示这条和刚才那条其实是同一单（已合并），
            # 那就不再重复点赞、也不再播一次战报。
            if not self._record_order(msg, text, result):
                log.info("和上一条是同一单，不重复点赞")
                return
            reply = result["reply"]

        label = f"{'晒单' if recorded else '模型'}·{'新增' if result['is_new'] else '业绩'}{result['package'] or ''}"
        self._reply(msg, reply, settings.at_sender, label, t0)
        cfg_st = self.store.sticker_settings()
        praise_n = cfg_st.praise_count
        streak = False
        if self.automation is not None and self._counts(msg):
            # 顺序要紧：先把这条记进活跃度，判断才把它算在内 ——
            # 否则第 4 条业绩会被当成第 3 条，捷报晚一条才发
            self.automation.activity.record()
            streak = self.automation.streak_pending(cfg_st)
            if streak:
                praise_n = cfg_st.praise_count_on_streak

        self._send_stickers(msg, "点赞", praise_n)

        # 捷报要紧跟在点赞后面，所以在这里同步触发，
        # 不交给定时线程 —— 那样会错位半分钟，看着像两件不相干的事
        if streak:
            self.automation.maybe_streak(msg.chat_id, cfg_st)
        self._broadcast(msg, result, branch)

    def _get_judge(self, settings: LLMSettings) -> LLMJudge | None:
        if self._judge is not None and self._judge_key is settings:
            return self._judge

        api_key = os.getenv(settings.api_key_env, "").strip()
        if not api_key:
            log.error("agent 已开启，但环境变量 %s 为空。请在 .env 里填入模型 API Key",
                      settings.api_key_env)
            self._judge, self._judge_key = None, settings
            return None

        self._judge = LLMJudge(settings, api_key)
        self._judge_key = settings
        log.info("模型判断已启用：%s @ %s", settings.model, settings.base_url)
        return self._judge

    # ---------- 活动台账 ----------

    def campaign(self) -> Campaign:
        # 两个 worker 同时首次记单会建出两个 Campaign，其中一个的单在内存和盘上都丢。加锁
        if self._campaign is None:
            with self._campaign_lock:
                if self._campaign is None:
                    self._campaign = Campaign(Roster(), self.store.campaign_settings().targets)
        return self._campaign

    def _record_rule_order(self, msg: InboundMessage, text: str, rule) -> None:
        """
        规则命中时记一笔 —— 但只记【固定格式的文字晒单】。

        山城卡规则会先于模型截住「办理套餐：19山城卡」那种晒单，
        所以这里也得走一遍解析，否则这单永远进不了清单。
        照片或随手打的「山城卡一张」命中规则只回复，不记。
        """
        try:
            self._record_text_order(msg)
        except Exception:
            log.exception("规则记单失败（不影响回复）")

    def _record_order(self, msg: InboundMessage, text: str, result: dict) -> bool:
        """
        模型判定是报单之后记一笔。返回 False 表示这条和已有的是同一单（已合并），
        调用方据此不重复点赞。

        【清单只收固定格式的文字晒单】（「业务晒单时刻」模板），不收照片：
          · 照片只有「工作内容」一行，云盒/千兆/FTTR/积分全拿不到，
            记进去也是一整行待补，排名跟着虚
          · 大家发照片是为了打卡和点赞，业绩以晒单文字为准
        所以照片和口语报单：点赞、表情包、战报照常，但不进清单、不触发跟单通报。
        """
        try:
            return self._record_text_order(msg)
        except Exception:
            log.exception("入清单失败（不影响点赞）")
            return True

    def _record_text_order(self, msg: InboundMessage) -> bool:
        """
        解析固定格式晒单并写进台账。不是那种格式就直接放过（返回 True）。

        揽收人：晒单里有明确字段就用它；没填才退回消息的发送者。
        同一单贴两遍（业务号码相同）会合并，不重复计数。
        """
        if not self._counts(msg):
            log.info("单聊消息只回复，不入清单")
            return True
        at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        order = parse_order(msg.text or "", msg_id=msg.msg_id, at=at)
        if order is None:
            order = self._fuzzy_parse(msg, at)
        if order is None:
            if msg.text:
                log.info("不是固定格式的晒单，不入清单：%s", redact(msg.text)[:40].replace("\n", " "))
            return True

        if not order.collector:
            order.collector = msg.sender_name or ""
        order.sender_id = msg.sender_id or ""

        # 同一单重发（同人同单号 / 同号码同类型）：后发的覆盖先发的，不计两次。
        # 回一句让发的人知道更正生效了；不重复点赞、不重复播战报
        camp = self.campaign()
        existing = camp.find_mergeable(order)
        if existing is not None:
            camp.merge_into(existing, order)
            tag = order.serial or f"{order.kind} {order.fee}"
            log.info("和 %s 那条是同一单，已按新发的覆盖 | %s", existing.at[11:16], tag)
            self.platform.reply(msg, f"已按最新一条更新（{tag}）", msg.sender_id or None)
            return False

        if camp.looks_duplicate(order):
            order.needs_review = True
            order.note = (order.note + " 疑似重复，请核对").strip()

        if camp.add(order):
            log.info("已入清单 | %s %s | 揽收人=%s 战区=%s%s",
                     order.kind, order.fee, order.collector,
                     order.team or "未归队",
                     "（待核对）" if order.needs_review else "")
            self._notify_order(msg)
            return True
        return False

    def _fuzzy_parse(self, msg: InboundMessage, at: str):
        """
        严格解析失败的晒单，让模型抽字段再走同一套 build_order。

        只对「像晒单」的文字试（命中 ≥ min_field_hits 个模板字段名），
        随手打的「搞定一单199」不会进来 —— 那种没有揽收人和积分，记了也是空行。
        抽出来的单一律标待核对，Excel 里标黄。
        """
        cfg = self.store.extract_settings()
        text = msg.text or ""
        if not cfg.enabled or not text.strip():
            return None
        hits = looks_like_order(text)
        if hits < cfg.min_field_hits:
            return None
        if has_multiple_orders(text):
            return None       # 多单不拆，也别浪费一次模型调用
        extractor = self._get_extractor(cfg)
        if extractor is None or not extractor.take_budget():
            return None
        log.info("严格解析失败但像晒单（命中 %d 个字段名），交给模型抽字段", hits)
        fields = extractor.extract(text)
        if not fields:
            return None
        order = build_order(fields, text, msg_id=msg.msg_id, at=at,
                            source="文字晒单·模型解析")
        if order is None:
            return None
        order.needs_review = True
        order.note = ("模型解析待核对 " + order.note).strip()
        log.info("模型抽出字段 | %s %s | 揽收人=%s 支局=%s 积分=%s",
                 order.kind, order.fee, order.collector or "?", order.team or "?", order.points)
        return order

    def _get_extractor(self, cfg: ExtractSettings) -> FieldExtractor | None:
        if self._extractor is not None and self._extractor_key is cfg:
            return self._extractor
        llm = self.store.llm_settings()
        api_key = os.getenv(cfg.api_key_env or llm.api_key_env, "").strip()
        if not api_key:
            log.error("extract 已开启，但环境变量 %s 为空", cfg.api_key_env or llm.api_key_env)
            self._extractor, self._extractor_key = None, cfg
            return None
        self._extractor = FieldExtractor(cfg, llm, api_key)
        self._extractor_key = cfg
        return self._extractor

    def _notify_order(self, msg: InboundMessage) -> None:
        """记了一单新的，告诉通报调度器（它自己管一小时一次的节流）。"""
        if self.bulletin is not None:
            try:
                self.bulletin.on_order(msg.chat_id)
            except Exception:
                log.exception("触发跟单通报出错（不影响记单）")

    # ---------- 名册更新 ----------

    def _handle_file(self, msg: InboundMessage) -> None:
        """
        群里上传的 Excel -> 更新名册。

        只认 .xlsx / .xls，且表里得有「姓名 / 分组 / 战区」表头；
        不是这种表（比如随手发的别的文件）就静默跳过，不打扰群。
        """
        if not hasattr(self.platform, "download_file"):
            return
        excel = (".xlsx", ".xlsm", ".xls")
        for file_id, name in msg.files:
            # 有名字且明显不是 Excel 的，不用下载
            if name and not name.lower().endswith(excel):
                log.info("收到文件 %s，不是 Excel，跳过", name)
                continue
            # 企微的文件消息【不带文件名】，名字只有下载时才拿得到。以前没名字就直接跳过，
            # 单聊发名册 Excel 永远不生效、日志里还什么都没有（09-24 实测踩到）
            if hasattr(self.platform, "download_file_named"):
                data, got = self.platform.download_file_named(msg, file_id)
                name = name or got
            else:
                data = self.platform.download_file(msg, file_id)
            if not data:
                log.warning("文件 %s 下载失败", name or "（无文件名）")
                continue
            if not (name.lower().endswith(excel) or _looks_like_xlsx(data)):
                log.info("收到文件 %s，不是 Excel，跳过", name or "（无文件名）")
                continue
            name = name or "（无文件名的 Excel）"
            camp = self.campaign()
            if not self._is_admin(msg):
                # 名册只给管理员更新（09-28）：别人发的先看是不是名册表，是才提示，不是就当普通文件不理
                try:
                    camp.roster.import_workbook(data, dry_run=True)
                except NotRosterSheet:
                    log.info("文件 %s 不是名册表，跳过", name)
                    continue
                except Exception:
                    pass
                log.warning("非管理员 %s 发了名册 %s，没导入", msg.sender_id, name)
                self.platform.reply(msg, "名册只能管理员更新，请把名册发给管理员", msg.sender_id or None)
                continue
            log.info("收到 Excel %s（%d 字节），按名册导入", name, len(data))
            try:
                summary = camp.roster.import_workbook(data)
            except NotRosterSheet as exc:
                # 不是名册表就算了 —— 群里发别的 Excel 很正常
                log.info("文件 %s 不是名册表，跳过：%s", name, exc)
                continue
            except RosterImportError as exc:
                # 是名册表但有问题：必须说，否则发的人以为更新成功了
                log.warning("名册 %s 没更新：%s", name, exc)
                self.platform.reply(msg, f"名册没更新：{exc}", msg.sender_id or None)
                continue
            except Exception as exc:
                log.exception("解析名册 %s 出错", name)
                self.platform.reply(msg, f"名册没更新成：{exc}", msg.sender_id or None)
                continue
            moved = camp.reassign()
            text = f"名册已更新：{summary}"
            if moved:
                text += f"，已有 {moved} 单按新分组重新归队"
            log.info("%s（来自 %s）", text, name)
            self.platform.reply(msg, text, msg.sender_id or None)

    # ---------- 表情包 ----------

    def _pool(self) -> StickerPool | None:
        """图库单例。配置热更新后会重建。"""
        cfg = self.store.sticker_settings()
        if not cfg.enabled:
            return None
        if not hasattr(self.platform, "send_stickers"):
            # 平台不支持（比如飞书）就跳过 —— 用 hasattr 判断，
            # 免得为了一个可选功能给所有平台都加个空方法
            return None
        if self._stickers is None or self._sticker_key is not cfg:
            self._stickers = StickerPool(cfg, namespace=self.platform.name)
            self._sticker_key = cfg
        return self._stickers

    def _send_stickers(self, msg: InboundMessage, category: str, count: int) -> int:
        """发某个分类的表情包。发不出去不影响主流程，只记日志。"""
        pool = self._pool()
        if pool is None or not pool.has(category):
            return 0
        try:
            return self.platform.send_stickers(msg.chat_id, pool, category, count)
        except Exception:
            log.exception("发表情包「%s」出错（不影响点赞）", category)
            return 0

    # ---------- 判重 / 战报 ----------

    @staticmethod
    def _counts(msg: InboundMessage) -> bool:
        """
        这条消息算不算数（战报 / 判重 / 清单 / 打卡 / 活跃度）。
        单聊是测试和样本通道，只回复不计数 —— 09-24 同事在单聊里发了一遍测试照片，
        占掉了判重的位，群里正式发的反倒被判「重复」。
        """
        return msg.chat_type != "p2p"

    def _is_duplicate(self, msg: InboundMessage, text: str, stamp: str) -> bool:
        """水印时间 + 工作内容都一致就算重复。命中则发警告并返回 True。"""
        cfg = self.store.dedupe_settings()
        if not cfg.enabled or not stamp or not self._counts(msg):
            # 时间抽不到就没法判重，放行。纯文字消息也走这条。单聊是测试通道，不占判重的位
            return False

        if self.tally.claim(f"{stamp}|{text}"):
            return False

        log.warning("重复内容，不计数 | 时间=%s | %s", stamp, text[:60])
        warn = cfg.template.format(stamp=stamp)
        self.platform.reply(msg, warn, msg.sender_id or None)
        self.sendlog.record(warn, "判重警告", msg.chat_id,
                            to=msg.sender_name or "", dedupe=False)
        return True

    def _broadcast(self, msg: InboundMessage, result: dict, branch: str) -> None:
        """新增单才计数播报。续约、升档、预约不计。"""
        cfg = self.store.tally_settings()
        if not cfg.enabled or not result["is_new"] or not self._counts(msg):
            return

        if not branch:
            # 支局没识别出来就不播 —— 播一个没有支局名的战报没意义，
            # 而且会让这个支局当天的计数对不上
            log.info("是新增单但没识别出支局，跳过战报")
            return

        # 【不要把 0 当成「模型没给」】。
        # 战报统计的是【新增宽带】和【新增中高套】这两项，不是所有业绩。
        # 号卡类的单（比如「19山城卡」）确实是新增，但两项都该是 0 ——
        # 以前这里写 `or 1` 兜底，结果把号卡单播成了「新增宽带破1」，是错的。
        # 模型有完整信息，这里就信它。
        add_bb = int(result.get("broadband_count") or 0)
        add_high = int(result.get("high_count") or 0)
        if add_bb == 0 and add_high == 0:
            log.info("是新增单但不涉及宽带/中高套（%s），只点赞不播战报",
                     result.get("package") or "未识别套餐")
            return

        counts = self.tally.record(branch, add_bb, add_high)

        # 只播当天累计【大于 0】的项。
        # 否则会出现「新增中高套破"0"」这种读起来很怪的战报。
        parts = []
        if counts.broadband > 0:
            parts.append(cfg.part_broadband.format(count=counts.broadband))
        if counts.high > 0:
            parts.append(cfg.part_high.format(count=counts.high))
        if not parts:
            # 两项都是 0 就没什么可播的（正常不会走到，record 至少加了 1）
            log.info("战报 | %s 当天累计为 0，不播", branch)
            return

        text = cfg.template.format(
            branch=branch, parts=cfg.part_sep.join(parts),
            broadband=counts.broadband, high=counts.high)

        # 【战报去重】同样的话一天只发一次。
        # 实际见过「恭喜东林支局新增宽带破1、新增中高套破1」连发两遍，
        # 群里看着像机器人卡了。计数照常累加，只是不重复播。
        if self.sendlog.is_duplicate(text, "战报"):
            log.info("战报内容和今天发过的一样，不重复播：%s", text[:40])
            return

        log.info("战报 | %s 本单 +宽带%d +中高套%d，当日累计 宽带%d 中高套%d",
                 branch, add_bb, add_high, counts.broadband, counts.high)
        # 战报【不 @ 人】：它播的是整个支局当天的累计，是发给全群看的，
        # 不是对某个人的回应。@ 上去反而像在单独通知他一个人。
        # （对个人的点赞和判重警告仍然带 @，那两条确实是冲着发单人去的。）
        self.platform.reply(msg, text, None)
        self.sendlog.record(text, "战报", msg.chat_id, dedupe=True)

    # ---------- 发送 ----------

    def _reply(self, msg: InboundMessage, text: str, at_sender: bool,
               label: str, t0: float) -> None:
        ok = self.platform.reply(msg, text, msg.sender_id if at_sender else None)
        if ok:
            # 【不去重】：两个人各报一单 139，模型很可能生成一样的回复语，
            # 按内容去重的话第二个人就收不到回复了。只记日志。
            self.sendlog.record(text, "点赞", msg.chat_id,
                                to=msg.sender_name or "", dedupe=False)
            log.info("已回复 %s，端到端耗时 %.0f ms", label, (time.perf_counter() - t0) * 1000)

    def shutdown(self, timeout: float = 5.0) -> None:
        """
        关线程池。【不死等】：还没开始的任务直接取消，在途的最多等 timeout 秒。

        原来是 wait=True 无限等 —— 一张图的 OCR + 大模型可能跑 60 秒以上，
        队列里再排几条，Ctrl+C 之后要等好几分钟才退，用起来像卡死了。
        没做完的活不会丢：下次启动重叠窗会把这些消息重新拉到。
        """
        # cancel_futures：把还没开始跑的任务直接扔掉，不再排队执行
        for pool in (self.pool, self.ocr_pool):
            pool.shutdown(wait=False, cancel_futures=True)
        log.info("已取消排队任务，等在途任务收尾（最多 %.0f 秒）", timeout)
