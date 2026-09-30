"""
打卡通报：定时往群里发「一段文字 + 一张打卡表图片」。

发送时刻（09-24 用户定）：上午 09:30 10:30 11:50 / 下午 15:00 16:00 17:00 18:00 / 晚上 19:30 20:50，
在 config.yaml 的 checkin.bulletin_times 改。时段结束那一刻的通报（11:50、18:00）统计刚结束的时段。
群里 @机器人 说「打卡通报」也能立刻发一份。

每次通报只看【上一次通报到这一次】之间的打卡（18:00 通报就是 17:00-18:00）；
时段里第一次通报标题从时段开始写（14:00-15:00），但把时段开始前、空档里拍的也算上（11:50-14:00 的进 15:00 那次）：

    13:00-15:30时段，各支局触点情况：
    通惠战队：8户（未打卡1组）
    …
    请各位支局长关注未打卡小组！！！

    · 户数 = 这个战队的打卡条数，无效打卡也算（和样表的「触点总量」一致）
    · 未打卡 = 这个时段一条打卡都没有的小组数
    · 时段开头是同一时段里上一次通报的时刻（第一次用 08:30 / 14:00 / 18:00），结尾是这次通报时刻
图片照样表画：战区 / 小组 / 成员 / 合计 / 第1户…；业务填绿、无效填红、没打卡的小组成员名填红。
"""

from __future__ import annotations

import io
import json
import logging
import threading
import time
from datetime import datetime, timedelta

from . import activity
from .bulletin import _font
from .checkin import GREEN, RED, checkin_title, load_records, select, slot_of, table_blocks, team_display
from .paths import DATA

log = logging.getLogger("botcore.checkin_bulletin")


def current_slot(hhmm: str, slots: list) -> dict:
    """
    这次通报统计哪个时段：开始 < 通报时刻 <= 结束 的那个时段。
    时段结束那一刻的通报统计刚结束的时段（11:50 -> 上午、18:00 -> 下午，不是晚上）；
    时段之间的空档（12:30 喊「打卡通报」）算下一个时段；21:00 以后给最后一个时段。
    """
    ordered = sorted(slots or (), key=lambda x: str(x.get("start", "")))
    for s in ordered:
        if hhmm <= str(s.get("end", "")):
            return s
    return ordered[-1] if ordered else {"name": "", "start": "00:00", "end": "23:59"}


def window_start(hhmm: str, slot: dict, times: list) -> str:
    """
    这次通报从哪儿算起（09-24 用户定：上一次通报到这一次，18:00 通报就是 17:00-18:00）。
    同一时段里上一个通报时刻；时段里的第一次通报从时段开始算（15:00 -> 14:00，不接上午的 11:50）。
    """
    start = str(slot.get("start", ""))
    earlier = [str(t) for t in times or () if start <= str(t) < hhmm]
    return max(earlier) if earlier else start


def build_report(records, roster, s, campaign_title: str, now: datetime) -> tuple[str, bytes, str]:
    """(文字, PNG, 标题)。records 是全部打卡记录，这里按今天 + 当前时段 + 截至现在筛。"""
    day = now.strftime("%Y-%m-%d")
    hhmm = now.strftime("%H:%M")
    slot = current_slot(hhmm, s.slots)
    if hhmm < str(slot.get("start", "")):
        # 时段之间的空档里临时喊「打卡通报」（12:30）：统计上一次通报到现在（11:50-12:30），不分时段
        start = max([str(t) for t in s.bulletin_times or () if str(t) < hhmm] or ["00:00"])
        recs = [r for r in select(records, day=day) if start < r.at[11:16] <= hhmm]
    else:
        start = window_start(hhmm, slot, s.bulletin_times)
        first = start == str(slot.get("start", ""))
        # 上一次通报到这一次：(上次, 这次]。
        # 时段里第一次通报把这个时段【之前】的也算上（09-24 用户：11:50-14:00 拍的算进 15:00 那次），
        # 标题照样写 14:00-15:00。空档里的打卡按 slot_of 本来就归下一个时段，这里不设下限即可
        recs = [r for r in select(records, day=day, slot=str(slot.get("name", "")), slots=s.slots)
                if (first or start < r.at[11:16]) and r.at[11:16] <= hhmm]
    title = checkin_title(s, day, campaign_title, start, hhmm)

    width, blocks = table_blocks(recs, roster, s.columns)
    lines = [f"{start}-{hhmm}时段，各支局触点情况："]
    for team, total, rows in blocks:
        if not team:
            continue
        idle = sum(1 for _label, members, rs in rows if members and not rs)
        lines.append(f"{team}：{total}户（未打卡{idle}组）")
    lines.append("请各位支局长关注未打卡小组！！！")
    return "\n".join(lines), render_png(title, width, blocks), title


