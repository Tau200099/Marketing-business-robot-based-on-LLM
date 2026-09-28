"""
业绩通报：把活动台账变成「一段文字 + 一张表格图 + 一个 Excel」发到群里。

三种触发（config.yaml 的 bulletin 段）：
    1) 跟着晒单 —— 有新单入清单就通报，但【一小时最多一次】。
       间隔没到的那些单不会丢：记一个「欠着」标记，间隔一到就补发一次，
       这样一波连发的最后几单也能出现在通报里。
    2) 定点 —— 11:50 和 21:00 各发一次（上午收工、全天收工的节点汇总）。
       是【固定】的：就算 11:20 刚跟着晒单发过，11:50 也照发。
    3) 群里喊「通报」—— 立刻发一份到当前群，不受间隔限制。

三样东西的分工：
    文字   手机上一眼能看的结论：前两名恭喜、末两名加油
    图片   整张「晒单数据」表，不用点开文件就能看明细
    Excel  留底 + 人工补字段（图片单缺的云盒/积分）

图片全程本地用 Pillow 画，不依赖 Excel / LibreOffice —— 服务器上没有那些。
需要一个中文字体，找不到会退化成方块，preflight 会提前查。
"""

from __future__ import annotations

import io
import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

from . import activity
from .campaign import Campaign, CampaignSettings, GroupStat, Targets, TeamStat
from .paths import DATA, ROOT
from .checkin import day_sheets
from .report import build_workbook, summary_title

log = logging.getLogger("botcore.bulletin")


@dataclass
class BulletinSettings:
    enabled: bool = False
    # 定点通报时刻（HH:MM），到点后 10 分钟内都算数
    fixed_times: tuple = ("11:50", "21:00")
    # 哪几天发。ISO 星期：周一=1 … 周日=7
    days: tuple = (1, 2, 3, 4, 5, 6, 7)
    # 有新单就跟着通报
    follow_orders: bool = True
    # 跟着晒单的通报，两次之间至少隔这么久
    min_interval_minutes: int = 60
    # 群里发这些词，立刻通报一次到当前群
    commands: tuple = ("通报", "看通报", "看排名", "排名", "清单")
    # 命令触发的最小间隔（秒），防止有人连喊刷屏
    command_cooldown: int = 60
    # 发哪几样
    with_text: bool = True
    with_image: bool = True
    with_excel: bool = True
    # 生成的文件放哪（相对数据目录）
    out_dir: str = "reports"
    state_file: str = "bulletin_state.json"

    # ---- 文字话术 ----
    # {day}=8.30  {title}=活动名  {time}=20:55
    header: str = "{day}{title}截至{time}，各战队发展情况："
    congrats: tuple = ("恭喜{team}获得第一名🎉🎉🎉", "恭喜{team}获得第二名🎉🎉🎉")
    # {teams} = 「古南、东林」，末两名，去掉「战队」后缀再拼
    cheer: str = "{teams}战队加油✊✊✊！"
    footer: str = "请办理业务还未晒单的小组积极晒单！！！"
    # 一单都没有时不恭喜谁第一，换成这句
    empty: str = "各战队暂无晒单，请抓紧晒单！"


# ---------- 文字 ----------

def bulletin_text(teams: list[TeamStat], cfg: BulletinSettings,
                  camp_cfg: CampaignSettings, now: datetime | None = None) -> str:
    """前两名恭喜、末两名加油。全 0 时不硬排名次。"""
    now = now or datetime.now()
    ranked = [t for t in teams if t.rank]
    # 0 分的不算名次：五个 0 分按名册顺序排出 2~6 名，「恭喜 X 获得第二名」是笑话（实测踩过）
    scored = [t for t in ranked if t.progress > 0]
    zeros = [t for t in ranked if t.progress <= 0]
    lines = [cfg.header.format(day=f"{now.month}.{now.day}", title=camp_cfg.title,
                               time=now.strftime("%H:%M"))]
    if not scored:
        lines.append(cfg.empty)
    else:
        for tpl, t in zip(cfg.congrats, scored):
            lines.append(tpl.format(team=t.team))
        if zeros:
            # 有人还没开张就点名全部没开张的，不挑「末两名」—— 都是 0 没法分先后
            tail = zeros
        else:
            # 都有业绩才取末两名；战队少于 4 个时别把刚恭喜过的又拉出来加油
            tail = [t for t in ranked[len(cfg.congrats):]][-2:]
        if tail:
            names = "、".join(_stem(t.team) for t in tail)
            lines.append(cfg.cheer.format(teams=names))
    if cfg.footer:
        lines.append(cfg.footer)
    return "\n".join(lines)


