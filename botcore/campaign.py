"""
营销活动统计：把群里的晒单变成「清单」的一行，再汇总出战队排名。

数据流（照搬那张手工 Excel 的结构，机器人只是替掉人工录入那一步）：

    群消息 —— 只认固定格式的文字晒单（「业务晒单时刻」模板）；
              照片只点赞不记单（字段拿不全，记进去也是整行待补）
        ↓  解析成一条 Order
    清单（每单一行）
        ↓  揽收人 → 名册 → 小组 / 战区
    按小组求和 → 按战队求和
        ↓
    综合进度 = 新增宽带/目标 × 25%
             + 中高套/目标   × 30%
             + 云盒AI智屏/目标 × 15%
             + 价值积分/目标  × 20%
    排名 = 综合进度降序

公式和权重来自原表 `晒单数据!W2`：
    =(D8/P2)*0.25+(E8/Q2)*0.3+(I8/U2)*0.15+(J8/V2)*0.2
注意四项权重只加到 90%：长续约 / 千兆500兆 / 单号卡 有统计但没有目标值，
不参与打分。这是原表就这样，不是这里算漏了。

⚠️ 哪些字段【推不出来】：
    千兆500兆、云盒/AI/智屏、FTTR 取决于现场实际装了什么设备，
    原表里是每单人工填的。套餐资费文本推不出来 —— 实测「38+10」的 +10
    不是云盒，「129改139」也没有千兆。所以：
      · 文字晒单有「是否加载云盒/AI/智屏/FTTR」字段，直接采信
      · 图片晒单拿不到，留空并标记 needs_review，等人工补
    宁可留空也不猜：排名要发到群里，填错了没法解释。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from dataclasses import dataclass, field, asdict, replace
from datetime import datetime, timedelta
from pathlib import Path

from .backup import keep_version
from .distill import _contact_key, _members_of
from .paths import DATA

log = logging.getLogger("botcore.campaign")

# 中高套门槛。回测 31 单 100% 吻合：139 及以上算中高套，同时也算千兆。
# 培训课件印证：139 起就是 500M，169 有条件升 1000M，199+ 是 1000M/2000M，
# 所以「千兆/500兆」= 139 及以上；79（200M）、99（300M）不算。
# 但千兆只对【新增】成立，存量升档到 139 未必有千兆（原表里 129改139 是 0）。
# 完整资费口径见 docs/套餐资费.md。
HIGH_TIER_MIN = 139
# 单宽 / 包年单宽里带宽 ≥ 500M 的档位（课件第 3 页）：138 单宽 500M、198 单宽 1000M、
# 1390 包年 500M、1990 包年 1000M。其余（68/98 单宽、390/690/990 包年）都在 300M 以下，
# 390 包年只有 50M —— 不能按「数字 ≥ 139」当千兆
SOLO_GIGA_TIERS = {138, 198, 1390, 1990}

_NUM = re.compile(r"(\d+)")
_UPGRADE = re.compile(r"(\d+)\s*(?:改|升档|升|提值|→|->)\s*(\d+)")
# 「169提值60」「提值60」：直接写提值积分的
_LIFT = re.compile(r"提值\s*(\d+)")
# 存量单看【总和】：前后各自「档位 + 附加 − 返费」
_PART_NUM = re.compile(r"(?<![返])(\d+)")
# 附加值：「+10」「＋10」「加10」「加装10」都算（09-29：只认字，员工不一定打 +）
# 线上真实晒单还有用汉字「十」、表情「➕」当加号的（「139十10」「79➕10云盒」），「十」后面跟数字才算
# 纯加装：开头就是「10元云盒」「云盒10」「云盒」这种，没有宽带档位
_PURE_ADDON = re.compile(r"^\D{0,3}?(?:\d{1,2}(?:\.\d+)?\s*元?\s*(?:的\s*)?)?(?:云盒|智屏|AI|FTTR|光纤到房间|视联网|看家|守护|摄像头)"
                         r"(?![^\d]*(?:\d{3}|融合|单宽|包年))", re.I)
_ADDON = re.compile(r"(?:[+＋➕]|加装|加|十(?=\s*\d))\s*(\d+)")
# 橙分期：「59返30」「99橙分期返55」—— 价值积分 = 月费 − 月返
# （培训课件的橙分期表就是这么算的：59返30 → 29 分，399 → 199 分）
_REBATE = re.compile(r"返\s*(\d+)")
# 没写「返多少」时按课件里的标准月返补：档位 -> 月翼支付金
_ORANGE_REBATE = {59: 30, 99: 55, 159: 100, 199: 90, 299: 145, 399: 200}
# 「5G」「5G-A」「4G」里的数字不是档位，抽数字前先剥掉
_TECH_TAG = re.compile(r"[45]G(?:-?A)?", re.I)
# 每个「副宽」都是独立的一条宽带：「199+副宽」= 2 条
_SUB_BB = re.compile(r"副宽")
# 单宽 / 副宽 / 包年：算新增宽带，但没有融合档位，不算中高套。
# 资费见 docs/套餐资费.md 第 3 节
_IS_SOLO = re.compile(r"单宽|副宽|包年|包月")
# 权益金币数，不是档位也不是积分（699 / 1200 / 1298 …）。见到就不当数字用
_IS_EQUITY = re.compile(r"权益|金币")
# 号卡类：山城卡、号卡、单C 卡等。命中就不算宽带。
# 号卡类：山城卡、号卡、携转异网卡、副卡、单C 等。命中就不算宽带。
# 「携转」= 携号转网，把别家的号转过来，属于新增卡（实测漏判过）。
# 「橙分期」是号卡的分期产品（培训课件「橙分期-手机需求」表，产品小类全是单C），
# 「橙分期399」是 399 档的号卡，不是 399 宽带
_IS_CARD = re.compile(r"山城卡|号卡|携转|异网卡|单\s*[CcＣ]|橙分期")
# 「副卡」两可：「新增副卡」是单办一张副卡（号卡单）；「199+副卡」是融合套餐带的副卡
# （附加项，主体还是 199 宽带）。看有没有主套餐档位来分
_ONLY_SUBCARD = re.compile(r"^[^\d]*副卡[^\d]*$")


@dataclass
class Targets:
    """各项目标值和权重。原表里六个战队目标相同，但留成可配。"""
    broadband: int = 8
    high: int = 4
    box: int = 5
    points: int = 1000
    w_broadband: float = 0.25
    w_high: float = 0.30
    w_box: float = 0.15
    w_points: float = 0.20


_PERIODS = ("day", "week", "fixed")


def _norm_day(s: str) -> str:
    """2026-09-11 / 2026/9/11 / 2026.9.11 / 2026年9月11日 / 9.11 -> "2026-09-11"，认不出返回空串。"""
    s = str(s).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y年%m月%d日"):
        try:
            return datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2})[./\-月](\d{1,2})日?", s)
    if m:
        try:
            return datetime(datetime.now().year, int(m.group(1)), int(m.group(2))).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


@dataclass
class CampaignSettings:
    """
    config.yaml 里的 campaign 段：活动叫什么、按什么周期统计、目标值多少。

    两个周期分开配，因为用途不同：
        rank_period    通报里的排名、晒单数据表、表格图 —— 默认按【天】，
                       每天都从零开始比，昨天冲得猛今天也不能躺
        ledger_period  Excel 里的「清单」记录 —— 默认按【周】，
                       一周的单都在一张表里，周一自动翻页
    取值都是：day（当天）/ week（本周一到周日）/ fixed（start~end，临时活动用）。
    """
    title: str = "城区营销活动"
    rank_period: str = "day"
    ledger_period: str = "week"
    start: str = ""            # YYYY-MM-DD，仅 fixed 时用
    end: str = ""              # YYYY-MM-DD，仅 fixed 时用
    targets: Targets = field(default_factory=Targets)
    # 农村活动群的目标（09-29 用户：城区农村目标不一样）；没配就用 targets
    targets_rural: "Targets | None" = None

    def targets_for(self, region: str = "") -> Targets:
        return self.targets_rural if (region == "农村" and self.targets_rural is not None) else self.targets

    @classmethod
    def from_dict(cls, raw: dict | None) -> "CampaignSettings":
        raw = dict(raw or {})
        t = raw.pop("targets", None) or {}
        targets = Targets(**{k: v for k, v in t.items() if k in Targets.__dataclass_fields__})
        tr = raw.pop("targets_rural", None)
        targets_rural = (Targets(**{k: v for k, v in tr.items() if k in Targets.__dataclass_fields__})
                         if isinstance(tr, dict) else None)
        known = {k: str(v).strip() if v is not None else "" for k, v in raw.items()
                 if k in ("title", "rank_period", "ledger_period", "start", "end")}
        cfg = cls(targets=targets, targets_rural=targets_rural, **known)
        # start / end 写成 2026/09/11、2026.9.11、9.11 都认；认不出就当没填
        for name in ("start", "end"):
            raw_v = getattr(cfg, name)
            if raw_v:
                norm = _norm_day(raw_v)
                if not norm:
                    log.warning("campaign.%s=%r 不是日期，忽略（要 2026-09-11 这种）", name, raw_v)
                setattr(cfg, name, norm)
        for name in ("rank_period", "ledger_period"):
            val = getattr(cfg, name)
            if val not in _PERIODS:
                log.warning("campaign.%s=%r 不认识，可选 %s，按 day 处理",
                            name, val, "/".join(_PERIODS))
                setattr(cfg, name, "day")
            elif val == "fixed" and not (cfg.start and cfg.end):
                log.warning("campaign.%s=fixed 但没填 start/end，按 day 处理", name)
                setattr(cfg, name, "day")
        return cfg

    def days(self, kind: str = "rank", now: datetime | None = None) -> tuple[str, str]:
        """
        某个用途的统计范围首尾两天（YYYY-MM-DD，含首尾）。
        kind = "rank"（排名 / 晒单数据）或 "ledger"（清单记录）。
        """
        period = self.ledger_period if kind == "ledger" else self.rank_period
        now = now or datetime.now()
        today = now.date()
        if period == "week":
            monday = today - timedelta(days=today.weekday())
            return monday.isoformat(), (monday + timedelta(days=6)).isoformat()
        if period == "fixed":
            start, end = self.start, self.end
            # start > end 是配错了，退回单日，别静默统计成空
            return (end, end) if start > end else (start, end)
        return today.isoformat(), today.isoformat()

    def window(self, kind: str = "rank", now: datetime | None = None) -> tuple[str, str]:
        """返回 (since, until)，给 Campaign.stats / build_workbook 用的完整时刻。"""
        start, end = self.days(kind, now)
        return f"{start} 00:00:00", f"{end} 23:59:59"

    def period_label(self, kind: str = "rank", now: datetime | None = None) -> str:
        """「8.27-8.30」这种标题用的日期段。单日就只有一个。"""
        def md(day: str) -> str:
            try:
                d = datetime.strptime(day, "%Y-%m-%d")
                return f"{d.month}.{d.day}"
            except ValueError:
                return day
        start, end = self.days(kind, now)
        return md(start) if start == end else f"{md(start)}-{md(end)}"


@dataclass
class Order:
    """清单里的一行。字段名对齐原表的列。"""
    at: str = ""                 # 记录时间 YYYY-MM-DD HH:MM:SS
    kind: str = ""               # 新增 / 存量 / 单C
    fee: str = ""                # 套餐资费，如 199+10、129改139
    collector: str = ""          # 揽收人
    team: str = ""               # 战区（由揽收人查名册得到）
    group: str = ""              # 小组（同上）

    broadband: int = 0           # 新增宽带
    high: int = 0                # 新增中高套
    renew: int = 0               # 长续约
    giga: int = 0                # 千兆/500兆
    card: int = 0                # 单号卡
    fttr: int = 0                # FTTR
    box: int = 0                 # 云盒/AI视联网/智屏
    loaded: str = ""             # 晒单「是否加载」里勾了哪几样：云盒,AI,智屏,FTTR（国庆版套包分要分清智屏）
    up_giga: str = ""            # 模板「是否升级千兆/500M」：是 / 否；旧模板没这栏为空（国庆版千五看它）
    up_2000: str = ""            # 模板「是否升级2000M」：是 / 否；旧模板没这栏为空
    # ---- 多群（09-29 国庆版）：按人分类，一单只记一次，各张表按这几个字段取 ----
    region: str = ""             # 城区 / 农村 / ""（未识别）
    role: str = ""               # 活动 / 非活动 / 未识别（单群模式为空）
    branch: str = ""             # 总表归哪个支局（简称）：揽收人的支局（09-29 用户定），查不到按晒单写的支局，再按晒单人
    written_branch: str = ""     # 晒单里写的「支局」（简称），原样留着
    community: bool = False      # 发单人是小区 / 场镇营销人员：总表里新增宽带、中高套 ×1.1
    points: int = 0              # 价值积分

    source: str = ""             # 文字晒单 / 图片晒单
    needs_review: bool = False   # 有字段没填满，要人工核对
    note: str = ""               # 备注，说明哪些字段缺
    msg_id: str = ""             # 来源消息，用于判重
    sender_id: str = ""          # 发消息的人（企微 userid）
    poster: str = ""             # 发消息的人在名册里的姓名（按账号拼音 / userid_map 对出来，对不上为空）
    contact: str = ""            # 客户号码的哈希。同一单重发时号码一定相同 —— 覆盖依据之一
    members: str = ""            # 水印里的「小组成员」（照片时代留下的，文字晒单一般为空）
    serial: str = ""             # 晒单头上的单号「9月21日第3单」。同人同天同单号 = 同一单，
                                 # 重发就是更正，后发的覆盖先发的


class NotRosterSheet(ValueError):
    """不是名册表（没有三列表头）。群里发别的 Excel 很正常，调用方静默跳过。"""


class RosterImportError(ValueError):
    """是名册表，但填错了或人数异常。调用方要回到群里说清楚。"""


_NAME_HEADERS = ("姓名",)
_GROUP_HEADERS = ("分组", "小组", "组别")
_TEAM_HEADERS = ("战区", "战队", "支局")


def _header_triples(cells: list[str]) -> list[tuple[int, int, int]]:
    """
    从表头行里找出每一组「姓名 / 分组 / 战区」的列号。
    一张表上左右并排两组（姓名|分组|战区|姓名|分组|战区）就返回两组。
    """
    triples = []
    for i, c in enumerate(cells):
        if c not in _NAME_HEADERS:
            continue
        g = next((j for j in range(i + 1, min(i + 6, len(cells))) if cells[j] in _GROUP_HEADERS), None)
        t = next((j for j in range(i + 1, min(i + 6, len(cells))) if cells[j] in _TEAM_HEADERS), None)
        if g is not None and t is not None:
            triples.append((i, g, t))
    return triples


@dataclass
class GroupInfo:
    team: str
    label: str          # 1组 / 2组 …
    members: str        # 康思宇、王国强


# 晒单头上的单号：「🔥 9月21日第3单🔥」。每个人自己数的，同人同天同号 = 同一单
_SERIAL = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*第\s*(\d+)\s*单")


_GROUP_NO = re.compile(r"^\s*第?\s*\d{1,2}\s*(?:小?组)?\s*$")


def _names_as_groups(people: dict) -> dict:
    """
    名单的「分组」一栏有两种填法：组员姓名（「康思宇、王国强」）或组号（「1组」）。
    机器人拿这一栏当小组的身份和表里的「成员」列，所以组号要换成同队同号的人名拼起来，
    否则晒单数据图、打卡表的成员列都显示成「1组」（09-24 新名单就是填的组号）。
    """
    members: dict[tuple[str, str], list[str]] = {}
    for name, info in people.items():
        group = str(info.get("group") or "")
        if _GROUP_NO.match(group):
            members.setdefault((info.get("team", ""), group), []).append(name)
    if not members:
        return people
    out = {}
    for name, info in people.items():
        key = (info.get("team", ""), str(info.get("group") or ""))
        out[name] = {**info, "group": "、".join(members[key])} if key in members else dict(info)
    return out


def _serial_of(text: str) -> str:
    m = _SERIAL.search(text or "")
    return f"{int(m.group(1))}月{int(m.group(2))}日第{int(m.group(3))}单" if m else ""


def _canon_team(name: str) -> str:
    """「新盛支局」「新盛」「新盛战区」-> 「新盛战队」。已经是战队的原样返回。"""
    name = (name or "").strip()
    for suffix in ("战队", "支局", "战区"):
        if name.endswith(suffix):
            return name[: -len(suffix)] + "战队"
    return name + "战队" if name else ""


class Roster:
    """姓名 → 小组 / 战区。对应原表的「分组」sheet 和那些 VLOOKUP。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else DATA / "roster.json"
        self.teams: list[str] = []
        self.people: dict[str, dict] = {}
        self._aliases: dict[str, str] = {}
        self.reload()

    def reload(self) -> None:
        if not self.path.exists():
            log.warning("名册 %s 不存在，揽收人无法归队", self.path)
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._mtime = self.path.stat().st_mtime
        except Exception as exc:
            log.error("名册读不了：%s", exc)
            return
        self._load(raw)

    @classmethod
    def from_dict(cls, raw: dict, label: str = "") -> "Roster":
        """从字典建名册（国庆每天一份的名单，见 regions.DailyRosters）。不对应文件，不热加载。"""
        r = cls.__new__(cls)
        r.path = Path(label or "roster_days")
        r.teams, r.people, r._aliases = [], {}, {}
        r._mtime = None
        r._load(raw, quiet=True)
        return r

    def _load(self, raw: dict, quiet: bool = False) -> None:
        self.teams = list(raw.get("teams") or [])
        self.people = _names_as_groups(dict(raw.get("people") or {}))

        # 战队别名：原表里同一个战队有「新盛支局」「新盛战队」「新盛」三种写法，
        # 晒单文字里写的又是「文龙支局」。统一归一化，否则会分裂成多个战队。
        self._aliases = {}
        for t in self.teams:
            stem = t.replace("战队", "").replace("支局", "").strip()
            for form in (t, stem, stem + "战队", stem + "支局", stem + "战区"):
                self._aliases[form] = t
        if not quiet:
            log.info("名册：%d 人 / %d 个战队", len(self.people), len(self.teams))

    def maybe_reload(self) -> bool:
        """
        roster.json 被别的进程改了（TeleAgent 通过 MCP 更新名册）就重新加载。
        和 config.yaml 的热加载一个套路：看文件 mtime。返回 True 表示换了新名册。
        """
        try:
            mtime = self.path.stat().st_mtime
        except OSError:
            return False
        if mtime == getattr(self, "_mtime", None):
            return False
        log.info("名册文件变了（TeleAgent 或手工改的），重新加载")
        self.reload()
        return True

    def normalize_team(self, name: str) -> str:
        """把「文龙支局」「文龙」「文龙战区」都归到「文龙战队」。"""
        return self._aliases.get((name or "").strip(), (name or "").strip())

    def lookup(self, person: str) -> tuple[str, str]:
        """返回 (小组, 战区)。查不到返回空串。"""
        info = self.people.get((person or "").strip())
        if not info:
            return "", ""
        return info.get("group", ""), self.normalize_team(info.get("team", ""))

    def groups(self) -> list["GroupInfo"]:
        """
        全部小组，按「战队顺序 × 出现顺序」排。

        小组没有单独的编号字段，编号就是它在本战队里的出现次序（1组、2组…），
        和原表「晒单数据」的排法一致。名册从 Excel 导入时保留了行顺序，
        所以这里的顺序就是表里的顺序。
        """
        by_team: dict[str, list[GroupInfo]] = {t: [] for t in self.teams}
        seen: set[tuple[str, str]] = set()
        for name, info in self.people.items():
            team = self.normalize_team(info.get("team", ""))
            group = (info.get("group") or "").strip()
            if not team or not group or (team, group) in seen:
                continue
            seen.add((team, group))
            bucket = by_team.setdefault(team, [])
            bucket.append(GroupInfo(team=team, label=f"{len(bucket) + 1}组", members=group))
        out: list[GroupInfo] = []
        for t in self.teams:
            out.extend(by_team.get(t, []))
        for t, items in by_team.items():
            if t not in self.teams:
                out.extend(items)
        return out

    def import_workbook(self, data: bytes, dry_run: bool = False) -> str:
        """
        从群里上传的 Excel 更新名册，返回一句给群里看的结果。

        认「姓名 | 分组 | 战区」三列的表（优先叫「分组」的 sheet）。能应付的排版：
          · 战区 / 分组列用合并单元格（竖着一格占几行）—— 合并格只有左上角有值，
            其余行读出来是空，按「同上」向上继承。以前这种表会丢一半人
          · 左右两组并排（姓名|分组|战区|姓名|分组|战区）—— 两组各读一遍
        Sheet1 / Sheet2 那种成对姓名、没表头的排版不解析。

        错误分两类：NotRosterSheet（没表头，不是名册表，调用方静默）；
        RosterImportError（是名册表但填错了 / 人数异常，调用方要回群里说）。

        战区名统一成「xx战队」：原表里「新盛支局」「新盛战队」混着写，
        不统一的话同一个战队会分成两个桶。
        """
        import io as _io
        from openpyxl import load_workbook

        try:
            wb = load_workbook(_io.BytesIO(data), read_only=True, data_only=True)
        except Exception as exc:
            raise RosterImportError(f"文件打不开（要 .xlsx，旧的 .xls 请另存为 xlsx）：{exc}")

        sheet, header_at, triples = None, -1, []
        order = sorted(wb.sheetnames, key=lambda n: 0 if n.strip() == "分组" else 1)
        for sname in order:
            ws = wb[sname]
            for r, row in enumerate(ws.iter_rows(min_row=1, max_row=10, values_only=True)):
                cells = [str(c).strip() if c is not None else "" for c in row]
                found = _header_triples(cells)
                if found:
                    sheet, header_at, triples = ws, r, found
                    break
            if sheet is not None:
                break
        if sheet is None:
            raise NotRosterSheet("表里没有「姓名 / 分组 / 战区」三列的表头")

        people: dict[str, dict] = {}
        teams: list[str] = []
        renamed: set[str] = set()
        last = [{"group": "", "team": ""} for _ in triples]     # 每组各自的「同上」
        for row in sheet.iter_rows(min_row=header_at + 2, values_only=True):
            cells = [str(c).strip() if c is not None else "" for c in row]

            def at(i: int) -> str:
                return cells[i] if i < len(cells) else ""

            for k, (ci_name, ci_group, ci_team) in enumerate(triples):
                name, group, team = at(ci_name), at(ci_group), at(ci_team)
                # 合并单元格：空的就是「同上」。但姓名空的行不算人
                group = group or last[k]["group"]
                team = team or last[k]["team"]
                if group:
                    last[k]["group"] = group
                if team:
                    last[k]["team"] = team
                if not name:
                    continue
                if name in ("姓名",):
                    continue
                if not group or not team:
                    raise RosterImportError(f"「{name}」这行缺分组或战区")
                canon = _canon_team(team)
                if canon != team:
                    renamed.add(f"{team}→{canon}")
                if canon not in teams:
                    teams.append(canon)
                if name in people:
                    raise RosterImportError(f"「{name}」出现了两次，一个人只能在一个小组")
                people[name] = {"group": group, "team": canon}
        if len(people) < 2:
            raise RosterImportError("表头下面没读到人")

        # 保险丝：比现在的名册少太多，多半是排版没读全，别把好名册覆盖成残缺的
        old_n = len(self.people)
        if old_n >= 10 and len(people) < old_n * 0.7:
            raise RosterImportError(
                f"只读到 {len(people)} 人，比现在名册的 {old_n} 人少太多，没有更新。"
                f"检查表格排版；确实是缩编的话先删掉 roster.json 再发")

        people = _names_as_groups(people)
        if dry_run:                          # 只看是不是名册表、能不能读（非管理员发的名册用）
            return f"{len(people)} 人"
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"teams": teams, "people": people},
                                  ensure_ascii=False, indent=2), encoding="utf-8")
        keep_version(self.path)              # 写之前留底
        tmp.replace(self.path)
        self.reload()
        n_groups = len({(v["team"], v["group"]) for v in people.values()})
        note = f"（战区名已统一：{'、'.join(sorted(renamed))}）" if renamed else ""
        return f"{len(people)} 人 / {len(teams)} 个战队 / {n_groups} 个小组{note}"