def render_png(title: str, width: int, blocks) -> bytes:
    """照通报样表画打卡表。"""
    from PIL import Image, ImageDraw

    pad = 20
    cols = [("触点\n总量", 64), ("战区", 110), ("小组", 64), ("成员", 220), ("合计", 60)] + \
           [(f"第{i + 1}户", 168) for i in range(width)]
    row_h, head_h = 34, 34
    n_rows = sum(len(rows) for _t, _n, rows in blocks)
    W = sum(w for _n, w in cols) + pad * 2
    H = pad * 2 + head_h * 3 + row_h * n_rows
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    f_title, f_head, f_cell, f_small = _font(26, True), _font(18, True), _font(18), _font(15)
    line = (120, 120, 120)
    fills = {GREEN: (146, 208, 80), RED: (255, 0, 0)}
    xs = [pad]
    for _n, w in cols:
        xs.append(xs[-1] + w)

    def box(x0, y0, x1, y1, text="", font=f_cell, fill=None, color=(20, 20, 20)):
        if fill:
            d.rectangle([x0, y0, x1, y1], fill=fill)
        d.rectangle([x0, y0, x1, y1], outline=line, width=1)
        if not text:
            return
        # 放不下就逐级缩小字号，别溢出到隔壁格
        f = font
        bb = d.multiline_textbbox((0, 0), text, font=f, spacing=2)
        for size in (16, 14, 12, 11):
            if bb[2] - bb[0] <= x1 - x0 - 6:
                break
            f = _font(size)
            bb = d.multiline_textbbox((0, 0), text, font=f, spacing=2)
        tx = x0 + (x1 - x0 - (bb[2] - bb[0])) / 2 - bb[0]
        ty = y0 + (y1 - y0 - (bb[3] - bb[1])) / 2 - bb[1]
        d.multiline_text((tx, ty), text, font=f, fill=color, spacing=2, align="center")

    y = pad
    # 左上「触点总量」占三行；标题、上门时间横跨后面所有列
    box(xs[0], y, xs[1], y + head_h * 3, "触点\n总量", f_head)
    box(xs[1], y, xs[-1], y + head_h, title, f_title)
    box(xs[1], y + head_h, xs[-1], y + head_h * 2, "上门时间", f_head)
    for i, (name, _w) in enumerate(cols[1:], start=1):
        box(xs[i], y + head_h * 2, xs[i + 1], y + head_h * 3, name, f_head)
    y += head_h * 3

    for team, total, rows in blocks:
        top = y
        for label, members, recs in rows:
            box(xs[2], y, xs[3], y + row_h, label)
            box(xs[3], y, xs[4], y + row_h, members or "（水印成员认不出）",
                fill=fills[RED] if (members and not recs) else None)
            box(xs[4], y, xs[5], y + row_h, str(len(recs)))
            for i in range(width):
                rec = recs[i] if i < len(recs) else None
                box(xs[5 + i], y, xs[6 + i], y + row_h,
                    f"{rec.at[11:16]}（{rec.label}）" if rec else "",
                    fill=fills.get(rec.color) if rec else None)
            y += row_h
        box(xs[0], top, xs[1], y, str(total), f_head)
        box(xs[1], top, xs[2], y, team_display(team), f_head)

    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


