#!/usr/bin/env python3
"""
离线测晒单解析：一段文字会走「严格解析」还是「模型容错解析」，抽出来什么。

    python test_parse.py                      # 跑内置样例
    python test_parse.py "支局 新盛 新增 139 揽收 郭静"
    python test_parse.py --file cases.json    # 批量：[{"text": "...", "expect": {...}}, ...]
    python test_parse.py --no-llm             # 只跑严格解析，不调模型、不花钱

批量模式会把每条的抽取结果和 expect 逐字段比对，最后打印不一致的。
expect 里能写的键：kind fee branch collector points cards box fttr renew broadband high card
（写了的才比；没写的不比）。expect 为 null 表示「不该入清单」。

不改台账（campaign.json），只解析不落盘。
"""

from __future__ import annotations

import io
import json
import os
import sys
from dataclasses import asdict

from botcore.campaign import build_order, looks_like_order, parse_order
from botcore.config import ConfigStore, load_env
from botcore.console import force_utf8
from botcore.extract import FieldExtractor

SAMPLES = [
    # 标准模板
    "🔥 9月4日第14单🔥\n[庆祝]支局：新盛支局\n🚩业务类型:存量\n✌办理套餐：149改169\n☎️业务号码:02348469166\n"
    "👊价值积分:20\n👏是否加载AI：\n👏是否加载云盒：\n👏是否加载智屏：\n👏是否加载FTTR:是\n👏号卡数量:\n 🚀揽收人:杨桐",
    # 字段名写歪
    "支局 新盛支局\n类型 新增\n套餐 139+10\n号码 13812345678\n积分 149\n云盒 是\n揽收 郭静",
    # 没冒号、挤一行
    "文龙支局 新增139 揽收人郭静 积分139 加载云盒",
    # 山城卡
    "支局：文龙支局 业务类型:新增 办理套餐：19山城卡 号卡数量:1 揽收人:郭静",
    # 不是晒单：催晒单 / 问问题
    "各位请按模板晒单：支局、业务类型、办理套餐、揽收人都要填",
    "今天新增几单了？谁还没晒单",
    # 口语报单（不该进容错，字段名不够）
    "搞定一单199",
]

KEYS = ("kind", "fee", "branch", "collector", "points", "cards", "box", "fttr",
        "renew", "broadband", "high", "card")


def parse_one(text: str, extractor: FieldExtractor | None, min_hits: int):
    """返回 (走了哪条路, Order 或 None)。"""
    o = parse_order(text, "t", "2026-09-14 10:00:00")
    if o is not None:
        return "严格", o
    hits = looks_like_order(text)
    if extractor is None or hits < min_hits:
        return f"不像晒单（命中 {hits} 个字段名）", None
    fields = extractor.extract(text)
    if not fields:
        return "模型放弃", None
    o = build_order(fields, text, "t", "2026-09-14 10:00:00", source="文字晒单·模型解析")
    return "模型", o


def describe(o) -> str:
    if o is None:
        return "不入清单"
    return (f"{o.kind} {o.fee} | 支局={o.team or '?'} 揽收人={o.collector or '?'} "
            f"| 宽带{o.broadband} 中高{o.high} 续{o.renew} 卡{o.card} 云盒{o.box} FTTR{o.fttr} "
            f"积分{o.points}" + ("  ⚠待核对" if o.needs_review else ""))


def main() -> int:
    force_utf8()
    load_env()
    store = ConfigStore()
    cfg = store.extract_settings()
    use_llm = "--no-llm" not in sys.argv
    extractor = None
    if use_llm:
        key = os.getenv(cfg.api_key_env or store.llm_settings().api_key_env, "")
        extractor = FieldExtractor(cfg, store.llm_settings(), key) if key else None

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    cases: list[dict] = []
    if "--file" in sys.argv:
        path = args[0]
        cases = json.load(io.open(path, encoding="utf-8"))
    elif args:
        cases = [{"text": a} for a in args]
    else:
        cases = [{"text": t} for t in SAMPLES]

    failures = []
    for i, c in enumerate(cases, 1):
        text = c["text"]
        route, o = parse_one(text, extractor, cfg.min_field_hits)
        print(f"\n[{i}] {text[:70].replace(chr(10), ' ⏎ ')}")
        print(f"     -> [{route}] {describe(o)}")
        if "expect" in c:
            exp = c["expect"]
            if exp is None:
                if o is not None:
                    failures.append((i, "不该入清单", describe(o)))
                continue
            if o is None:
                failures.append((i, "该入清单但没入", route))
                continue
            got = asdict(o)
            got["branch"] = got.pop("team")
            for k, v in exp.items():
                if k not in KEYS:
                    continue
                if str(got.get(k)) != str(v):
                    failures.append((i, f"{k} 期望 {v!r}", f"实际 {got.get(k)!r}"))

    if any("expect" in c for c in cases):
        print("\n" + "=" * 60)
        if failures:
            print(f"不一致 {len(failures)} 处：")
            for i, what, actual in failures:
                print(f"  [{i}] {what}  ->  {actual}")
        else:
            print("全部一致")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
