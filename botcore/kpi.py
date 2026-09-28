"""
国庆专项营销活动（10.1–10.7）五项指标：一单算多少（09-29 国庆版，规则见 docs/国庆多群版功能说明.md 第六节）。

    新增宽带 25%   中高套 30%   千兆/500兆 20%   套包 15%   单号卡 10%

只管「一单 -> 五个数」，算给哪个支局、目标、进度、排名在总表那边。
晒单能读出来的就算；读不出来的（纯加装没写原档、高迁没写原档、FTTR 没写付没付费……）记 0 并在 notes 里说明，
由总表标黄让人看。

还在等领导定的（改这里的常量 / 函数就行）：
  · FTTR / 云盒 / 智屏 哪些是权益金兑换（不收月费、不计套包、不计千五⑤）—— 现在按 _paid_items 的默认
  · 单号卡「剔除直管」—— 现在晒单写了「直管」就不算
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .campaign import (HIGH_TIER_MIN, SOLO_GIGA_TIERS, _IS_CARD, _IS_EQUITY, _IS_SOLO, _ONLY_SUBCARD,
                       _TECH_TAG, _UPGRADE, _part_total, normalize_kind)

WEIGHTS = {"broadband": 0.25, "high": 0.30, "giga": 0.20, "pack": 0.15, "card": 0.10}
CAPPED = {"giga", "pack", "card"}     # 这三项进度最多算 130%，宽带、中高套不封顶
CAP = 1.30

TIER_2000M = 239          # 239 / 299 / 399 是 2000M
HIGH_13 = 239             # 中高套：239 及以上按 1.3 计
SUB_BB_GIGA_PRICE = 60    # 副宽 60 元 = 500M，算千五；38 元 = 300M 不算

# 新增宽带折算：写了这些字才折
_FOLD_THIRD = re.compile(r"明厨亮灶|780\s*包年")
_FOLD_FIFTH = re.compile(r"酒店|短期|临时宽带|列收|390\s*包年")
_NO_COUNT = re.compile(r"明星机")
_DIRECT = re.compile(r"直管")
_SUB_BB = re.compile(r"副宽\s*(\d+)?")
_LOOKHOME = re.compile(r"看家|守护|摄像头|9\.9")
_EQUITY_WORD = re.compile(r"权益|兑换|金币|必选\s*R|R\s*[（(]\s*1\s*\+\s*1")
_PRICE_AFTER = {
    "FTTR": re.compile(r"(?:FTTR|光纤到房间)\s*[+＋]?\s*(\d+(?:\.\d+)?)|[+＋]\s*(\d+(?:\.\d+)?)\s*(?:元\s*)?(?:FTTR|光纤到房间)", re.I),
    "云盒": re.compile(r"云盒\s*[+＋]?\s*(\d+(?:\.\d+)?)|[+＋]\s*(\d+(?:\.\d+)?)\s*(?:元\s*)?(?:轻)?云盒"),
    "智屏": re.compile(r"智屏\s*[+＋]?\s*(\d+(?:\.\d+)?)|[+＋]\s*(\d+(?:\.\d+)?)\s*(?:元\s*)?(?:天翼)?智屏"),
    "AI": re.compile(r"AI\s*(?:视联网)?\s*[+＋]?\s*(\d+(?:\.\d+)?)|[+＋]\s*(\d+(?:\.\d+)?)\s*(?:元\s*)?AI", re.I),
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


def _num(s) -> float:
    return float(s) if s not in (None, "") else 0.0


def _items(fee: str, loaded: str) -> list[str]:
    """这一单加载了哪些套包：晒单「是否加载」勾的 + 套餐里写的字。"""
    names = [x for x in (loaded or "").split(",") if x]
    for n, pat in (("FTTR", r"FTTR|光纤到房间"), ("云盒", r"云盒"), ("智屏", r"智屏"), ("AI", r"AI\s*视联网|视联网")):
        if n not in names and re.search(pat, fee, re.I):
            names.append(n)
    if "看家" not in names and _LOOKHOME.search(fee):
        names.append("看家")
    return names


def _price(name: str, fee: str) -> float | None:
    """套餐里写了这一样的价钱就返回价钱（「FTTR25」「+25FTTR」「智屏10」），没写返回 None。"""
    pat = _PRICE_AFTER.get(name)
    if not pat:
        return None
    m = pat.search(fee)
    if not m:
        return None
    v = m.group(1) or m.group(2)
    return float(v) if v else None


def _paid_items(fee: str, loaded: str, tier: int, notes: list) -> list[tuple[str, float]]:
    """
    付了月费的套包 [(名字, 月费)]。权益金兑换的不算（通知：权益金兑换的 FTTR 和云盒不计量）。
    【默认，等领导核对后改这里】
      · 套餐里写了「权益 / 兑换 / 金币 / 必选R / R(1+1)」→ FTTR、云盒都当权益兑换
      · FTTR：写了价钱（「FTTR25」「+25FTTR」）才算付费；只勾了「是否加载FTTR」没写价钱 → 当权益兑换，标黄
      · 云盒：239 及以上是 0 元云盒（249 权益兑换）→ 不算；写了价钱按价钱；其余按 10 元
      · 智屏、AI 视联网：按写的价钱，没写按 10 元；看家 / AI 守护 / 摄像头 9.9
    """
    equity = bool(_EQUITY_WORD.search(fee))
    out = []
    for name in _items(fee, loaded):
        price = _price(name, fee)
        if name == "FTTR":
            if equity or price is None:
                if not equity:
                    notes.append("FTTR 没写价钱，按权益兑换不计套包，请核对")
                continue
        elif name == "云盒":
            if equity or (tier >= TIER_2000M and price is None):
                continue
        out.append((name, price if price is not None else _DEFAULT_PRICE[name]))
    return out


def pack_score(paid: list[tuple[str, float]]) -> float:
    """套包分：一单月费合计 V，1 + (V − 9.9) ÷ 10（V ≤ 9.9 就是 1）；每台智屏再加 月费 × 0.3。"""
    v = sum(p for _n, p in paid)
    if v <= 0:
        return 0.0
    score = 1 + max(0.0, v - 9.9) / 10
    score += sum(p * 0.3 for n, p in paid if n == "智屏")
    return score


def compute(kind: str, fee: str, loaded: str = "", cards: int = 1) -> KPI:
    """一单 -> 五项。kind / fee 是晒单里的「业务类型」「办理套餐」原文，loaded 是 Order.loaded。"""
    k = KPI()
    raw_fee = (fee or "").strip()
    fee = _TECH_TAG.sub("", raw_fee)
    kind = normalize_kind(kind)
    up = _UPGRADE.search(fee)
    nums = [int(x) for x in re.findall(r"\d+", fee)]
    base = nums[0] if nums else 0
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

    # 权益金兑换单：什么都不算
    if _IS_EQUITY.search(fee) and not up and not re.search(r"^\s*\d{2,3}\s*[+＋]", fee):
        k.notes.append("权益金兑换不计量")
        return k

    paid = _paid_items(fee, loaded, after_base, k.notes)
    k.pack = pack_score(paid)
    lookhome_paid = any(n == "看家" for n, _p in paid)
    fttr_paid = any(n == "FTTR" for n, _p in paid)

    solo_only = bool(_IS_SOLO.search(fee)) and not up and "+" not in fee
    if kind == "新增" or (solo_only and kind != "存量"):
        # ---- 新增宽带 ----
        fold = 1.0
        if _FOLD_THIRD.search(fee):
            fold = 1 / 3
        elif _FOLD_FIFTH.search(fee):
            fold = 1 / 5
        subs = _SUB_BB.findall(fee)
        if solo_only:
            k.broadband = fold
            if re.search(r"副宽", fee):
                k.giga = 1.0 if base >= SUB_BB_GIGA_PRICE else 0.0
            else:
                k.giga = 1.0 if base in SOLO_GIGA_TIERS else 0.0
            return k
        k.broadband = fold * (1 + len(subs))
        # ---- 中高套（只算新增融合）----
        if base >= HIGH_13:
            k.high = 1.3
        elif base >= HIGH_TIER_MIN:
            k.high = 1.0
        # ---- 千五：每条宽带各算一次 ----
        main_giga = 1.0 if base >= HIGH_TIER_MIN else 0.0
        if not main_giga and (lookhome_paid or fttr_paid):
            main_giga = 1.0            # ⑤ 付费看家 / FTTR 小合约
        sub_giga = sum(1.0 for price in subs if price and int(price) >= SUB_BB_GIGA_PRICE)
        k.giga = main_giga + sub_giga
        return k

    if kind == "存量":
        if up:
            before = _part_total(fee[:up.start(2)])
            after = _part_total(fee[up.start(2):])
            if after_base >= TIER_2000M:
                # 2000M：是不是 2000M 只看宽带价格；高迁 / 平签 / 低签按总和比
                k.giga = 1.5 if after > before else (1.2 if after == before else 1.1)
            elif after_base >= HIGH_TIER_MIN and after - before >= 10:
                k.giga = 1.0
            elif lookhome_paid or fttr_paid:
                k.giga = 1.0
            return k
        if base >= TIER_2000M and not re.search(r"[+＋]", fee):
            # 「299续约」「299」：2000M 平签
            if re.search(r"高迁|提值", fee):
                k.giga = 1.5
            elif re.search(r"降档|负迁|低签", fee):
                k.giga = 1.1
            else:
                k.giga = 1.2
            return k
        addon_total = sum(p for _n, p in paid)
        plus = [float(x) for x in re.findall(r"[+＋]\s*(\d+(?:\.\d+)?)", fee)]
        added = max(addon_total, sum(plus))
        if base and added >= 10 and base + added >= HIGH_TIER_MIN:
            k.giga = 1.0              # ④ 加包：附加 ≥10 且加完 ≥139
        elif lookhome_paid or fttr_paid:
            k.giga = 1.0              # ⑤ 付费小合约
        elif not base:
            k.notes.append("纯加装没写原档位，千五待核对")
        elif re.search(r"高迁|提值", fee) and base >= HIGH_TIER_MIN:
            k.notes.append("高迁没写原档位，千五待核对")
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
