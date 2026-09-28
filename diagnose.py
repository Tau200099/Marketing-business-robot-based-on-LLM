#!/usr/bin/env python3
"""
只读诊断。不发消息、不改配置。

    python diagnose.py                    列出机器人所在的所有群
    python diagnose.py --image 截图.png    拿一张本地截图跑完整链路

--image 模式是排查「图片明明是业务内容，机器人却跳过」的正确工具。
它把 OCR -> 粗筛 -> 模型判断 三步的中间结果全部打出来，
一眼就能看出是哪一步出的问题：

    OCR 没识别出关键字段     -> 截图质量或 min_score 的问题
    粗筛没命中               -> prefilter 缺词
    模型判定「不是报单」      -> agent.context 需要调整（会打印模型原始返回）

用法：在飞书里把那张图另存到本地，然后指向它。
"""

from __future__ import annotations

import sys

import lark_oapi as lark
from lark_oapi.api.im.v1 import ListChatRequest

from botcore.config import load_env
from botcore.console import force_utf8


def diagnose_image(path: str) -> int:
    """拿一张本地图片跑完整链路，把每一步的中间结果都打出来。"""
    import os
    import time

    import yaml

    from botcore.config import CONFIG_PATH, _build
    from botcore.llm import LLMJudge
    import botcore.image as im

    if not os.path.exists(path):
        print(f"[!!] 找不到文件：{path}")
        return 1

    with CONFIG_PATH.open(encoding="utf-8") as f:
        b = _build(yaml.safe_load(f))
        matcher, llm, ocr = b.matcher, b.llm, b.ocr

    blob = open(path, "rb").read()
    print("=" * 60)
    print(f"图片诊断：{os.path.basename(path)}  ({len(blob)//1024} KB)")
    print("=" * 60)

    # ---- 1. OCR ----
    print("\n[1/3] 本地 OCR")
    if not ocr.enabled:
        print("  [!!] config.yaml 里 ocr.enabled 是 false，图片消息会被直接丢弃")
        return 1

    t = time.perf_counter()
    im.preload(ocr)
    print(f"   ·    引擎加载 {time.perf_counter()-t:.2f}s")

    t = time.perf_counter()
    text = im._ENGINE.read(blob, ocr)
    dt = time.perf_counter() - t

    if not text:
        print(f"  [!!] 没识别出任何文字（耗时 {dt:.2f}s）")
        print("       截图可能太模糊，或者 min_score 设得太高（当前 "
              f"{ocr.min_score}，可以调到 0.3 试试）")
        return 1

    truncated = len(text) > ocr.max_chars > 0
    print(f"  [OK]  识别 {len(text)} 字，耗时 {dt:.2f}s")
    if truncated:
        print(f"  [!!]  超过 max_chars={ocr.max_chars}，会被截断！"
              f"后面 {len(text)-ocr.max_chars} 个字模型看不到")
    print("\n---- 模型实际看到的文本 ----")
    shown = text[:ocr.max_chars] if truncated else text
    for i in range(0, len(shown), 70):
        print("  " + shown[i:i+70])
    print("  ----------------------------")

    # ---- 2. 粗筛 ----
    print("\n[2/3] 粗筛")
    judge = LLMJudge(llm, api_key="")
    if not llm.enabled:
        print("   ·    agent 未启用，跳过")
        return 0
    if judge.prefilter(shown):
        hits = [w for w in llm.prefilter if judge._prefilter
                and Rule_hit(w, shown)]
        print(f"  [OK]  命中，会交给模型。命中词：{hits[:8]}")
    else:
        print("  [!!]  未命中 —— 这张图会被直接丢弃，模型根本收不到")
        print(f"       当前 prefilter {len(llm.prefilter)} 个词，"
              "从上面 OCR 文本里挑几个稳定出现的加进去")
        return 1

    # ---- 3. 模型 ----
    print("\n[3/3] 模型判断")
    api_key = os.getenv(llm.api_key_env, "").strip()
    if not api_key:
        print(f"   ·    {llm.api_key_env} 为空，跳过实际调用")
        return 0

    judge = LLMJudge(llm, api_key)
    t = time.perf_counter()
    result = judge.judge(shown)
    dt = time.perf_counter() - t

    if result is None:
        print(f"  [!!] 模型调用失败（耗时 {dt:.2f}s），看上面的 WARNING")
        return 1

    print(f"  ·    耗时 {dt:.2f}s")
    print(f"  ·    is_report = {result['is_report']}")
    print(f"  ·    is_new    = {result['is_new']}")
    print(f"  ·    package   = {result['package']!r}")
    print(f"  ·    count     = {result['count']}")
    print(f"  ·    reply     = {result['reply']!r}")

    if result["is_report"]:
        print("\n[OK] 判定为报单，群里会回复上面这句")
    else:
        print("\n[!!] 判定为「不是报单」—— 这就是跳过的原因")
        print("       对照上面的 OCR 文本看模型被什么带偏了。常见元凶：")
        print("       · 温馨提示 / 免责条款里的「拆机」「违约金」「改约」「退订」")
        print("       · 页面按钮文字「返回首页」「取消」")
        print("       改 config.yaml 的 agent.context，明确让模型忽略这些")
    return 0


def Rule_hit(word: str, text: str) -> bool:
    from botcore.matcher import Rule
    return Rule(name="_", mode="any", keywords=[word], reply="").compile().match(text)


def main() -> int:
    force_utf8()

    args = sys.argv[1:]
    if args and args[0] == "--image":
        if len(args) < 2:
            print("用法：python diagnose.py --image 截图.png")
            return 1
        return diagnose_image(args[1])

    print("=" * 60)
    print("机器人所在群列表")
    print("=" * 60)

    import os
    load_env()
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        print("[!!] .env 里缺 FEISHU_APP_ID / FEISHU_APP_SECRET")
        return 1
    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

    req = ListChatRequest.builder().page_size(100).build()
    resp = client.im.v1.chat.list(req)

    if not resp.success():
        print(f"\n[!!] 查询失败 code={resp.code} msg={resp.msg} log_id={resp.get_log_id()}")
        if resp.code == 99991672:
            print("""
     缺少 im:chat:readonly 权限。这个权限只是本脚本用来查群列表的，
     机器人正常收发消息并不需要它。

     两个选择：
       ① 去开放平台「权限管理」申请 im:chat:readonly，重新发布版本后再跑本脚本
       ② 跳过本脚本，直接在飞书群里手动确认：
          群设置 -> 群机器人 -> 看列表里有没有你的应用
""")
        return 1

    items = getattr(resp.data, "items", None) or []

    if not items:
        print("""
[!!] 机器人当前不在任何群里。

     这就是群里没反应的原因 —— 应用发布 != 机器人进群，是两个独立步骤。

     处理：打开目标群 -> 群设置 -> 群机器人 -> 添加机器人 -> 选中你的应用
""")
        return 1

    print(f"\n共 {len(items)} 个群：\n")
    for i, chat in enumerate(items, 1):
        name = getattr(chat, "name", None) or "(无群名)"
        chat_id = getattr(chat, "chat_id", "?")
        print(f"  {i}. {name}")
        print(f"     chat_id = {chat_id}")

    print("""
确认你测试用的那个群在上面的列表里。

  · 在 -> 机器人确实在群里，问题出在事件订阅配置
  · 不在 -> 去那个群里把机器人加进去（群设置 -> 群机器人 -> 添加机器人）
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