def _stem(team: str) -> str:
    for suffix in ("战队", "支局", "战区"):
        if team.endswith(suffix):
            return team[: -len(suffix)]
    return team


# ---------- 图片 ----------

# 和 report.SUMMARY_COLUMNS 同序：(表头, 像素宽)
_IMG_COLUMNS = [
    ("战区", 110), ("小组", 72), ("成员", 200),
    ("新增宽带", 96), ("中高套", 84), ("长续约", 84), ("千兆/500M", 104),
    ("单号卡", 84), ("云盒/AI/智屏", 132), ("价值积分", 100),
    ("综合进度", 108), ("排名", 64),
]
_FONT_CANDIDATES = (
    # 项目自带的优先，放 assets/fonts/ 下即可
    str(ROOT / "assets" / "fonts" / "NotoSansCJK-Regular.ttc"),
    str(ROOT / "assets" / "fonts" / "msyh.ttc"),
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",
    "/System/Library/Fonts/PingFang.ttc",
)


def find_font() -> str | None:
    """找一个能画中文的字体文件。找不到返回 None（会画成方块）。"""
    env = os.getenv("BOT_FONT", "").strip()
    for p in ([env] if env else []) + list(_FONT_CANDIDATES):
        if p and Path(p).is_file():
            return p
    return None


def _font(size: int, bold: bool = False):
    from PIL import ImageFont
    path = find_font()
    if not path:
        return ImageFont.load_default()
    try:
        # msyh.ttc 里 index 1 是粗体；别的字体没有就退回常规
        if bold and path.lower().endswith("msyh.ttc"):
            return ImageFont.truetype(path, size, index=1)
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.truetype(path, size)