# 「业务类型」的各种写法 -> 清单里的三个口径。按顺序匹配，先命中先算。
# 号卡类放最前：「新增号卡」是号卡不是宽带。
# 「策反」（把异网用户策反过来）算新增，放最前 —— 「异网策反」不能因为「异网」被当成号卡（09-29 用户定）；
# 「办理」也算新增，但放最后 —— 「续约办理」还是存量。
_KIND_ALIASES = (
    ("新增", ("策反",)),
    ("单C", ("单C", "单c", "单Ｃ", "号卡", "山城卡", "副卡", "携转", "异网")),
    ("新增", ("新增", "新装", "新办", "新开", "新入网")),
    ("存量", ("存量", "续约", "续签", "续费", "改", "升档", "升级", "加装", "融合", "老用户",
              "平迁", "高迁", "负迁", "迁转", "提值", "拆机维系", "维系")),
    ("新增", ("办理",)),
)


def normalize_kind(kind: str) -> str:
    """
    「业务类型」写成什么都归到 新增 / 存量 / 单C。

    晒单模板里这格是手填的，见过「续约」「升档」「新装」这些写法。
    认不出来就原样返回，调用方标待核对，别硬猜。
    """
    kind = (kind or "").strip()
    for canon, words in _KIND_ALIASES:
        if any(w in kind for w in words):
            return canon
    return kind


