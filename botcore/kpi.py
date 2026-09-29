"""
国庆专项营销活动（10.1–10.7）五项指标：一单算多少（09-29 国庆版，规则见 docs/国庆多群版功能说明.md 第六节）。

    新增宽带 25%   中高套 30%   千兆/500兆 20%   套包 15%   单号卡 10%

只管「一单 -> 五个数」，算给哪个支局、目标、进度、排名在总表那边。
晒单【只认字，不靠符号】（员工不一定打「+」「M」「→」）：「129改139加10」「续约139升级五百兆加云盒」「升千兆」都认。
读不准的记 0 并在 notes 里说明，由总表标黄让人看。

千五（09-29 用户重定）：
  · 新增：看速率 —— 139 及以上融合（含 2000M）、138/198 单宽、1390/1990 包年、副宽 60 各记 1；
          79/99 融合、98 单宽、390/780 包年、副宽 38 不算（加了看家、FTTR 也不算）
  · 存量迁到 2000M（迁完宽带档位 239/299/399，只看宽带价格）：前后总和比，高迁 1.5 / 平签 1.2 / 低签 1.1
  · 存量其他：【速率升档】且【提值 ≥10】才记 1。
          速率先看晒单写的「升500兆 / 升级五百兆 / 升千兆」（写了就算升档），没写按档位查表
          （79=200M、99/129=300M、139/169=500M、199=千兆）；
          提值 = 办完的总和 − 原来的总和（付费套包算，权益兑换的不算）
套包：只有写了「权益 / 兑换 / 金币」的那样算权益兑换，其余都当付费；没写价钱按默认价（FTTR 25、云盒 / AI / 智屏 10、看家 9.9）

还在等领导定的：单号卡「剔除直管」—— 现在晒单写了「直管」就不算
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .campaign import HIGH_TIER_MIN, SOLO_GIGA_TIERS, _IS_CARD, _ONLY_SUBCARD, _TECH_TAG, normalize_kind

WEIGHTS = {"broadband": 0.25, "high": 0.30, "giga": 0.20, "pack": 0.15, "card": 0.10}
CAPPED = {"giga", "pack", "card"}     # 这三项进度最多算 130%，宽带、中高套不封顶
CAP = 1.30

TIER_2000M = 239          # 239 / 299 / 399 是 2000M
HIGH_13 = 239             # 中高套：239 及以上按 1.3 计
SUB_BB_GIGA_PRICE = 60    # 副宽 60 元 = 500M，算千五；38 元 = 300M 不算
GIGA_SPEED = 500          # 千五：500M 及以上
GIGA_LIFT = 10            # 存量：提值 ≥10
TIERS = {79, 99, 129, 139, 169, 199, 239, 299, 399}   # 融合档位（剥权益金数字时用来认「这是档位不是金币」）

# 新增宽带折算：写了这些字才折（1390 包年里的「390包年」不算）
_FOLD_THIRD = re.compile(r"明厨亮灶|(?<!\d)780\s*包年")
_FOLD_FIFTH = re.compile(r"酒店|短期|临时宽带|列收|(?<!\d)390\s*包年")
_NO_COUNT = re.compile(r"明星机")
_DIRECT = re.compile(r"直管")
_SOLO_WORD = re.compile(r"单宽|包年|包月")
_EQUITY_WORD = re.compile(r"权益|兑换|金币")

# ---- 速率：只认字 ----
# 「500M」「500兆」「1000MB」「五百兆」「千兆」「一千兆」「两千兆」「三百兆」「百兆」
_SPEED_UNIT = r"(?:(?<![\d.])[1-9]\d{2,3}\s*(?:MB|M|Ｍ|兆)|[一二两三五]?\s*千\s*兆|[一二两三五]\s*百\s*兆|百兆)"
# 跟在「升 / 升级 / 提速 …」后面、没写单位的：「升500」「升级1000」
_SPEED_BARE = r"(?<![\d.])(?:500|1000|2000)(?![\d.])"
_SPEED_ANY = re.compile(_SPEED_UNIT, re.I)
# 「升500M」「升级为五百兆」「提速到千兆」「300M升500M」「改1000兆」
_SPEED_UP = re.compile(
    rf"(?:(?P<frm>{_SPEED_UNIT})\s*(?:宽带)?\s*)?"
    r"(?:升级|升档|提速|提升|升|提|改为|改成|改|换成|换|转为|转|调整为|调到|调|到)"
    rf"\s*(?:了)?\s*(?:到|至|为|成)?\s*(?P<to>{_SPEED_UNIT}|{_SPEED_BARE})", re.I)
_SPEED_HINT = re.compile(r"提速|升速|高迁|升档")   # 写了要升但没写升到多少
_CN = {"一": 1, "二": 2, "两": 2, "三": 3, "五": 5}

# ---- 档位变化：「129改139」「199升239」「299续约299」「139换169」 ----
_TIER_UP = re.compile(
    r"(?<![\d.])(\d{2,4})\s*(?:元)?\s*(?:套餐)?\s*"
    r"(?:续约|续签|升档|升级|升|改为|改成|改|换成|换|转为|转|调整为|调到|调|迁转|迁|到)"
    r"\s*(?:了)?\s*(?:到|至|为|成)?\s*(\d{2,4})(?![\d.])")
_NUMF = re.compile(r"\d+(?:\.\d+)?")
_REBATE = re.compile(r"返\s*(\d+(?:\.\d+)?)")
# 权益金、金币数（699 / 300 / 599 …）不是钱：「权益金699」「699权益金兑换」都剥掉；档位（239 权益兑换 FTTR 的 239）留着
_EQUITY_NUM = re.compile(r"(?:权益金?|金币)\s*(\d+(?:\.\d+)?)|(\d+(?:\.\d+)?)\s*(?:元|个)?\s*(?:权益|金币|兑换)")

# ---- 套包 ----
_ITEM_PAT = {
    "FTTR": re.compile(r"FTTR|光纤到房间", re.I),
    "云盒": re.compile(r"云盒"),
    "智屏": re.compile(r"智屏"),
    "AI": re.compile(r"AI\s*视联网|视联网", re.I),
    "看家": re.compile(r"看家|守护|摄像头|9\.9"),
}
_P = r"(\d{1,2}(?:\.\d+)?)"     # 套包月费 1–2 位数（9.9 / 10 / 25），免得把「139云盒」的 139 当价钱
_PRICE = {
    "FTTR": re.compile(rf"(?:FTTR|光纤到房间)\s*(?:加|[+＋])?\s*{_P}(?![\d.])|(?<![\d.]){_P}\s*(?:元\s*)?(?:的\s*)?(?:FTTR|光纤到房间)", re.I),
    "云盒": re.compile(rf"云盒\s*(?:加|[+＋])?\s*{_P}(?![\d.])|(?<![\d.]){_P}\s*(?:元\s*)?(?:的\s*)?(?:轻)?云盒"),
    "智屏": re.compile(rf"智屏\s*(?:加|[+＋])?\s*{_P}(?![\d.])|(?<![\d.]){_P}\s*(?:元\s*)?(?:的\s*)?(?:天翼)?智屏"),
    "AI": re.compile(rf"(?:AI\s*)?视联网\s*(?:加|[+＋])?\s*{_P}(?![\d.])|(?<![\d.]){_P}\s*(?:元\s*)?(?:的\s*)?AI\s*视联网", re.I),
    "看家": re.compile(rf"(?:看家|守护|摄像头)\s*{_P}(?![\d.])|(?<![\d.]){_P}\s*(?:元\s*)?(?:的\s*)?(?:AI\s*)?(?:看家|守护|摄像头)", re.I),
}
_DEFAULT_PRICE = {"云盒": 10.0, "AI": 10.0, "智屏": 10.0, "FTTR": 25.0, "看家": 9.9}


@dataclass
class KPI:
    broadband: float = 0.0
    high: float = 0.0
    giga: float = 0.0
    pack: float = 0.0
    card: float = 0.0
    notes: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: round(getattr(self, k), 4) for k in WEIGHTS}


# ---------------------------------------------------------------- 读字

def _speed_value(t: str) -> int:
    """「500M」→500，「五百兆」→500，「千兆」「一千兆」→1000，「两千兆」→2000。"""
    t = re.sub(r"\s", "", t)
    m = re.match(r"\d+", t)
    if m:
        return int(m.group())
    if "千" in t:
        return 1000 * _CN.get(t[0], 1)
    if "百" in t:
        return 100 * _CN.get(t[0], 1)
    return 0


def _strip_equity_num(m: re.Match) -> str:
    v = m.group(1) or m.group(2)
    if m.group(2) and float(v) in TIERS:
        return m.group(0)                  # 「239权益兑换FTTR」：239 是档位，留着
    return m.group(0).replace(v, " ", 1)


def _money_text(fee: str) -> str:
    """剥掉速率（「升500兆」「千兆」）和权益金数字，剩下的数字都是档位和钱。"""
    s = _SPEED_UP.sub(" ", fee)
    s = _SPEED_ANY.sub(" ", s)
    return _EQUITY_NUM.sub(_strip_equity_num, s)


def _total(part: str) -> float:
    """一段文字里的钱加起来，减掉「返多少」：「139加10」→149，「169返60」→109，「169加9.9看家」→178.9。"""
    rebate = sum(float(x) for x in _REBATE.findall(part))
    return sum(float(x) for x in _NUMF.findall(_REBATE.sub(" ", part))) - rebate


def _tier_up(money: str) -> re.Match | None:
    """「129改139」这种档位变化；改成的档位 < 39 的不算（「139续约 10元云盒」的 10 是钱）。"""
    for m in _TIER_UP.finditer(money):
        if int(m.group(2)) >= 39:
            return m
    return None


def _speed_of(tier: int, solo: bool = False) -> int:
    """档位 -> 速率（晒单没写速率时查这个）。单宽 / 包年另一张表。"""
    if solo:
        return 1000 if tier in (198, 1990) else 500 if tier in (138, 1390) else 300
    if tier >= TIER_2000M:
        return 2000
    if tier >= 199:
        return 1000
    if tier >= HIGH_TIER_MIN:
        return 500
    if tier >= 99:
        return 300
    return 200 if tier else 0


def _sub_bbs(fee: str) -> list[bool]:
    """每条副宽是不是 500M 以上：副宽 60（或「60元副宽」）、「副宽500兆」算；38 元 300M、没写价钱不算。"""
    out = []
    for m in re.finditer(r"副宽", fee):
        after = fee[m.end():m.end() + 12]
        before = fee[max(0, m.start() - 6):m.start()]
        pa = re.match(r"\s*(\d{2,3})(?![\d.]|\s*(?:MB|M|Ｍ|兆))", after, re.I)
        pb = re.search(r"(?<![\d.])(\d{2})\s*元\s*(?:的\s*)?$", before)
        sp = re.match(rf"\s*(?:\d{{2,3}}\s*元?\s*)?({_SPEED_UNIT})", after, re.I)
        price = int(pa.group(1)) if pa else (int(pb.group(1)) if pb else 0)
        out.append(price >= SUB_BB_GIGA_PRICE or bool(sp and _speed_value(sp.group(1)) >= GIGA_SPEED))
    return out


_SUB_BB_TEXT = re.compile(r"(?:(?<![\d.])\d{2}\s*元\s*(?:的\s*)?)?副宽\s*(?:\d{2,3}(?![\d.]))?")


# ---------------------------------------------------------------- 套包

def _items(fee: str, loaded: str) -> list[str]:
    """这一单加载了哪些套包：晒单「是否加载」勾的 + 套餐里写的字。"""
    names = [x for x in (loaded or "").split(",") if x]
    for n, pat in _ITEM_PAT.items():
        if n not in names and pat.search(fee):
            names.append(n)
    return names


def _price(name: str, fee: str) -> float | None:
    """套餐里写了这一样的价钱就返回价钱（「FTTR25」「25元FTTR」「智屏10」「加10云盒」），没写返回 None。"""
    m = _PRICE[name].search(fee) if name in _PRICE else None
    if not m:
        return None
    v = m.group(1) or m.group(2)
    return float(v) if v else None


def _equity_items(fee: str, names: list[str]) -> set[str]:
    """
    哪几样是权益兑换的：只看「权益 / 兑换 / 金币」这几个字挨着哪一样。
    「兑换FTTR」「权益金兑换云盒」→ 后面那样；「FTTR权益兑换」→ 前面那样；
    套餐里一样都没写、只勾了加载（「699权益兑换」+ 勾 FTTR）→ 勾的都算兑换。
    """
    runs = list(re.finditer(r"(?:权益金?|金币|兑换)(?:\s*(?:权益金?|金币|兑换|\d+(?:\.\d+)?|元|个))*", fee))
    if not runs:
        return set()
    spans = {n: [m.span() for m in _ITEM_PAT[n].finditer(fee)] for n in names if n in _ITEM_PAT}
    if not any(spans.values()):
        return set(names)
    out = set()
    for r in runs:
        nxt = [(s - r.end(), n) for n, ss in spans.items() for s, _e in ss if 0 <= s - r.end() <= 3]
        prv = [(r.start() - e, n) for n, ss in spans.items() for _s, e in ss if 0 <= r.start() - e <= 4]
        if nxt:
            out.add(min(nxt)[1])
        elif prv:
            out.add(min(prv)[1])
    return out


def _paid_items(fee: str, loaded: str) -> list[tuple[str, float]]:
    """付了月费的套包 [(名字, 月费)]：写了「权益 / 兑换 / 金币」的那样不算，其余按写的价钱，没写按默认价。"""
    names = _items(fee, loaded)
    equity = _equity_items(fee, names)
    out = []
    for n in names:
        if n in equity:
            continue
        p = _price(n, fee)
        out.append((n, p if p is not None else _DEFAULT_PRICE[n]))
    return out


def _mentioned(name: str, text: str) -> bool:
    pat = _ITEM_PAT.get(name)
    return bool(pat and pat.search(text))


def pack_score(paid: list[tuple[str, float]]) -> float:
    """套包分：一单月费合计 V，1 + (V − 9.9) ÷ 10（V ≤ 9.9 就是 1）；每台智屏再加 月费 × 0.3。"""
    v = sum(p for _n, p in paid)
    if v <= 0:
        return 0.0
    score = 1 + max(0.0, v - 9.9) / 10
    score += sum(p * 0.3 for n, p in paid if n == "智屏")
    return score


# ---------------------------------------------------------------- 一单 -> 五项

def compute(kind: str, fee: str, loaded: str = "", cards: int = 1) -> KPI:
    """一单 -> 五项。kind / fee 是晒单里的「业务类型」「办理套餐」原文，loaded 是 Order.loaded。"""
    k = KPI()
    fee = _TECH_TAG.sub("", (fee or "").strip())
    kind = normalize_kind(kind)
    money = _money_text(fee)
    up = _tier_up(money)
    nums = _NUMF.findall(money)
    base = int(float(nums[0])) if nums else 0
    after_base = int(up.group(2)) if up else base

    if _NO_COUNT.search(fee):
        k.notes.append("明星机赠送宽带不计量")
        return k

    # 号卡：19 元及以上，副卡、直管不算
    if _IS_CARD.search(fee) or _ONLY_SUBCARD.search(fee) or kind == "单C":
        if _ONLY_SUBCARD.search(fee):
            return k
        if _DIRECT.search(fee):
            k.notes.append("直管不计单号卡")
            return k
        if after_base >= 19:
            k.card = float(cards or 1)
        return k

    # 只有权益兑换、没有档位（「699权益兑换FTTR」）：什么都不算
    if _EQUITY_WORD.search(fee) and not base:
        k.notes.append("权益金兑换不计量")
        return k

    paid = _paid_items(fee, loaded)
    k.pack = pack_score(paid)

    subs = _sub_bbs(fee)
    solo = bool(_SOLO_WORD.search(fee)) or (bool(subs) and not _NUMF.search(_SUB_BB_TEXT.sub(" ", money)))
    if kind == "新增" or (solo and kind != "存量"):
        # ---- 新增宽带 ----
        fold = 1.0
        if _FOLD_THIRD.search(fee):
            fold = 1 / 3
        elif _FOLD_FIFTH.search(fee):
            fold = 1 / 5
        if solo:
            if subs:                               # 单办副宽
                k.broadband = fold * len(subs)
                k.giga = float(sum(subs))
            else:                                  # 单宽 / 包年：138/198/1390/1990 是 500M 以上
                k.broadband = fold
                k.giga = 1.0 if base in SOLO_GIGA_TIERS else 0.0
            return k
        k.broadband = fold * (1 + len(subs))
        # ---- 中高套（只算新增融合）----
        if base >= HIGH_13:
            k.high = 1.3
        elif base >= HIGH_TIER_MIN:
            k.high = 1.0
        # ---- 千五：每条宽带各算一次；融合 139 及以上 500M 起，副宽 60 另算 ----
        k.giga = (1.0 if base >= HIGH_TIER_MIN else 0.0) + sum(subs)
        return k

    if kind == "存量":
        # 迁转前后的宽带档位和总和（总和 = 档位 + 付费套包 − 返费）
        before_tier = base
        after_tier = after_base
        if up:
            before_total = _total(money[:up.start(2)])
            after_text = money[up.start(2):]
        else:
            before_total = float(base)
            after_text = money
        # 套包没写价钱的按默认价补：这次办的付费套包合计比写出来的附加钱多，就按套包算
        addon_written = _total(after_text) - after_tier
        addon_paid = sum(p for n, p in paid if _mentioned(n, after_text) or not _mentioned(n, money))
        after_total = after_tier + max(addon_written, addon_paid)

        if after_tier >= TIER_2000M and not solo:
            # 2000M：是不是 2000M 只看宽带价格；高迁 / 平签 / 低签按前后总和比
            if up:
                k.giga = 1.5 if after_total > before_total else (1.2 if after_total == before_total else 1.1)
            elif after_total > before_total or re.search(r"高迁|提值", fee):
                k.giga = 1.5                       # 「299续约加10元云盒」：总和变高 = 高迁
            elif re.search(r"降档|负迁|低签", fee):
                k.giga = 1.1
            else:
                k.giga = 1.2                       # 「299续约」：平签
            return k

        # 其他：速率升档 且 提值 ≥10
        sp = _SPEED_UP.search(fee)
        if sp:
            # 晒单写了「升500兆 / 升千兆」：原来的 139 可能不是 500M，以写的为准
            after_speed = _speed_value(sp.group("to"))
            upgraded = not sp.group("frm") or _speed_value(sp.group("frm")) < after_speed
        else:
            after_speed = _speed_of(after_tier, solo)
            upgraded = after_speed > _speed_of(before_tier, solo)
            if not upgraded and _SPEED_HINT.search(fee):
                k.notes.append("写了提速 / 高迁，没写升到多少兆，千五待核对")
        lift = after_total - before_total
        if upgraded and after_speed >= GIGA_SPEED and lift >= GIGA_LIFT:
            k.giga = 1.0
        return k

    k.notes.append(f"业务类型「{kind}」认不出，五项都记 0")
    return k


def progress(done: dict, target: dict) -> dict:
    """各项进度（0.6 = 60%）；目标为 0 的记 0。千五、套包、单号卡最多 130%。"""
    out = {}
    for key in WEIGHTS:
        t = target.get(key) or 0
        p = (done.get(key) or 0) / t if t else 0.0
        out[key] = min(p, CAP) if key in CAPPED else p
    return out


def composite(prog: dict) -> float:
    """综合完成进度 = Σ 权重 × 进度。【待领导核对算法】"""
    return sum(WEIGHTS[k] * prog.get(k, 0.0) for k in WEIGHTS)