def render_png(groups: list[GroupStat], teams: list[TeamStat], targets: Targets,
               title: str) -> bytes:
    """
    把「晒单数据」画成一张 PNG。布局照 Excel 那张表：
    标题 / 表头 / 每战队一块（小组行 + 合计行），战区、进度、排名在块内居中。
    """
    from PIL import Image, ImageDraw

    pad = 24
    row_h, head_h, title_h, foot_h = 40, 50, 84, 44
    widths = [w for _, w in _IMG_COLUMNS]
    table_w = sum(widths)
    n_rows = len(groups) + len({g.team for g in groups})      # 小组行 + 每队一行合计
    W = table_w + pad * 2
    H = title_h + head_h + row_h * n_rows + foot_h + pad * 2

    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    f_title, f_head, f_cell, f_bold = _font(28, True), _font(20, True), _font(20), _font(20, True)
    line = (217, 221, 227)
    orange, orange_light = (238, 96, 44), (253, 233, 217)
    zebra = (248, 249, 251)
    gold = (255, 244, 204)

    xs = [pad]
    for w in widths:
        xs.append(xs[-1] + w)

    def cell(text, col, y, h, font=f_cell, fill=None, color=(33, 37, 41), align="center"):
        x0, x1 = xs[col], xs[col + 1]
        if fill:
            d.rectangle([x0, y, x1, y + h], fill=fill)
        d.rectangle([x0, y, x1, y + h], outline=line, width=1)
        text = "" if text is None else str(text)
        if not text:
            return
        bb = d.textbbox((0, 0), text, font=font)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        # 成员名太长就缩字号，别溢出到隔壁格
        if tw > x1 - x0 - 12:
            small = _font(16)
            bb = d.textbbox((0, 0), text, font=small)
            tw, th = bb[2] - bb[0], bb[3] - bb[1]
            font = small
        tx = x0 + 6 if align == "left" else x0 + (x1 - x0 - tw) / 2 - bb[0]
        ty = y + (h - th) / 2 - bb[1]
        d.text((tx, ty), text, font=font, fill=color)

    # 标题
    d.text((pad, pad + 8), title, font=f_title, fill=(33, 37, 41))
    y = pad + title_h

    # 表头
    for i, (name, _w) in enumerate(_IMG_COLUMNS):
        cell(name, i, y, head_h, font=f_head, fill=orange, color="white")
    y += head_h

    by_team = {t.team: t for t in teams}
    # 按战队切块
    blocks: list[tuple[str, list[GroupStat]]] = []
    for g in groups:
        if not blocks or blocks[-1][0] != g.team:
            blocks.append((g.team, []))
        blocks[-1][1].append(g)

    for team, rows in blocks:
        ts = by_team.get(team)
        block_h = row_h * (len(rows) + 1)
        y0 = y
        for k, g in enumerate(rows):
            fill = zebra if k % 2 else None
            vals = [None, g.label, g.members, g.broadband, g.high, g.renew, g.giga,
                    g.card, g.box, g.points, None, None]
            for i, v in enumerate(vals):
                if i in (0, 10, 11):
                    continue
                cell(v, i, y, row_h, fill=fill, align="left" if i == 2 else "center")
            y += row_h
        # 合计行
        tot = [None, "合计", None]
        tot += ([ts.broadband, ts.high, ts.renew, ts.giga, ts.card, ts.box, ts.points]
                if ts else [0] * 7)
        for i, v in enumerate(tot):
            if i in (0, 10, 11):
                continue          # 这三列是块内合并的，下面统一画
            cell(v, i, y, row_h, font=f_bold, fill=orange_light)
        y += row_h
        # 块内合并的三列：战区、综合进度、排名
        ranked = bool(ts and ts.rank)
        scored = ranked and ts.progress > 0
        fill = gold if (scored and ts.rank == 1) else None
        cell(team, 0, y0, block_h, font=f_bold, fill=fill)
        cell(f"{ts.progress:.2%}" if ranked else "", 10, y0, block_h, font=f_bold, fill=fill)
        # 0 分不排名次，画「-」
        cell(ts.rank if scored else ("-" if ranked else ""), 11, y0, block_h, font=f_bold, fill=fill)

    # 页脚：目标值，让看图的人知道进度是按什么算的
    foot = (f"目标：新增宽带 {targets.broadband}（{targets.w_broadband:.0%}）  "
            f"中高套 {targets.high}（{targets.w_high:.0%}）  "
            f"云盒/AI/智屏 {targets.box}（{targets.w_box:.0%}）  "
            f"价值积分 {targets.points}（{targets.w_points:.0%}）")
    d.text((pad, y + 12), foot, font=_font(17), fill=(108, 117, 125))

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


# ---------- 调度 ----------

@dataclass
class _State:
    fixed_sent: dict[str, str] = field(default_factory=dict)   # "11:50" -> 日期
    last_follow_at: float = 0.0
    last_command_at: float = 0.0