def _part_total(part: str) -> int:
    """「139+10」→ 149；「169返60」→ 109。存量单前后各算一次总和再比。"""
    rebate = sum(int(x) for x in _REBATE.findall(part))
    total = sum(int(x) for x in _NUM.findall(_REBATE.sub("", part)))
    return total - rebate


def derive(kind: str, fee: str) -> dict:
    """
    从「业务类型 + 套餐资费」推出能推的那几项。

    回测 31 单的结果：
        新增宽带 100%   中高套 100%   单号卡 100%   积分 97%
        千兆 90%        云盒 84%      <- 这两项【不要用推的】
    所以这里【只返回能推准的】，千兆/云盒/FTTR 一律留给调用方从
    晒单文字的显式字段里取。
    """
    fee = _TECH_TAG.sub("", (fee or "").strip())
    # 9.9（AI 守护 / 看家）按 10 分算（09-29 用户定）；不换的话整数口径会拆成 9 和 9
    fee = re.sub(r"(?<![\d.])9\.9(?![\d.])", "10", fee)
    raw_kind = (kind or "").strip()
    kind = normalize_kind(raw_kind)
    nums = [int(x) for x in _NUM.findall(fee)]
    base = nums[0] if nums else 0

    up = _UPGRADE.search(fee)
    final = int(up.group(2)) if up else base
    # 附加项全算：「139+10+25」= 10 + 25。以前只取第一个「+」
    addon = sum(int(x) for x in _ADDON.findall(fee))
    rb = _REBATE.search(fee)
    rebate = int(rb.group(1)) if rb else 0

    out = dict(broadband=0, high=0, renew=0, card=0, points=0,
               tier=final, kind=kind)

    # 权益金币（699 兑换 FTTR 之类）：数字不是档位。留给人看，别算进去
    if _IS_EQUITY.search(fee) and not up:
        out["tier"] = 0
        out["points_unknown"] = True          # 让 build_order 标黄：是否计业绩要人定
        out["equity"] = True
        return out

    # 号卡单不是宽带单。晒单模板里这类会写成「业务类型:新增 / 套餐:19山城卡」，
    # 光看业务类型会误判成新增宽带，战报里的宽带数就虚高了。
    # 原表「清单」里这类是记成【单C】的，这里对齐那个口径。
    if _IS_CARD.search(fee) or _ONLY_SUBCARD.search(fee):
        # 【新增/存量只针对宽带】。号卡类一律是「单C」，
        # 哪怕晒单里业务类型填的是「新增」也要纠正过来 ——
        # 否则清单里会出现「新增 / 19山城卡」这种自相矛盾的行。
        out["card"] = 1
        out["kind"] = "单C"
        # 橙分期按「月费 − 月返」算分（59返30 → 29）；没写返多少就查课件标准
        if "橙分期" in fee and not rebate:
            rebate = _ORANGE_REBATE.get(final, 0)
        out["points"] = max(final + addon - rebate, 0)
        return out

    # 单宽 / 副宽 / 包年（套餐本身就是单宽，如「38副宽」「1390包年」）：
    # 新增一条宽带，没有融合档位，不算中高套。
    # 「199+副宽」「99+副宽」是融合套餐带副宽 —— 带「+」的一律不走这里，
    # 由下面的新增分支按主宽 + 副宽数（以前 99/79+副宽 被当成单宽只记 1 条）
    # 新增纯加装（「10元云盒」「云盒」，没有宽带档位）：不是新开宽带，只记积分（09-29 用户：有这种说法）
    if kind == "新增" and _PURE_ADDON.search(fee) and not _SUB_BB.search(fee):
        out["points"] = _part_total(fee)
        out["pure_addon"] = True
        return out

    if _IS_SOLO.search(fee) and not up and not re.search(r"[+＋➕]|加|十(?=\s*\d)", fee):
        if kind != "存量":
            out["kind"] = "新增"
            out["broadband"] = 1
        elif _SUB_BB.search(fee):
            # 存量单办副宽：副宽是新开的一条宽带，算新增宽带（09-29 用户定）
            out["broadband"] = len(_SUB_BB.findall(fee))
        out["points"] = final + addon
        out["solo"] = True
        out["solo_giga"] = final in SOLO_GIGA_TIERS
        return out

    if kind == "新增":
        # 主宽 1 条 + 每个副宽再各 1 条；副宽没有自己的档位，不计中高套
        out["broadband"] = 1 + len(_SUB_BB.findall(fee))
        if final >= HIGH_TIER_MIN:
            out["high"] = 1
        # 积分 = 写出来的钱都加上（档位 + 加装 + 副宽，09-29 用户定：看价值总和）；写了「改」的只算改成那段
        out["points"] = max(_part_total(fee[up.start(2):] if up else fee), 0)
    elif kind == "单C":
        out["card"] = 1
        out["points"] = final
    elif kind == "存量":
        # 【存量就是续约】，看【总和】（2026-09-22 定的口径，原表 9 条存量单全部印证）：
        #   前 = 原档位（返费要减掉：169返60 实缴 109），后 = 新档位 + 附加
        #   课件「存量权益政策」按【提值 = 后 − 前】分档：负迁/平迁/高迁 [0,30)、[30,50)、提值 50+
        #   后 > 前  高迁 / 提值    长续约 1，积分 = 后 − 前   （79改139+10 → 70，109提值169 → 60，99+10 → 10）
        #   后 = 前  平迁 / 一般续约 长续约 1，积分 0        （139、199续签、139改129+10）
        #   后 < 前  负迁 / 降档    长续约 0，积分 0，标「降档不计业绩」（139改99+10：139→109 也是降）
        #   没写原档的高迁 / 提值   长续约 1，积分待核对
        #   没有档位数字的（云盒、+10） 纯加装：长续约 0，积分 = 附加
        lift = _LIFT.search(fee)
        # 存量带副宽：每个副宽是新开的一条宽带，算新增宽带（09-29 用户定）；续约本身照下面算
        out["broadband"] = len(_SUB_BB.findall(fee))
        if up:
            before = _part_total(fee[:up.start(2)])
            after = _part_total(fee[up.start(2):])
            if lift and after < before:
                # 「169提值60」：后面的数比前面小，那是直接写的提值积分
                out["renew"] = 1
                out["points"] = int(lift.group(1)) + addon
                out["tier"] = before
            elif after > before:
                out["renew"] = 1
                out["points"] = after - before
            elif after == before:
                # 平迁（139改129+10：139 → 139）：总和没变，提值 0
                out["renew"] = 1
                out["points"] = 0
            else:
                out["downgrade"] = True
        elif re.search(r"负迁|降档", fee + raw_kind):
            out["downgrade"] = True
        elif lift:
            # 「提值60」：只写了提值积分
            out["renew"] = 1
            out["points"] = int(lift.group(1)) + addon
        elif re.search(r"高迁|提值|拆机维系|维系", fee + raw_kind):
            # 升到更高档但没写原档位（「199高迁」），差价算不出来：
            # 积分留给晒单里写的「价值积分」，没写就由 build_order 标待核对
            out["renew"] = 1
            out["points"] = addon
            out["points_unknown"] = True
        elif re.search(r"续|平迁", fee) or re.search(r"续|平迁", raw_kind) or base >= 19:
            # 平续 / 只写了档位的一般续约：长续约记 1，积分 = 总和 − 原档位（加装、副宽都算：「199续约加副宽60」60）
            out["renew"] = 1
            out["points"] = max(_part_total(fee) - base, 0)
        else:
            # 纯加装（「云盒」「+10」），不算长续约，只算附加值
            out["points"] = max(_part_total(fee), addon)
    return out


