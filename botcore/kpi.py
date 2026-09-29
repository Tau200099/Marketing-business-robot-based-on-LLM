"""
国庆专项营销活动（10.1–10.7）五项指标：一单算多少（09-29 国庆版，规则见 docs/国庆多群版功能说明.md 第六节）。

    新增宽带 25%   中高套 30%   千兆/500兆 20%   套包 15%   单号卡 10%

只管「一单 -> 五个数」，算给哪个支局、目标、进度、排名在总表那边。
晒单【只认字，不靠符号】（员工不一定打「+」「M」「→」）：「129改139加10」「续约139升级五百兆加云盒」「升千兆」都认。
读不准的记 0 并在 notes 里说明，由总表标黄让人看。

晒单模板里有两栏（09-29 新模板）：「是否升级千兆/500M」「是否升级2000M」—— 只认「是」，空着 / 否都是否，以这两栏为准；
模板里根本没这两栏（旧模板）才看套餐里的字和档位。

千五（09-29 用户重定）：
  · 新增：看速率 —— 139 及以上融合（含 2000M）、138/198 单宽、1390/1990 包年、副宽 60 各记 1；
          79/99 融合、98 单宽、390/780 包年、副宽 38 不算（加了看家、FTTR 也不算）
  · 存量 2000M：「是否升级2000M：是」才算，再按前后总和比，高迁 1.5 / 平签 1.2 / 低签 1.1
  · 存量其他：【速率升档】且【提值 ≥10】才记 1。速率升档看「是否升级千兆/500M」；
          旧模板没这栏才看套餐里写的「升500兆 / 升千兆」，再没写按档位查表（79=200M、99/129=300M、139/169=500M、199=千兆）；
          提值 = 办完的总和 − 原来的总和（只算写出来的钱，1 元提速也算）
套包：只算「办理套餐」里写出来的加装钱（「239+25元FTTR」「239+10」），没写价钱不算；
      套包分 = 加装钱合计 ÷ 10（9.9 按 10 算），有智屏再加 0.3。1 元提速、副宽、副卡、返费不是套包，权益金兑换没有钱也不算

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
# 这些钱不是套包：「1元提速」「副宽60 / 60元副宽」「副卡」「返55」
_NOT_PACK = re.compile(r"(?<![\d.])\d+(?:\.\d+)?\s*元?\s*(?:的\s*)?(?:提速|副宽|副卡|副机)|(?:副宽|副卡|副机)\s*\d+(?:\.\d+)?|返\s*\d+(?:\.\d+)?")
_SCREEN = re.compile(r"智屏")
SCREEN_BONUS = 0.3
MIN_TIER = 39             # 第一个数 < 39 又紧挨着套包名的不是档位，是加装的钱（「10元云盒」「云盒10」；单写「38」是档位）
_ITEM_WORD = r"(?:云盒|智屏|FTTR|光纤到房间|AI|视联网|看家|守护|摄像头)"


@dataclass
class KPI:
    broadband: float = 0.0
    high: float = 0.0
    giga: float = 0.0
    pack: float = 0.0
    card: float = 0.0
    notes: list = field(default_factory=list)
    giga_lines: int = 0      # 千五达标的宽带有几条（不乘 2000M 系数）：活动群的「千兆/500M」按这个数（09-29 用户定）
    bb_lines: int = 0        # 新增宽带几条（不折算、不 ×1.1）：活动群按这个数
    box_items: int = 0       # 写了价钱的云盒 / AI / 智屏 几个（FTTR 不算）：活动群「云盒/AI/智屏」按这个数

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


def strip_speed(fee: str) -> str:
    """剥掉速率的字（「升500兆」「千兆」「500M」），免得 500 / 1000 被当成档位。晒单解析算积分前也用。"""
    return _SPEED_ANY.sub(" ", _SPEED_UP.sub(" ", fee or ""))


def _money_text(fee: str) -> str:
    """剥掉速率（「升500兆」「千兆」）和权益金数字，剩下的数字都是档位和钱。"""
    return _EQUITY_NUM.sub(_strip_equity_num, strip_speed(fee))


def _addon_money(text: str, tier: int) -> float:
    """一段套餐文字里加装套包的钱：去掉档位（第一个数）、提速 / 副宽 / 副卡 / 返费；9.9 按 10 算。"""
    nums = [float(x) for x in _NUMF.findall(_NOT_PACK.sub(" ", text))]
    if tier and nums and nums[0] == tier:
        nums = nums[1:]
    return sum(10.0 if abs(v - 9.9) < 1e-6 else v for v in nums)


def _tier_of(text: str) -> int:
    """第一个数当档位；「10元云盒」「云盒10」「9.9看家」这种紧挨着套包名的小数目是加装的钱，不是档位，返回 0。"""
    m = _NUMF.search(text)
    if not m:
        return 0
    v = float(m.group())
    if v < MIN_TIER and (re.match(rf"\s*元?\s*(?:的\s*)?{_ITEM_WORD}", text[m.end():], re.I)
                         or re.search(rf"{_ITEM_WORD}\s*(?:加|[+＋➕])?\s*$", text[:m.start()], re.I)):
        return 0
    return int(v)


def _flag(v) -> str:
    """模板「是否升级…」一栏：是 / 否；模板里没这一栏为空串。"""
    v = (v or "").strip() if isinstance(v, str) else ("是" if v else "")
    return "" if not v else ("是" if v == "是" else "否")


def _total(part: str) -> float:
    """一段文字里的钱加起来，减掉「返多少」：「139加10」→149，「169返60」→109，「169加9.9看家」→179（9.9 按 10）。"""
    rebate = sum(float(x) for x in _REBATE.findall(part))
    nums = (float(x) for x in _NUMF.findall(_REBATE.sub(" ", part)))
    return sum(10.0 if abs(v - 9.9) < 1e-6 else v for v in nums) - rebate      # 9.9 看家按 10 算


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
        pb = re.search(r"(?<![\d.])(\d{2})\s*元?\s*(?:的\s*)?$", before)      # 「60元副宽」「38副宽」
        sp = re.match(rf"\s*(?:\d{{2,3}}\s*元?\s*)?({_SPEED_UNIT})", after, re.I)
        price = int(pa.group(1)) if pa else (int(pb.group(1)) if pb else 0)
        out.append(price >= SUB_BB_GIGA_PRICE or bool(sp and _speed_value(sp.group(1)) >= GIGA_SPEED))
    return out


sub_broadbands = _sub_bbs          # 晒单解析（活动群战报 / 通报）也按这个数副宽的千五


_SUB_BB_TEXT = re.compile(r"(?:(?<![\d.])\d{2}\s*元?\s*(?:的\s*)?)?副宽\s*(?:\d{2,3}(?![\d.]))?")


# ---------------------------------------------------------------- 套包

_ITEM_NAME = r"(FTTR|光纤到房间|云盒|智屏|AI|视联网|看家|守护|摄像头)"
_FTTR_NAME = re.compile(r"FTTR|光纤到房间", re.I)


def _box_items(text: str, tier: int, loaded: str) -> int:
    """
    活动群「云盒/AI/智屏」的个数（09-29 用户定：和总表同一套判定，写了价钱才算；FTTR 不算进这一列）。
    每笔加装钱算一个套包：紧挨着套包名的按那个名字（「10元云盒」「FTTR25」）；没挨着名字的，
    勾了 FTTR 且 ≥20 元（FTTR 一般 25）当 FTTR（只认一笔），其余按云盒（没写是哪样默认云盒）。
    """
    t = _NOT_PACK.sub(" ", text)
    ms = list(_NUMF.finditer(t))
    if tier and ms and float(ms[0].group()) == tier:
        ms = ms[1:]
    fttr_flag, fttr_used, n = "FTTR" in (loaded or ""), False, 0
    for m in ms:
        a = re.match(rf"\s*元?\s*(?:的\s*)?{_ITEM_NAME}", t[m.end():], re.I)
        b = re.search(rf"{_ITEM_NAME}\s*(?:加|[+＋➕十])?\s*$", t[:m.start()], re.I)
        word = (a or b).group(1) if (a or b) else ""
        if word:
            is_fttr = bool(_FTTR_NAME.match(word))
        else:
            is_fttr = fttr_flag and not fttr_used and float(m.group()) >= 20
        if is_fttr:
            fttr_used = True
        else:
            n += 1
    return n


def pack_score(money: float, screen: bool = False) -> float:
    """套包分 = 加装钱合计 ÷ 10，有智屏再加 0.3；没写钱就是 0。例：10云盒 + 10智屏 = 2.3，AI 9.9 = 1，199改199+10+25 = 3.5。"""
    if money <= 0:
        return 0.0
    return money / 10 + (SCREEN_BONUS if screen else 0.0)


# ---------------------------------------------------------------- 一单 -> 五项

def compute(kind: str, fee: str, loaded: str = "", cards: int = 1, up_giga: str = "", up_2000: str = "") -> KPI:
    """
    一单 -> 五项。kind / fee 是晒单里的「业务类型」「办理套餐」原文，loaded 是 Order.loaded；
    up_giga / up_2000 是模板「是否升级千兆/500M」「是否升级2000M」：是 / 否，旧模板没这栏为空串。
    """
    k = KPI()
    fee = _TECH_TAG.sub("", (fee or "").strip())
    kind = normalize_kind(kind)
    up_giga, up_2000 = _flag(up_giga), _flag(up_2000)
    money = _money_text(fee)
    up = _tier_up(money)
    nums = _NUMF.findall(money)
    first = int(float(nums[0])) if nums else 0
    base = _tier_of(money)                        # 「10元云盒」「云盒10」：没写档位，10 是钱
    after_base = int(up.group(2)) if up else base

    if _NO_COUNT.search(fee):
        k.notes.append("明星机赠送宽带不计量")
        return k

    # 号卡：19 元及以上，副卡、直管不算
    if _IS_CARD.search(fee) or _ONLY_SUBCARD.search(fee) or kind == "单C":
        if _ONLY_SUBCARD.search(fee):
            return k
        # 「剔除直管」这次活动不用了（09-29 用户：直管这个概念从这次活动删除）
        price = int(up.group(2)) if up else first
        if price >= 19 or not nums:        # 没写月费（「山城卡」）判不了价钱，照算
            k.card = float(cards or 1)
        return k

    # 只有权益兑换、没有档位（「699权益兑换FTTR」）：什么都不算
    if _EQUITY_WORD.search(fee) and not base:
        k.notes.append("权益金兑换不计量")
        return k

    # ---- 套包：办完的那部分里写出来的加装钱 ----
    after_text = money[up.start(2):] if up else money
    screen = "智屏" in (loaded or "") or bool(_SCREEN.search(fee))
    k.pack = pack_score(_addon_money(after_text, after_base), screen)
    k.box_items = _box_items(after_text, after_base, loaded) if k.pack else 0

    subs = _sub_bbs(fee)
    solo = bool(_SOLO_WORD.search(fee)) or (bool(subs) and not _NUMF.search(_SUB_BB_TEXT.sub(" ", money)))
    if kind == "新增" and not base and not subs and not solo and re.search(_ITEM_WORD, fee, re.I):
        # 新增纯加装（「10元云盒」「云盒」）：不是新开宽带 —— 只算套包（09-29 用户定）
        return k
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
                k.bb_lines = len(subs)
                k.giga = float(sum(subs))
            else:                                  # 单宽 / 包年：138/198/1390/1990 是 500M 以上
                k.broadband = fold
                k.bb_lines = 1
                k.giga = 1.0 if base in SOLO_GIGA_TIERS else 0.0
            k.giga_lines = int(k.giga)
            return k
        k.broadband = fold * (1 + len(subs))
        k.bb_lines = 1 + len(subs)
        # ---- 中高套（只算新增融合）----
        # 新增写了「改」（「109改139」异网策反之类）：按改成的档位算
        if after_base >= HIGH_13:
            k.high = 1.3
        elif after_base >= HIGH_TIER_MIN:
            k.high = 1.0
        # ---- 千五：每条宽带各算一次；融合 139 及以上 500M 起，副宽 60 另算 ----
        k.giga = (1.0 if after_base >= HIGH_TIER_MIN else 0.0) + sum(subs)
        k.giga_lines = int(k.giga)
        return k

    if kind == "存量":
        # 存量带副宽（09-29 用户定）：副宽是新开的一条宽带 —— 算新增宽带，60 元（500M）另记千五；
        # 副宽的钱不算进主宽带的迁转 / 提值（下面只看去掉副宽后的「主宽带」那段）
        k.broadband = float(len(subs))
        k.bb_lines = len(subs)
        sub_giga = float(sum(subs))
        main = _SUB_BB_TEXT.sub(" ", money) if subs else money
        up = _tier_up(main)
        before_tier = _tier_of(main)
        after_tier = int(up.group(2)) if up else before_tier
        after_text = main[up.start(2):] if up else main
        # 迁转前后的宽带档位和总和（总和 = 写出来的档位 + 加装钱 − 返费）
        before_total = _total(main[:up.start(2)]) if up else float(before_tier)
        after_total = _total(after_text)
        sp = _SPEED_UP.search(fee)

        # ---- 2000M：模板「是否升级2000M：是」才算；旧模板没这栏看写的字 / 迁完档位 ----
        if up_2000:
            is_2000 = up_2000 == "是"
        else:
            is_2000 = (bool(sp) and _speed_value(sp.group("to")) >= 2000) or (after_tier >= TIER_2000M and not solo)
        if is_2000:
            if up:
                g = 1.5 if after_total > before_total else (1.2 if after_total == before_total else 1.1)
            elif after_total > before_total or re.search(r"高迁|提值", fee):
                g = 1.5                            # 「299续约加10元云盒」：总和变高 = 高迁
            elif re.search(r"降档|负迁|低签", fee):
                g = 1.1
            else:
                g = 1.2                            # 「299续约」：平签
            k.giga = g + sub_giga
            k.giga_lines = 1 + int(sub_giga)       # 2000M 高迁 / 平签 / 低签在活动群都记 1 条
            return k

        # ---- 其他：速率升档 且 提值 ≥10 ----
        if up_giga:
            upgraded, after_speed = up_giga == "是", GIGA_SPEED    # 模板这一栏为准
        elif sp:
            # 旧模板、套餐里写了「升500兆 / 升千兆」：原来的 139 可能不是 500M，以写的为准
            after_speed = _speed_value(sp.group("to"))
            upgraded = not sp.group("frm") or _speed_value(sp.group("frm")) < after_speed
        else:
            after_speed = _speed_of(after_tier, solo)
            upgraded = after_speed > _speed_of(before_tier, solo)
            if not upgraded and _SPEED_HINT.search(fee):
                k.notes.append("写了提速 / 高迁，没写升到多少兆，千五待核对")
        main_giga = 1.0 if upgraded and after_speed >= GIGA_SPEED and after_total - before_total >= GIGA_LIFT else 0.0
        k.giga = main_giga + sub_giga
        k.giga_lines = int(main_giga + sub_giga)
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
    """综合完成进度 = Σ 权重 × 进度（加权和，暂定，用户还要确认）。进度先按上面的封顶规则。"""
    return sum(WEIGHTS[k] * prog.get(k, 0.0) for k in WEIGHTS)
