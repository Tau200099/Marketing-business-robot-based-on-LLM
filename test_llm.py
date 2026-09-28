#!/usr/bin/env python3
"""
模型接入调试。按 config.yaml 当前的 agent 配置真调一次，把细节全打出来。

    python test_llm.py                 用默认样例测
    python test_llm.py "今天新增199一个"   指定样例
    python test_llm.py --curl          只打印等价的 curl，不发请求

和 preflight 第 6 步的区别：那边只报成功/失败（错误截断到 200 字），
这里把发出去的 URL、请求头、字段名，和完整的返回体都打出来 ——
接新服务商时错在哪一眼能看见。

凭证会打码，输出可以安全贴出来。
"""

from __future__ import annotations

import json
import os
import sys

import requests

from botcore.config import ConfigStore, load_env
from botcore.console import force_utf8
from botcore.llm import LLMJudge

DEFAULT_SAMPLE = "今天新增199一个"


def mask(value: str) -> str:
    """凭证打码：留头尾各几位，中间抹掉。"""
    if len(value) <= 12:
        return value[:3] + "…" if value else "(空)"
    return f"{value[:8]}…{value[-4:]}（共 {len(value)} 字符）"


def mask_headers(headers: dict) -> dict:
    out = {}
    for k, v in headers.items():
        if k.lower() in ("authorization", "x-app-id", "order-num", "api-key"):
            prefix = "Bearer " if v.startswith("Bearer ") else ""
            out[k] = prefix + mask(v[len(prefix):])
        else:
            out[k] = v
    return out


def diagnose(status: int, body: str) -> list[str]:
    """按状态码给针对性建议 —— 接新服务商时这几种错最常见。"""
    low = body.lower()
    tips: list[str] = []
    if status == 401 or status == 403:
        tips += [
            "鉴权没过。依次检查：",
            "  · .env 里的 key 是不是填对了、有没有多余空格或换行",
            "  · 这家是不是不用 Authorization: Bearer，而要自己的请求头",
            "    （在 config.yaml 的 agent.extra_headers 里加，值可写 ${环境变量名}）",
        ]
    elif status == 404:
        tips += [
            "地址不对。base_url 填的应该是 /chat/completions 【之前】那一段。",
            "  文档给的若是 https://x/v1/chat/completions，这里只填 https://x/v1",
        ]
    elif status == 400:
        tips += ["请求体被拒。看上面的返回内容说哪个字段有问题："]
        if "max_tokens" in low or "max_completion" in low:
            tips.append("  · token 上限字段名不对，试试把 agent.max_tokens_field 改成 max_completion_tokens")
        if "model" in low:
            tips.append("  · model 名不对，以服务商控制台里显示的调用名为准")
        tips.append("  · 也可能是这家不支持某个参数（如 temperature 范围不同）")
    elif status == 429:
        tips.append("被限流了。降低 agent.max_calls_per_minute，或稍后再试。")
    elif status >= 500:
        tips.append("服务端错误，不是你的配置问题。稍后重试；持续报错就联系服务商。")
    return tips


def main() -> int:
    force_utf8()
    load_env()

    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    curl_only = "--curl" in sys.argv[1:]
    sample = args[0] if args else DEFAULT_SAMPLE

    store = ConfigStore()
    s = store.llm_settings()

    if not s.enabled:
        print("[!!] config.yaml 里 agent.enabled 是 false，先打开再测")
        return 1

    api_key = os.getenv(s.api_key_env, "").strip()
    if not api_key:
        print(f"[!!] 环境变量 {s.api_key_env} 是空的，去 .env 里填")
        return 1

    judge = LLMJudge(s, api_key)
    url = f"{s.base_url.rstrip('/')}/chat/completions"
    headers = judge._headers()

    print("=" * 64)
    print("配置")
    print("=" * 64)
    print(f"  URL          {url}")
    print(f"  model        {s.model}")
    print(f"  key 来源     {s.api_key_env} = {mask(api_key)}")
    print(f"  token 字段   {s.max_tokens_field} = {s.max_tokens}")
    print(f"  超时         {s.timeout}s")
    print("  请求头：")
    for k, v in mask_headers(headers).items():
        print(f"    {k}: {v}")

    if curl_only:
        print("\n" + "=" * 64)
        print("等价 curl（凭证已打码，自己替换后可在 Python 外验证）")
        print("=" * 64)
        parts = [f"curl -X POST '{url}'"]
        for k, v in mask_headers(headers).items():
            parts.append(f"  -H '{k}: {v}'")
        parts.append("  -d '{\"model\":\"%s\",\"messages\":[{\"role\":\"user\",\"content\":\"你好\"}]}'"
                     % s.model)
        print(" \\\n".join(parts))
        return 0

    print("\n" + "=" * 64)
    print(f"实测调用：{sample}")
    print("=" * 64)

    # 直接发一次原始请求，好把完整响应打出来（judge() 只返回解析结果）
    payload = {
        "model": s.model,
        "messages": [{"role": "user", "content": sample}],
        "temperature": s.temperature,
        s.max_tokens_field: s.max_tokens,
        "stream": False,
    }
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=s.timeout)
    except requests.RequestException as exc:
        print(f"\n[!!] 请求没发出去：{exc}")
        print("     检查网络，以及这个域名是否需要内网/专线才能访问")
        return 1

    print(f"\nHTTP {resp.status_code}")
    body = resp.text
    print("返回内容：")
    print("  " + (body[:1500].replace("\n", "\n  ") or "(空)"))

    if resp.status_code != 200:
        print()
        for tip in diagnose(resp.status_code, body):
            print(f"  {tip}")
        return 1

    # 到这里连通性没问题了，再看它能不能按我们要的格式输出
    print("\n" + "=" * 64)
    print("按机器人实际的提示词跑一遍（看能不能吐出合法 JSON）")
    print("=" * 64)
    result = judge.judge(sample)
    if result is None:
        print("\n[!!] 接口通了，但返回内容解析不了。")
        print("     多半是这个模型不老实输出 JSON，爱加解释性文字。")
        print("     对策：在 config.yaml 的 agent.context 末尾加一句")
        print("           「只输出 JSON，不要任何额外说明文字」")
        return 1

    print(f"\n  是否报单   {result['is_report']}")
    print(f"  是否新增   {result['is_new']}")
    print(f"  套餐       {result['package'] or '(未识别)'}")
    print(f"  单量       {result['count']}")
    print(f"  新增宽带   {result.get('broadband_count', 0)}   新增中高套 {result.get('high_count', 0)}")
    print(f"  生成回复   {result['reply']}")
    print("\n[OK] 全通。可以启动了 -> python bot.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