def _same_collector(a: Order, b: Order) -> bool:
    """同一个揽收人；两边都没写揽收人时退到「同一个发消息的人」。"""
    if a.collector and b.collector:
        return a.collector == b.collector
    return bool(a.sender_id) and a.sender_id == b.sender_id


@dataclass
class GroupStat:
    """「晒单数据」里的一行：一个小组的累计。"""
    team: str
    label: str
    members: str
    broadband: int = 0
    high: int = 0
    renew: int = 0
    giga: int = 0
    card: int = 0
    box: int = 0
    points: int = 0
    orders: int = 0
    pending: int = 0


@dataclass
class TeamStat:
    team: str
    broadband: int = 0
    high: int = 0
    renew: int = 0
    giga: int = 0
    card: int = 0
    box: int = 0
    points: int = 0
    orders: int = 0
    pending: int = 0          # 待核对的单数
    progress: float = 0.0
    rank: int = 0


class BranchRoster:
    """「全部」视图用的名册：战队 = 支局（按总表分类的顺序），每个支局一个「全支局」小组；查人走四张名册。"""
    ALL = "（按支局汇总）"

    def __init__(self, branches, rosters=None) -> None:
        self.teams = list(branches)
        self.people: dict = {}
        self._rs = rosters

    def normalize_team(self, name: str) -> str:
        return (name or "").strip()

    def lookup(self, person: str) -> tuple[str, str]:
        if self._rs is None:
            return "", ""
        i = self._rs.identify_name(person)
        return (i.group or i.region + i.role, i.branch) if i.role != "未识别" else ("", "")

    def groups(self) -> list:
        return [GroupInfo(team=b, label="全支局", members=self.ALL) for b in self.teams]

    def maybe_reload(self) -> bool:
        return False