class CheckinBulletin:
    def __init__(self, platform, store, campaign_getter, executor) -> None:
        self.platform = platform
        self.store = store
        self._campaign = campaign_getter
        self._pool = executor
        self._state_path = DATA / store.checkin_settings().bulletin_state_file
        self._sent: dict[str, str] = self._load()       # 时刻 -> 哪天发过
        self._lock = threading.Lock()
        self._last_command = 0.0
        self._inflight: set[str] = set()          # 正在发的定点，别在它发完之前又提交一次
        self.hq_command = None                    # 攻坚群喊「打卡通报」时调它（发支局长点检表），bot.py 装上
        self.recorder = None                      # CheckinRecorder，bot.py 装上：发通报前先补判
        self._stop = threading.Event()

    # ---------- 定时 ----------

    def start(self) -> None:
        s = self.store.checkin_settings()
        threading.Thread(target=self._loop, name="checkin-bulletin", daemon=True).start()
        log.info("打卡通报：%s，时刻 %s", "已开启" if (s.enabled and s.bulletin_enabled) else "未开启（只响应群里喊「打卡通报」）",
                 "、".join(s.bulletin_times))

    def close(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(30):
            try:
                self._tick(datetime.now())
            except Exception:
                log.exception("打卡通报定时检查出错，下轮继续")

    def _tick(self, now: datetime) -> None:
        s = self.store.checkin_settings()
        # 有活动文件就只在活动期间发；没有按 config 的 bulletin_days（09-28）
        if not (s.enabled and s.bulletin_enabled) or not activity.allows(now, s.bulletin_days):
            return
        today = now.strftime("%Y-%m-%d")
        ready = getattr(self.platform, "is_ready", None)
        if ready is not None and not ready():
            return              # 断线时不发、不记「已发」：连上后还在 10 分钟补发窗口里就补
        for hhmm in s.bulletin_times or ():
            if self._sent.get(hhmm) == today or hhmm in self._inflight:
                continue
            h, m = str(hhmm).split(":")
            due = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
            # 到点后 10 分钟内都算：进程刚好那一分钟没跑也能补上，但不会把上午的拖到下午发
            if not (due <= now <= due + timedelta(minutes=10)):
                continue
            targets = self._targets()
            if not targets:
                continue
            log.info("打卡通报定点 %s", hhmm)
            self._inflight.add(hhmm)

            def done(fut, hhmm=hhmm):
                self._inflight.discard(hhmm)
                try:
                    ok = bool(fut.result().get("ok"))
                except Exception:
                    ok = False
                if ok:                      # 发成功才记「已发」；失败下一轮（30 秒后）重试，到点 10 分钟内有效
                    with self._lock:
                        self._sent[hhmm] = today
                    self._save()
                else:
                    log.warning("打卡通报 %s 没发出去，稍后重试", hhmm)
            try:
                self._pool.submit(self.publish_targets, targets, f"定点{hhmm}", due).add_done_callback(done)
            except RuntimeError:
                self._inflight.discard(hhmm)

    # ---------- 群里喊 ----------

    def handle_command(self, msg) -> bool:
        s = self.store.checkin_settings()
        text = (msg.text or "").strip()
        if not s.enabled or text not in set(s.bulletin_commands or ()):
            return False
        with self._lock:
            if time.time() - self._last_command < 60:
                log.info("「%s」一分钟内喊过了，忽略", text)
                return True
            self._last_command = time.time()
        from . import regions
        region = None
        if regions.multi_enabled(self.store):
            role = regions.group_role(self.store, msg.chat_id)
            if role == "hq":
                # 攻坚群没有活动打卡通报：喊「打卡通报」发支局长打卡点检表（第 6 块装上）
                if self.hq_command is not None:
                    log.info("攻坚群喊「%s」，发支局长打卡点检表", text)
                    self.hq_command(msg.chat_id)
                return True
            region = regions.REGION_OF_ROLE.get(role)
        log.info("群里喊「%s」，立即打卡通报%s", text, f"（{region}）" if region else "")
        try:
            self._pool.submit(self.publish, (msg.chat_id,), f"命令「{text}」", datetime.now(), region)
        except RuntimeError:
            pass
        return True

    # ---------- 发送 ----------

    def _submit(self, chats, reason: str, now: datetime) -> None:
        try:
            self._pool.submit(self.publish, chats, reason, now)
        except RuntimeError:
            pass

    def _targets(self) -> list[tuple[tuple, str | None]]:
        """[(群, 区域)]：多群模式城区群发城区、农村群发农村（攻坚群没有打卡通报，它有支局长点检表）；单群照旧。"""
        from . import regions
        if regions.multi_enabled(self.store):
            out = []
            for role, region in regions.REGION_OF_ROLE.items():
                chats = regions.role_chats(self.store, role)
                if chats:
                    out.append((chats, region))
            return out
        chats = tuple(getattr(getattr(self.platform, "cfg", None), "chat_ids", ()) or ())
        return [(chats, None)] if chats else []

    def build(self, now: datetime, region: str | None = None) -> tuple[str, bytes, str]:
        from .checkin import of_region
        s = self.store.checkin_settings()
        camp = self._campaign()
        camp.refresh_roster()
        roster = (camp.rosters.on(now.strftime("%Y-%m-%d")).activity[region]      # 那天的活动名单（09-29）
                  if (region and getattr(camp, "rosters", None)) else camp.roster)
        records = of_region(load_records(DATA / s.store_file), region)
        return build_report(records, roster, s, self.store.campaign_settings().title, now)

    def publish_targets(self, targets, reason: str, now: datetime) -> dict:
        """定点：每个 (群, 区域) 发各自那份；有一个群发成功就算这个时刻发过（失败的记日志）。"""
        total = {"ok": [], "failed": []}
        for chats, region in targets:
            r = self.publish(chats, reason, now, region)
            total["ok"] += r.get("ok", [])
            total["failed"] += r.get("failed", [])
        return total

    def publish(self, chats, reason: str, now: datetime, region: str | None = None) -> dict:
        result = {"ok": [], "failed": []}
        if self.recorder is not None:
            try:                    # 发之前把「待判定」补判一遍，表里尽量不留待判定（09-28）
                self.recorder.retry_now(timeout=180)
            except Exception:
                log.exception("通报前补判出错（照常发）")
        try:
            text, png, title = self.build(now, region)
        except Exception:
            log.exception("生成打卡通报失败（%s）", reason)
            return result
        for chat in chats:
            try:
                ok = self.platform.send_text(chat, text)
                if png and hasattr(self.platform, "send_image"):
                    ok = self.platform.send_image(chat, png, f"打卡_{now:%m%d_%H%M}.png") and ok
                (result["ok"] if ok else result["failed"]).append(chat)
                log.info("打卡通报%s（%s）-> %s | %s", "已发" if ok else "发送失败", reason, chat, title)
            except Exception:
                log.exception("打卡通报发送失败（%s）-> %s", reason, chat)
                result["failed"].append(chat)
        return result

    # ---------- 状态 ----------

    def _load(self) -> dict:
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            return {str(k): str(v) for k, v in (raw.get("sent") or {}).items()} if isinstance(raw, dict) else {}
        except Exception:
            return {}

    def _save(self) -> None:
        try:
            with self._lock:
                payload = {"sent": dict(self._sent)}
            self._state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as exc:
            log.warning("打卡通报状态写不进去：%s", exc)