class Bulletin:
    """
    通报调度。由 bot.py 注入 pipeline，pipeline 在记单后调 on_order()。

    发送是重活（画图 + 写 Excel + 三次上传），一律丢进线程池，
    不在调用方线程里做。
    """

    def __init__(self, platform, store, campaign_getter, executor) -> None:
        self.platform = platform
        self.store = store
        self._campaign = campaign_getter
        self._pool = executor
        self._state_path = DATA / store.bulletin_settings().state_file
        self._pending: set[str] = set()      # 欠着补发的群；_load 会从盘上读回来
        self._pending_day = ""                # 欠账是哪天记的：隔天作废，别 00:05 补发一份空排名
        self._fixed_inflight: set[str] = set()   # 正在发的定点通报
        self._state = self._load()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---------- 生命周期 ----------

    def start(self) -> None:
        cfg = self.store.bulletin_settings()
        if not cfg.enabled:
            return
        if not self._target_chats():
            log.info("没配 %s_CHAT_IDS，定点通报不启用（群里喊「通报」仍然可用）",
                     self.platform.name.upper())
        if not find_font():
            log.warning("没找到中文字体，通报图片会是方块。Windows 装了微软雅黑即可，"
                        "Linux：apt install fonts-wqy-zenhei")
        self._thread = threading.Thread(target=self._loop, name="bulletin", daemon=True)
        self._thread.start()
        log.info("通报已启动：定点 %s，跟单最多每 %d 分钟一次",
                 "、".join(cfg.fixed_times), cfg.min_interval_minutes)

    def close(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(30):
            try:
                self._tick()
            except Exception:
                log.exception("通报定时检查出错，下轮继续")

    def _tick(self) -> None:
        cfg = self.store.bulletin_settings()
        if not cfg.enabled:
            return
        now = datetime.now()
        # 有活动文件就只在活动期间发；没有按 config 的星期（09-28）
        if activity.allows(now, cfg.days):
            self._check_fixed(cfg, now)
        self._check_pending(cfg)

    # ---------- 三种触发 ----------

    def _check_fixed(self, cfg: BulletinSettings, now: datetime) -> None:
        today = now.date().isoformat()
        ready = getattr(self.platform, "is_ready", None)
        if ready is not None and not ready():
            return              # 断线时不发、不记「已发」（09-24 断线那晚 21:00 通报就是这么丢的）
        for hhmm in cfg.fixed_times or ():
            if self._state.fixed_sent.get(hhmm) == today or hhmm in self._fixed_inflight:
                continue
            try:
                h, m = str(hhmm).split(":")
                due = datetime.combine(now.date(), dtime(int(h), int(m)))
            except Exception:
                log.warning("定点时刻格式不对：%r，应为 HH:MM", hhmm)
                continue
            # 到点后 10 分钟内都算数：进程刚好那一分钟没跑也能补上
            if not (due <= now <= due + timedelta(minutes=10)):
                continue
            chats = self._target_chats()
            if not chats:
                continue
            # 这份已经包含了之前欠着的单：跟单节流从现在重新计时，别几分钟内再来一份
            self._mark_sent()
            log.info("定点通报 %s", hhmm)
            self._fixed_inflight.add(hhmm)

            def done(fut, hhmm=hhmm):
                self._fixed_inflight.discard(hhmm)
                try:
                    ok = bool(fut.result().get("ok"))
                except Exception:
                    ok = False
                if ok:                      # 发成功才记「已发」，失败下一轮重试（到点 10 分钟内）
                    self._state.fixed_sent[hhmm] = today
                    self._save()
                else:
                    log.warning("定点通报 %s 没发出去，稍后重试", hhmm)
            try:
                self._pool.submit(self.publish, chats, f"定点{hhmm}").add_done_callback(done)
            except RuntimeError:
                self._fixed_inflight.discard(hhmm)

    def _mark_sent(self) -> None:
        """
        任何一份通报发出去（定点 / 命令 / 跟单）都算「刚通报过」：
        更新跟单的计时起点、清掉欠着的补发标记。
        以前定点和命令不碰这两样，实测 11:50 定点刚发、11:52 来一单又发一份，
        甚至同一个 tick 里定点和补发各提交一份一模一样的（审查踩出来的）。
        """
        with self._lock:
            self._state.last_follow_at = time.time()
            self._pending.clear()

    def _check_pending(self, cfg: BulletinSettings) -> None:
        """跟单通报欠着的，间隔一到就补发。"""
        with self._lock:
            if not self._pending:
                return
            today = datetime.now().date().isoformat()
            if self._pending_day and self._pending_day != today:
                # 23:40 欠着的单到 00:05 才够间隔：新一天排名是空的，补发没意义，
                # 那批单在下一次定点通报（按天排名已翻页）里也不会再出现，直接作废
                log.info("欠着的跟单通报是 %s 的，已跨天，不再补发", self._pending_day)
                self._pending = set()
                self._pending_day = ""
                self._save()
                return
            if time.time() - self._state.last_follow_at < cfg.min_interval_minutes * 60:
                return
            chats, self._pending = tuple(self._pending), set()
            self._state.last_follow_at = time.time()
        self._save()
        log.info("跟单通报（补发）-> %d 个群", len(chats))
        self._submit(chats, "跟单补发")

    def on_order(self, chat_id: str) -> None:
        """pipeline 记了一单新的就调这里。间隔够就发，不够就记着。"""
        cfg = self.store.bulletin_settings()
        if not cfg.enabled or not cfg.follow_orders or not chat_id:
            return
        with self._lock:
            if time.time() - self._state.last_follow_at < cfg.min_interval_minutes * 60:
                self._pending.add(chat_id)
                self._pending_day = datetime.now().date().isoformat()
                log.info("有新单，但距上次通报不足 %d 分钟，稍后补发", cfg.min_interval_minutes)
                self._save()      # 欠账也落盘，重启不丢
                return
            self._state.last_follow_at = time.time()
        self._save()
        self._submit((chat_id,), "跟单")

    def handle_command(self, msg) -> bool:
        """群里喊「通报」。命中返回 True，调用方就不用再往下走规则和模型了。"""
        cfg = self.store.bulletin_settings()
        if not cfg.enabled:
            return False
        text = (msg.text or "").strip()
        if text not in set(cfg.commands or ()):
            return False
        return self.request(msg.chat_id, f"命令「{text}」")

    def request(self, chat_id: str, reason: str) -> bool:
        """
        「立刻通报一次」的统一入口：群里喊「通报」和助手的「发通报」都走这里。
        三道门一起过：通报开关、60 秒冷却、重置跟单计时。返回 False = 没开通报。
        以前助手那条路绕过了全部三道，关了通报也照发、连喊不限次（审查踩出来的）。
        """
        cfg = self.store.bulletin_settings()
        if not cfg.enabled:
            return False
        text = reason
        with self._lock:
            if time.time() - self._state.last_command_at < cfg.command_cooldown:
                log.info("%s 触发太频繁（%d 秒内已发过），忽略", text, cfg.command_cooldown)
                return True
            self._state.last_command_at = time.time()
        self._mark_sent()
        self._save()
        log.info("%s，立即通报", text)
        self._submit((chat_id,), reason)
        return True

    # ---------- 生成 + 发送 ----------

    def _submit(self, chats: tuple[str, ...], reason: str) -> None:
        try:
            self._pool.submit(self.publish, chats, reason)
        except RuntimeError:
            log.debug("正在退出，放弃通报")

    def build(self, now: datetime | None = None) -> tuple[str, bytes, Path]:
        """生成 (文字, PNG 字节, Excel 路径)。也给离线测试用。"""
        now = now or datetime.now()
        cfg = self.store.bulletin_settings()
        camp_cfg = self.store.campaign_settings()
        camp: Campaign = self._campaign()
        camp.targets = camp_cfg.targets
        camp.refresh_roster()          # TeleAgent 可能刚改过名册
        # 排名按天、清单按周（campaign.rank_period / ledger_period），两个窗口分开取
        rank = camp_cfg.window("rank", now)
        ledger = camp_cfg.window("ledger", now)
        teams = camp.stats(*rank)
        groups = camp.group_stats(*rank)
        title = summary_title(camp_cfg.period_label("rank", now), camp_cfg.title, now)

        text = bulletin_text(teams, cfg, camp_cfg, now)
        png = render_png(groups, teams, camp.targets, title) if cfg.with_image else b""

        # Excel 每次都生成、落在 reports/（09-14 定的「Excel 不进群，只落 reports」）；
        # 发不发到群里由 with_excel 管
        xlsx = self.build_excel(now, stem=f"业绩通报_{now:%m%d_%H%M}")
        if png:
            xlsx.with_suffix(".png").write_bytes(png)
        return text, png, xlsx

    def build_excel(self, now: datetime | None = None, stem: str = "") -> Path:
        """
        晒单 Excel：「晒单数据」+「清单」+ 当天上午 / 下午 / 晚上三张「打卡」表。
        通报和「单聊要表格」用的是同一份；TeleAgent 的 export_report 生成的也是同样的结构。
        """
        now = now or datetime.now()
        cfg = self.store.bulletin_settings()
        camp_cfg = self.store.campaign_settings()
        camp: Campaign = self._campaign()
        camp.targets = camp_cfg.targets
        camp.refresh_roster()
        rank = camp_cfg.window("rank", now)
        ledger = camp_cfg.window("ledger", now)
        title = summary_title(camp_cfg.period_label("rank", now), camp_cfg.title, now)
        out_dir = DATA / cfg.out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        xlsx = out_dir / f"{stem or f'晒单表格_{now:%m%d_%H%M%S}'}.xlsx"
        ck = self.store.checkin_settings()
        sheets = day_sheets(ck, now.strftime("%Y-%m-%d"), camp_cfg.title) if ck.enabled else None
        return build_workbook(camp, xlsx, title, rank=rank, ledger=ledger,
                              checkin_sheets=sheets, checkin_columns=ck.columns)

    def publish(self, chats: tuple[str, ...], reason: str = "") -> dict:
        """
        生成并发送。返回 {"ok": [发成功的群], "failed": [失败的群], "error": 生成失败的原因}，
        调用方（MCP / 助手）按它报成功或失败 —— 以前一律报 ok，群里没收到也显示「已发」。
        """
        cfg = self.store.bulletin_settings()
        result = {"ok": [], "failed": [], "error": ""}
        try:
            text, png, xlsx = self.build()
        except Exception as exc:
            log.exception("生成通报失败（%s）", reason)
            result["error"] = f"生成通报失败：{exc}"
            return result
        for chat_id in chats:
            try:
                sent = []
                if cfg.with_text and hasattr(self.platform, "send_text"):
                    sent.append(self.platform.send_text(chat_id, text))
                if cfg.with_image and png and hasattr(self.platform, "send_image"):
                    sent.append(self.platform.send_image(chat_id, png, xlsx.with_suffix(".png").name))
                if cfg.with_excel and hasattr(self.platform, "send_file"):
                    sent.append(self.platform.send_file(chat_id, xlsx))
                if sent and all(sent):
                    log.info("通报已发（%s）-> %s", reason, chat_id)
                    result["ok"].append(chat_id)
                else:
                    log.error("通报发送失败（%s）-> %s：%s", reason, chat_id,
                              "平台没有发送方法" if not sent else "有一样没发出去")
                    result["failed"].append(chat_id)
            except Exception:
                log.exception("通报发送失败（%s）-> %s", reason, chat_id)
                result["failed"].append(chat_id)
        return result

    # ---------- 杂项 ----------

    def _target_chats(self) -> tuple[str, ...]:
        return tuple(getattr(getattr(self.platform, "cfg", None), "chat_ids", ()) or ())

    def _load(self) -> _State:
        if not self._state_path.exists():
            return _State()
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            # 欠着补发的群也读回来：以前只在内存里，重启就丢，那批单再也不通报
            self._pending = set(str(c) for c in (raw.get("pending") or []) if c)
            self._pending_day = str(raw.get("pending_day") or "")
            if self._pending:
                log.info("上次退出前还欠着 %d 个群的跟单通报，间隔一到就补", len(self._pending))
            return _State(fixed_sent=dict(raw.get("fixed_sent") or {}),
                          last_follow_at=float(raw.get("last_follow_at") or 0),
                          last_command_at=float(raw.get("last_command_at") or 0))
        except Exception as exc:
            log.warning("通报状态读不了，当作全新的：%s", exc)
            return _State()

    def _save(self) -> None:
        try:
            self._state_path.write_text(json.dumps({
                "fixed_sent": self._state.fixed_sent,
                "last_follow_at": self._state.last_follow_at,
                "last_command_at": self._state.last_command_at,
                "pending": sorted(self._pending),
                "pending_day": self._pending_day,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as exc:
            log.warning("通报状态写不进去：%s", exc)