class Campaign:
    """一次活动的全部订单 + 汇总。落盘，跨重启不丢。"""

    def __init__(self, roster: Roster, targets: Targets | None = None,
                 path: str | Path | None = None, rosters=None) -> None:
        self.roster = roster
        # 多群模式：四张名册（regions.RosterSet）。有它就按人分城区 / 农村、活动 / 非活动
        self.rosters = rosters
        self._readonly = False
        self.targets = targets or Targets()
        self.path = Path(path) if path else DATA / "campaign.json"
        self.orders: list[Order] = []
        self._seen: set[str] = set()
        self._lock = threading.Lock()
        self._load()

    # ---------- 存取 ----------

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("活动数据读不了，当作空的重来：%s", exc)
            return
        for d in raw.get("orders") or []:
            try:
                o = Order(**{k: v for k, v in d.items()
                             if k in Order.__dataclass_fields__})
                self.orders.append(o)
                if o.msg_id:
                    self._seen.add(o.msg_id)
            except Exception:
                continue
        log.info("活动数据：已有 %d 单", len(self.orders))
        # 按当前名册 / 归组规则把旧单重算一遍（名册格式变过、规则改过的旧单才能对上小组）。
        # 只改内存不写盘：MCP 进程也会建 Campaign，台账只许机器人进程写；机器人下次记单时一并存
        moved = self.reassign(save=False)
        if moved:
            log.info("按当前名册重新归组：%d 单", moved)

    def _save(self) -> None:
        if self._readonly:
            return                       # 区域视图只读，不许写台账
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(
                {"orders": [asdict(o) for o in self.orders]},
                ensure_ascii=False, indent=2), encoding="utf-8")
            keep_version(self.path)          # 写之前留底：一天之内写坏也能退回
            tmp.replace(self.path)
        except Exception as exc:
            log.warning("活动数据写不进去：%s", exc)

    def refresh_roster(self) -> None:
        """
        名册文件变了就重载并把已有单重新归队。记单前、出表前各调一次 ——
        台账只允许机器人这一个进程写，别的进程（MCP）只改名册文件。
        """
        if self._readonly:
            return
        changed = self.roster.maybe_reload()
        if self.rosters is not None:
            changed = self.rosters.maybe_reload() or changed
        if changed:
            moved = self.reassign()
            log.info("按新名册重新归队：%d 单有变化", moved)

    def view_all(self, branches: list) -> "Campaign":
        """
        全部人的单按【落地支局】汇总（攻坚群问答、TeleAgent「全部」用，和总表一个口径）。只读。
        每一单复制一份把 战队 换成支局，原单不动。
        """
        v = Campaign.__new__(Campaign)
        v.rosters = None
        v._readonly = True
        v.roster = BranchRoster(branches, self.rosters)
        v.targets = Targets()
        v.path = self.path
        v._seen = set()
        v._lock = threading.Lock()
        with self._lock:
            v.orders = [replace(o, team=o.branch or "未归属", group=BranchRoster.ALL) for o in self.orders]
        return v

    def view(self, region: str, targets: "Targets | None" = None, day: str = "") -> "Campaign":
        """
        某个区域的活动人员的单（城区 / 农村活动群的通报、晒单表用）。只读：不存盘、不重新归组。
        订单对象和主台账是同一批，统计时只读不改。
        day（YYYY-MM-DD，不给 = 今天）：表里的小组按那天的活动名单画（国庆每天名单不同，09-29）。
        """
        from .regions import ACTIVITY
        v = Campaign.__new__(Campaign)
        v.rosters = None
        v._readonly = True
        day = day or datetime.now().strftime("%Y-%m-%d")
        v.roster = self.rosters.on(day).activity[region] if self.rosters is not None else self.roster
        v.targets = targets or self.targets
        v.path = self.path
        v._seen = set()
        v._lock = threading.Lock()
        with self._lock:
            v.orders = [o for o in self.orders if o.region == region and o.role == ACTIVITY]
        return v

    def add(self, order: Order) -> bool:
        """
        追加一单。按 msg_id 判重 —— 同一条消息被重复拉到时不会重复计数。
        返回 True 表示确实是新单。
        """
        self.refresh_roster()
        with self._lock:
            if order.msg_id and order.msg_id in self._seen:
                return False
            if not order.at:
                order.at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            self._place(order)
            self.orders.append(order)
            if order.msg_id:
                self._seen.add(order.msg_id)
            self._save()
            return True

    def find(self, msg_id: str) -> "Order | None":
        """按来源消息找单。合并进旧单的那条找不到（msg_id 只记在 _seen 里）。"""
        if not msg_id:
            return None
        with self._lock:
            for o in reversed(self.orders):
                if o.msg_id == msg_id:
                    return o
        return None

    def flag(self, msg_id: str, note: str) -> bool:
        """给某条单打上待核对标记并落盘。单不删 —— 让人在 Excel 里看一眼再定。"""
        with self._lock:
            for o in reversed(self.orders):
                if o.msg_id == msg_id:
                    o.needs_review = True
                    if note and note not in (o.note or ""):
                        o.note = (o.note + " " + note).strip()
                    self._save()
                    return True
        return False

    # ---------- 同一单重发（更正）----------

    def find_mergeable(self, order: Order) -> Order | None:
        """
        找出「其实是同一单」的已有记录 —— 现在清单里只有文字晒单，规则就两条：

          ① 同揽收人 + 同单号 + 同一天    晒单头上的「9月21日第3单」是每个人自己数的，
                                          所以必须连人一起比；重发 = 更正
          ② 业务号码相同 + 同一天 + 同类型  没写单号、或单号打错时的兜底。
                                          类型不同（新增后又加装云盒）是两笔业务，不合

        其余一律当两单。以前还有「同人同天档位不冲突」「同人 5 分钟内」两层，
        那是给照片设计的（照片没号码）；照片不进清单后只剩误伤 —— 实测同一人
        一天办两单 139、第二单没填号码，就被吞成一单。已删。
        """
        if not self.orders:
            return None
        day = order.at[:10]
        with self._lock:
            for old in reversed(self.orders):
                if old.at[:10] != day:
                    continue
                if (order.serial and old.serial == order.serial
                        and _same_collector(old, order)):
                    return old
                if (order.contact and old.contact == order.contact
                        and old.kind == order.kind):
                    return old
        return None

    def looks_duplicate(self, order: Order) -> bool:
        """
        没号码、也没匹配上单号，但同人同天同档位已经有一单 —— 可能是重发也可能真是两单。
        照记，标黄让人看一眼；漏合并的代价是人看一眼，误合并的代价是排名少一单。
        """
        if order.contact:
            return False
        day = order.at[:10]
        with self._lock:
            return any(old.at[:10] == day and old.fee == order.fee
                       and _same_collector(old, order) for old in self.orders)

    def merge_into(self, old: Order, new: Order) -> None:
        """
        重发的那条【整体覆盖】先发的 —— 更正才能生效（积分填错改一下、加载项补一下）。
        只保留最早的时间和最初的 msg_id；重发次数记在 source 里。
        """
        with self._lock:
            keep_at, keep_msg = old.at, old.msg_id
            times = 1
            m = re.search(r"×(\d+)$", old.source or "")
            if m:
                times = int(m.group(1))
            for f in Order.__dataclass_fields__:
                setattr(old, f, getattr(new, f))
            old.at, old.msg_id = keep_at, keep_msg
            old.source = f"文字晒单×{times + 1}"
            # 归队要按名册重做一次 —— 上面整体覆盖绕过了 add() 里的名册查询
            if old.collector:
                grp, team = self.roster.lookup(old.collector)
                if grp:
                    old.group = grp
                if team:
                    old.team = team
            old.team = self.roster.normalize_team(old.team)
            if new.msg_id:
                self._seen.add(new.msg_id)
            self._save()

    # ---------- 汇总 ----------

    def stats(self, since: str = "", until: str = "") -> list[TeamStat]:
        """
        按战队汇总并排名。没有任何订单的战队也会出现，进度为 0。

        since / until 是 "YYYY-MM-DD HH:MM:SS" 前缀比较，用来切时段 ——
        原表的透视表就是按日期筛过的（Sheet3 只统计 8.21 那天），
        通报也是按时段发的，所以这个筛选是必需的，不是可选项。
        """
        t = self.targets
        by: dict[str, TeamStat] = {name: TeamStat(team=name)
                                   for name in self.roster.teams}
        with self._lock:
            orders = [o for o in self.orders
                      if (not since or o.at >= since) and (not until or o.at <= until)]

        for o in orders:
            name = o.team or "未归队"
            s = by.setdefault(name, TeamStat(team=name))
            s.broadband += o.broadband
            s.high += o.high
            s.renew += o.renew
            s.giga += o.giga
            s.card += o.card
            s.box += o.box
            s.points += o.points
            s.orders += 1
            if o.needs_review:
                s.pending += 1

        for s in by.values():
            s.progress = (
                (s.broadband / t.broadband * t.w_broadband if t.broadband else 0)
                + (s.high / t.high * t.w_high if t.high else 0)
                + (s.box / t.box * t.w_box if t.box else 0)
                + (s.points / t.points * t.w_points if t.points else 0)
            )

        # 排名只在正式战队之间排，「未归队」那一桶不参与。
        # sorted 是稳定的：同分的保持名册顺序，都是 0 的时候不会每次乱跳。
        # ⚠️ rank 只是排序位置：0 分的也会拿到号，但通报 / 表 / 图里
        # 一律按「progress > 0 才算名次」显示（0 分写「-」），别拿 rank 直接说第几名
        ranked = sorted((s for s in by.values() if s.team in self.roster.teams),
                        key=lambda s: s.progress, reverse=True)
        for i, s in enumerate(ranked, 1):
            s.rank = i
        extra = [s for s in by.values() if s.team not in self.roster.teams]
        return ranked + extra

    def group_stats(self, since: str = "", until: str = "") -> list[GroupStat]:
        """
        按小组汇总，顺序和名册一致；一单都没有的小组也出现（全 0）。

        揽收人不在名册的单归到最后一行「未归队」，让人一眼看到有几单没归上，
        而不是悄悄消失。
        """
        rows = [GroupStat(g.team, g.label, g.members) for g in self.roster.groups()]
        index = {(r.team, r.members): r for r in rows}
        # 揽收人不在名册、但晒单里写了支局的单：归到该战队末尾的「未分组」行，
        # 这样战队合计和 stats() 对得上（那边是按 o.team 加的）
        unassigned: dict[str, GroupStat] = {}
        orphan = GroupStat("未归队", "", "")
        with self._lock:
            orders = [o for o in self.orders
                      if (not since or o.at >= since) and (not until or o.at <= until)]
        for o in orders:
            r = index.get((o.team, o.group))
            if r is None and o.team in self.roster.teams:
                r = unassigned.setdefault(
                    o.team, GroupStat(o.team, "未分组", "揽收人不在名册"))
            if r is None:
                r = orphan
            r.broadband += o.broadband
            r.high += o.high
            r.renew += o.renew
            r.giga += o.giga
            r.card += o.card
            r.box += o.box
            r.points += o.points
            r.orders += 1
            if o.needs_review:
                r.pending += 1
        for team, extra in unassigned.items():
            last = max((i for i, r in enumerate(rows) if r.team == team), default=-1)
            rows.insert(last + 1, extra)
        if orphan.orders:
            rows.append(orphan)
        return rows

    # 归组时加的备注：重算前先摘掉，免得名册改了以后旧备注还挂着
    _PLACE_NOTES = ("晒单人不在名册，按揽收人归组", "晒单人和揽收人都不在名册",
                    "揽收人不在名册且没写支局", "揽收人不在名册")

    def _place_multi(self, order: "Order") -> None:
        """
        多群模式归组（09-29）：按人分类，不按群。
          ① 发消息的人 → 四张名册 → 城区 / 农村 × 活动 / 非活动
          ② 对不上 → 按晒单里的「揽收人」再查一次
          ③ 还对不上 → 未识别：只进攻坚群总表（按晒单里写的支局），标黄
        活动人员的 小组 / 战队 按他那区的活动名册；非活动人员 战队 记成他的支局。
        落地支局（总表用）= 晒单里写的「支局」；没写按人查到的支局。
        """
        from .regions import ACTIVITY, UNKNOWN, branch_stem
        rs = self.rosters.on((order.at or "")[:10])     # 国庆每天名单不同：按这单那天的名单认人（09-29）
        if not order.written_branch:
            # 第一次归组时 team 还是晒单里写的「支局」（build_order 放进来的），先存下来；
            # 老记录没有这个字段，branch 里存的就是当时写的支局
            order.written_branch = order.branch or branch_stem(order.team)
        ident = rs.identify_sender(order.sender_id, order.written_branch) if order.sender_id else None
        poster_branch = ident.branch if ident is not None and ident.role != UNKNOWN else ""
        order.poster = ident.name if ident and ident.role != UNKNOWN else ""
        if (ident is None or ident.role == UNKNOWN) and order.collector:
            by_collector = rs.identify_name(order.collector, order.written_branch)
            if by_collector.role != UNKNOWN:
                ident = by_collector
                if order.sender_id:
                    order.note = (order.note + " 晒单人不在名册，按揽收人归组").strip()
        if ident is not None and ident.role != UNKNOWN:
            order.region, order.role, order.community = ident.region, ident.role, ident.community
            if ident.role == ACTIVITY:
                order.group, order.team = ident.group, ident.team
            else:
                order.group, order.team = "", ident.branch
        # 总表归哪个支局（09-29 用户定）：按揽收人查名册 → 他的支局；揽收人没写 / 查不到 → 晒单里写的支局 → 晒单人的支局
        #（用户确认过不会出现揽收人是别的支局的人）
        by_collector = rs.identify_name(order.collector, order.written_branch) if order.collector else None
        if by_collector is not None and by_collector.role != UNKNOWN and by_collector.branch:
            order.branch = by_collector.branch
        else:
            order.branch = order.written_branch or poster_branch or (ident.branch if ident is not None else "")
        if ident is None or ident.role == UNKNOWN:
            order.region, order.role, order.community = "", UNKNOWN, False
            order.needs_review = True
            if "都不在名册" not in order.note:
                order.note = (order.note + " 晒单人和揽收人都不在名册").strip()
            order.team = order.branch

    def _place(self, order: "Order") -> None:
        """
        这单算到哪个小组 / 战队（09-24 用户定：【发消息的人】所在的小组；揽收人只记在表里）。

          ① 发消息的人（企微账号 ID 按拼音 / userid_map 对到名册里的人）-> 他的小组、战队
          ② 对不上（名册外的人、账号不是拼音）-> 退回按晒单里的「揽收人」查名册，备注说明
          ③ 揽收人也查不到 -> 按晒单里写的「支局」只归战队，标待核对
        名册优先于晒单里手填的「支局」：支局可能填错，小组也只能从名册查。
        """
        if self.rosters is not None:
            self._place_multi(order)
            return
        from .checkin import sender_name       # 账号 ID -> 名册姓名，和打卡归组同一套
        order.poster = sender_name(order.sender_id, self.roster) if order.sender_id else ""
        grp, team = self.roster.lookup(order.poster) if order.poster else ("", "")
        if team:
            order.group, order.team = grp, team
        elif order.collector:
            grp, team = self.roster.lookup(order.collector)
            if team:
                order.group, order.team = grp, team
                if order.sender_id:
                    order.note = (order.note + " 晒单人不在名册，按揽收人归组").strip()
            elif order.team:
                # 名册里没这个人，但消息里写了支局 —— 先按支局归，标记待核对
                order.needs_review = True
                order.note = (order.note + " 揽收人不在名册").strip()
            else:
                order.needs_review = True
                order.note = (order.note + " 揽收人不在名册且没写支局").strip()
        order.team = self.roster.normalize_team(order.team)

    def reassign(self, save: bool = True) -> int:
        """名册更新后，把已有订单按同一套规则（_place）重新归组。返回改动的单数。"""
        changed = 0
        with self._lock:
            for o in self.orders:
                before = (o.group, o.team, o.note, o.poster)
                note = o.note
                removed = any(n in note for n in self._PLACE_NOTES)
                for n in self._PLACE_NOTES:
                    note = note.replace(n, "")
                o.note = " ".join(note.split())
                self._place(o)
                if removed:
                    # 「晒单人不在名册」只是说明归组依据，不算要核对
                    o.needs_review = bool(o.note.replace("晒单人不在名册，按揽收人归组", "").strip())
                if (o.group, o.team, o.note, o.poster) != before:
                    changed += 1        # 一单只计一次
            if changed and save:
                self._save()
        return changed

