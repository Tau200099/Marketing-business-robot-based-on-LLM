#!/usr/bin/env python3
"""
离线测试，不需要飞书凭证、不联网、不调模型、不花钱。

    python test_rules.py                        # 跑内置样例
    python test_rules.py "新增199"               # 测一句话会走哪条路
    python test_rules.py --file messages.txt    # 批量：一行一条真实消息

批量模式是调粗筛词的正确姿势：把群里最近几十条消息复制进一个 txt，
跑一遍看有没有报单被丢弃（漏筛），以及多少无关消息被放进模型（浪费）。

对每句话给出三种结果之一：
    [规则]  命中确定性规则 -> 本地秒回，零成本
    [模型]  规则没命中但粗筛命中 -> 会调一次模型（有 API 费用和 ~1 秒延迟）
    [丢弃]  两层都没命中 -> 直接忽略，零成本

调模型的那次判断本身是模型说了算，本脚本测不了 —— 这里能告诉你的是
「哪些消息会走到模型面前」，也就是你的成本敞口。
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

from botcore.config import CONFIG_PATH, _build
from botcore.console import force_utf8
from botcore.llm import LLMJudge

SAMPLES = [
    # --- 报单，应该走到模型 ---
    "新增79",
    "新增 199",
    "今天新增299一个",
    "79",
    "79元套餐",
    "今天79的3个",
    "办了个199",
    "智屏一台",
    "宽带装好了",
    "云盒2个",
    # --- 含关键词但不是报单：粗筛会放进来，靠模型判断挡掉 ---
    "宽带坏了报下障",
    "宽带什么时候能装",
    "完成率79%",
    "还有79个没处理完",
    "199的要退",
    # --- 粗筛就挡掉，零成本 ---
    "179",
    "1799",
    "2079年的目标",
    "今天天气不错",
    "下午开会",
    "中午吃什么",
    # --- 确定性规则 ---
    "机器人自检",
]


def load_inputs() -> list[str]:
    """命令行参数 > --file 文件 > 内置样例。"""
    args = sys.argv[1:]
    if args and args[0] == "--file":
        if len(args) < 2:
            sys.exit("用法：python test_rules.py --file messages.txt")
        path = Path(args[1])
        if not path.exists():
            sys.exit(f"找不到文件：{path}")
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
        return [ln for ln in lines if ln]
    return args or SAMPLES


def main() -> None:
    force_utf8()
    with CONFIG_PATH.open(encoding="utf-8") as f:
        b = _build(yaml.safe_load(f))
        matcher, llm = b.matcher, b.llm

    rule_names = "、".join(r.name for r in matcher.rules) or "（无）"
    print(f"\n规则层：{len(matcher.rules)} 条 —— {rule_names}")
    if llm.enabled:
        judge = LLMJudge(llm, api_key="")     # 只用它的粗筛，不发请求
        print(f"模型层：已启用（{llm.model}），粗筛词 {len(llm.prefilter)} 个\n")
    else:
        judge = None
        print("模型层：未启用\n")

    to_model = 0
    inputs = load_inputs()
    for i, raw in enumerate(inputs):
        text = raw
        # 每条样例用不同 chat_id，免得冷却时间互相干扰
        fired = matcher.match(text, f"chat_{i}")

        if fired:
            rule = fired[0]
            print(f"  [规则] {raw}\n         -> ({rule.name}) {rule.reply.splitlines()[0]}")
        elif judge is not None and judge.prefilter(text):
            to_model += 1
            print(f"  [模型] {raw}")
        else:
            print(f"  [丢弃] {raw}")

    total = len(inputs)
    if judge is not None:
        pct = to_model * 100 // total if total else 0
        print(f"\n{total} 条里有 {to_model} 条会调模型（{pct}%），其余零成本。\n")
    else:
        print()


if __name__ == "__main__":
    main()