# ---------- 晒单文字 -> Order ----------

# 「业务晒单时刻」模板里的字段。冒号中英文都有，前面还常挂 emoji，
# 所以不锚定行首，只按「字段名 + 冒号」抓。
# ⚠️ 冒号后面只吃【空格和 Tab】，不能用 \s —— \s 会吃掉换行，
# 于是空字段会抓到下一行的内容（实测「是否加载云盒：」空着时抓到了
# 下一行的「是否加载智屏」，三项全被判成已加载）。
_SP = r"[ 	　]*"
# 值是「冒号后第一个不含空白的词」，而且这个词里【不能再有冒号】——
# 否则单行模板「业务类型： 办理套餐： 业务号码：」里空着的字段会把
# 下一个字段名当成值（实测 kind 抓成了「办理套餐：」）
_VAL = r"(?!\S*[:：])(\S+)"
_FIELDS = {
    "branch":    re.compile(r"支" + _SP + r"局" + _SP + r"[:：]" + _SP + _VAL),
    "kind":      re.compile(r"业务类型" + _SP + r"[:：]" + _SP + _VAL),
    # 办理套餐取到这一行结尾（「129改139 加10」员工会打空格），再在下一个字段名处截断，见 _fee_value
    "fee":       re.compile(r"办理套餐" + _SP + r"[:：]" + _SP + r"([^\n\r]*)"),
    "points":    re.compile(r"价值积分" + _SP + r"[:：]" + _SP + r"(\d+)"),
    "cards":     re.compile(r"号卡数量" + _SP + r"[:：]" + _SP + r"(\d+)"),
    "collector": re.compile(r"揽收人" + _SP + r"[:：]" + _SP + _VAL),
}

# 「是否加载X」后面填了东西才算加载。留空 / 无 / 否 都算没有。
# 值只取一个词，遇到空白或下一个「是否加载」就停。以前用 (.*) 吃到行尾，
# 两个字段挤一行（「是否加载智屏：否 🌐是否加载FTTR：否」）时智屏那格
# 抓到「否 🌐是否加载FTTR：否」，不在否定词表里，被判成已加载（对抗测试踩过）
_LOADED_VAL = r"((?:(?!是否)\S)*)"
_LOADED = {
    "ai":   re.compile(r"是否加载" + _SP + r"AI" + _SP + r"[:：]" + _SP + _LOADED_VAL, re.I),
    "box":  re.compile(r"是否加载云盒" + _SP + r"[:：]" + _SP + _LOADED_VAL),
    "scrn": re.compile(r"是否加载智屏" + _SP + r"[:：]" + _SP + _LOADED_VAL),
    "fttr": re.compile(r"是否加载" + _SP + r"FTTR" + _SP + r"[:：]" + _SP + _LOADED_VAL, re.I),
}
# 新模板（09-29）两栏：只认「是」，空着 / 否都是否；模板里没这栏（旧模板）记空串
_UPGRADED = {
    "up_giga": re.compile(r"是否升级" + _SP + r"(?:千兆|1000" + _SP + r"M?)" + _SP + r"(?:[/／、或]" + _SP + r"500" + _SP + r"(?:M|兆)?)?"
                          + _SP + r"[:：]" + _SP + _LOADED_VAL, re.I),
    "up_2000": re.compile(r"是否升级" + _SP + r"(?:2000|两千|二千)" + _SP + r"(?:M|兆)?" + _SP + r"[:：]" + _SP + _LOADED_VAL, re.I),
}
# 模板里其它字段名：抓「是否加载」的值时碰到这些就到头了
_NEXT_FIELD = re.compile(r"揽收人|号卡数量|业务类型|办理套餐|支局|价值积分|业务号码|是否加载|是否升级|小组")
_NEGATIVE = {"", "无", "否", "没有", "没", "暂无", "无需", "不需要", "不用", "不要", "未加载",
             "未", "不", "n", "no", "none", "x", "×", "不加载", "-", "/", "0"}
# 以这些字开头的也算否：「未装」「不装」「无此项」「没加」……
_NEGATIVE_PREFIX = ("未", "不", "无", "没", "否")


def _upgraded(text: str, key: str) -> str:
    """「是否升级千兆/500M：是」→ 是；空着 / 否 → 否；模板里没这一栏 → 空串。"""
    m = _UPGRADED[key].search(text)
    if not m:
        return ""
    v = re.sub(r"[^\w]", "", _NEXT_FIELD.split(m.group(1), 1)[0])
    return "是" if v.startswith("是") else "否"


def _fee_value(raw: str) -> str:
    """办理套餐这一行：截到下一个字段名，去掉尾巴上的 emoji / 标点。"""
    v = _NEXT_FIELD.split(raw or "", 1)[0]
    return re.sub(r"[^\w）)%]+$", "", v.strip())


def _loaded(text: str, key: str) -> bool:
    m = _LOADED[key].search(text)
    if not m:
        return False
    raw = m.group(1)
    # 单行模板里这格空着（或值和下一格挤在一起没空格）时会抓到下一个字段名：
    # 「揽收人:郭静」「是揽收人:郭静」。从字段名处截断，只看前半段
    raw = _NEXT_FIELD.split(raw, 1)[0]
    # 去掉 emoji、空白，只留字词和 - /，再查否定词表
    v = re.sub(r"[^\w/\-]", "", raw).lower()
    return v not in _NEGATIVE and not v.startswith(_NEGATIVE_PREFIX)


# 一条消息里贴了两张模板（「第5、6单」）：业务类型 / 办理套餐 出现两次以上。
# parse_fields 用 search 只取第一次，第二单会静默丢掉。
# 口径和模型路径的 multiple 一致：不拆单，放弃，留给人补
def has_multiple_orders(text: str) -> bool:
    return any(len(_FIELDS[k].findall(text or "")) > 1 for k in ("kind", "fee"))


# 严格解析只认「在报一笔已办成的业务」。带疑问的文字（「这样填对吗」
# 「积分是填20还是169」）就算带了「字段名+冒号」也不是晒单，那是在问怎么填。
# 命中就不走严格路径，交给模型（提示词里 is_order 会判 false）
_ASKING = re.compile(r"[?？]|请教|怎么填|填对|对不对|是填|还是填|该填|填什么")


# 晒单模板里会出现的字段名。数「命中了几个」来判断一段文字像不像晒单 ——
# 严格解析失败但命中够多的，交给模型容错解析（见 extract.py）
_FIELD_WORDS = ("支局", "业务类型", "办理套餐", "套餐", "价值积分", "积分", "号卡数量",
                "揽收人", "揽收", "是否加载", "是否升级", "业务号码", "晒单")


def looks_like_order(text: str) -> int:
    """像晒单的程度：命中了几个模板字段名。0 = 完全不像。"""
    return sum(1 for w in _FIELD_WORDS if w in (text or ""))


def parse_fields(text: str) -> dict:
    """
    严格解析：按「字段名 + 冒号」抓。返回的字典各键都在，抓不到的是空串 / None：
        kind fee branch collector points cards   —— 字符串
        ai box scrn fttr                          —— True / False（写了才 True）
    """
    got = {}
    for k, rx in _FIELDS.items():
        m = rx.search(text or "")
        got[k] = m.group(1).strip() if m else ""
    got["fee"] = _fee_value(got["fee"])
    for k in _LOADED:
        got[k] = _loaded(text or "", k)
    for k in _UPGRADED:
        got[k] = _upgraded(text or "", k)
    return got


def build_order(fields: dict, text: str, msg_id: str = "", at: str = "",
                source: str = "文字晒单") -> "Order | None":
    """
    字段 -> Order。严格解析和模型容错解析共用这一段，规则只写一份：
        取值优先级：【消息里写了的以消息为准，没写的才用套餐推算】
        号卡类一律单C；千兆只对新增且 ≥139 记；认不出的业务类型标待核对
    缺业务类型或缺套餐就返回 None —— 那两样推不出来。
    text 是【原始】消息，只用来算号码哈希和抽小组成员，不外发。
    """
    kind, fee = (fields.get("kind") or "").strip(), (fields.get("fee") or "").strip()
    if not kind or not fee:
        return None
    # 第二道保险：值里带冒号、或者本身就是个字段名，说明正则被排版绕过了
    if re.search(r"[:：]", kind + fee) or kind in _FIELD_WORDS or fee in _FIELD_WORDS:
        return None

    # kpi 引用本模块，放函数里免得循环引用
    from .kpi import strip_speed, sub_broadbands   # 「139续约升500M」的 500 不是档位；副宽 60 算千兆
    d = derive(kind, strip_speed(fee).strip() or fee)
    raw_kind, kind = kind, normalize_kind(kind)
    # 号卡张数：只看【纠正后】的类型 —— 「业务类型:新增 / 套餐:19山城卡」
    # 经 derive 已经是单C。填了张数用张数，没填按 1 张（模板里这格常留空，
    # 以前留空记成 0，单号卡列系统性少计）；不是号卡单一律 0
    try:
        cards = int(str(fields.get("cards") or "0").strip() or 0)
    except ValueError:
        cards = 0
    card = (cards or 1) if d.get("kind") == "单C" else 0

    # 云盒/AI/智屏 是三项合一的口径（原表列名就是「云盒/AI视联网/智屏」），
    # 加载了几项就算几个
    box = sum(1 for k in ("box", "ai", "scrn") if fields.get(k))
    points_raw = str(fields.get("points") or "").strip()
    try:
        points = int(points_raw) if points_raw else d["points"]
    except ValueError:
        points = d["points"]

    o = Order(
        # 用 derive 纠正过的类型：号卡类会被改成「单C」，
        # 不管晒单里原本填的是不是「新增」
        at=at, kind=d.get("kind") or kind, fee=fee,
        collector=(fields.get("collector") or "").strip(),
        team=(fields.get("branch") or "").strip(),
        broadband=d["broadband"], high=d["high"], renew=d["renew"],
        card=card, box=box,
        fttr=1 if fields.get("fttr") else 0,
        loaded=",".join(n for k, n in (("box", "云盒"), ("ai", "AI"), ("scrn", "智屏"), ("fttr", "FTTR"))
                        if fields.get(k)),
        up_giga=fields.get("up_giga") or "", up_2000=fields.get("up_2000") or "",
        points=points,
        source=source, msg_id=msg_id,
        # 号码哈希和单号是「同一单重发」的判据；小组成员是照片时代留下的
        contact=_contact_key(text), members=_members_of(text),
        serial=_serial_of(text),
    )
    # 千兆/500兆（活动群的数）：和攻坚群总表同一套判定（kpi：新增看速率；存量看「是否升级」栏 + 提值 ≥10；
    # 副宽 60 另算），但【只数条数、不乘国庆系数】—— 2000M 高迁 / 平签 / 低签在活动群都记 1（09-29 用户定）。
    # ⚠️ 用 derive 纠正后的类型：「新增 / 399橙分期」纠正后是单C，不算宽带
    from .kpi import compute as kpi_compute
    k = kpi_compute(o.kind, fee, o.loaded, o.card or 1, o.up_giga, o.up_2000)
    o.giga = k.giga_lines
    # 新增宽带 / 中高套 / 云盒AI智屏 / 单号卡 也一样：和总表同一套判定，活动群只记个数（09-29 用户定）。
    # 新增宽带不折算（780 包年记 1）、明星机赠送不算；中高套 239 以上也记 1；云盒/AI/智屏 写了价钱才算、FTTR 不算进这列
    o.broadband, o.high, o.box = k.bb_lines, (1 if k.high else 0), k.box_items
    if o.kind == "单C":
        o.card = int(k.card)
    if any("待核对" in n for n in k.notes):
        o.needs_review = True
        o.note = (o.note + " " + "；".join(n for n in k.notes if "待核对" in n)).strip()
    # 业务类型写了个认不出的词（既不像新增也不像存量/单C）：
    # 照原文记，标出来让人看一眼，别让它悄悄按 0 计
    # 「业务类型:新增 / 套餐:109改139」：新增不该有「改」，用户会上强调别这样报（09-29）；出现了照算，标黄让人核对
    if o.kind == "新增" and _UPGRADE.search(fee):
        o.needs_review = True
        o.note = (o.note + " 新增单写了「改」，请核对是新增还是存量").strip()
    if o.kind not in ("新增", "存量", "单C"):
        o.needs_review = True
        o.note = (o.note + f" 业务类型「{raw_kind}」待核对").strip()
    # 高迁 / 提值 / 负迁 这类差价算不出来的，晒单里没写积分就标出来
    if d.get("downgrade"):
        # 降档（139改99、负迁、139改99+10 总和变小）：照记，不算长续约不算分
        o.note = (o.note + " 降档不计业绩").strip()
    elif d.get("equity"):
        # 「699权益兑换FTTR」：699 是权益金币不是档位，这单算不算业绩要人定
        o.needs_review = True
        o.note = (o.note + " 权益金兑换，请核对是否计业绩").strip()
    elif d.get("points_unknown") and not points_raw:
        o.needs_review = True
        o.note = (o.note + " 积分待核对").strip()
    return o


def parse_order(text: str, msg_id: str = "", at: str = "") -> "Order | None":
    """
    把群里的「业务晒单时刻」文字解析成一条 Order（严格解析）。

    这类消息字段是打好的，比 OCR 那条路准得多：
        支局 / 业务类型 / 办理套餐 / 价值积分 / 号卡数量 / 揽收人
        是否加载 AI / 云盒 / 智屏 / FTTR
    不像晒单就返回 None（缺套餐或缺业务类型都算不像）；
    字段写歪了的由 pipeline 交给模型容错解析，再走 build_order。
    """
    if not text:
        return None
    if _ASKING.search(text):
        return None            # 像在提问，让模型（或人）来判，别直接建单
    if has_multiple_orders(text):
        log.info("一条消息里报了多单（业务类型/办理套餐出现多次），不自动拆，留给人补")
        return None
    return build_order(parse_fields(text), text, msg_id, at)
