#!/usr/bin/env python3
"""
企业微信 MCP 服务器探测。只读，不发任何消息。

    python wecom_discover.py            列出所有工具和入参 schema
    python wecom_discover.py --probe    再实际拉一次群列表和消息，看出参结构

为什么需要这个：
企微把能力包成了 MCP 服务器，工具 schema 可以在运行时问出来，
比翻文档准（文档页经常被截断，而且和实际实现可能有出入）。

⚠️ 隐私：--probe 会真的拉到群里的消息。本脚本【只打印字段名和类型】，
   不打印字段值 —— 所以输出可以安全地贴出来讨论，不会泄露聊天内容。
   唯一的例外是明确标了「样例」的地方，那些已经过脱敏。

配置：在 .env 里加一行（URL 里带 apikey，整条从企微后台复制）
    WECOM_MCP_URL=https://qyapi.weixin.qq.com/mcp/v2/bot/msg?apikey=xxx
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timedelta

from botcore.config import load_env
from botcore.console import force_utf8
from botcore.distill import redact

try:
    from mcp import Client
except ImportError:
    sys.exit("缺少依赖，请先执行：pip install mcp")


def describe(value, depth: int = 0, max_depth: int = 4) -> str:
    """
    只描述结构，不泄露内容：把值换成类型名，字典保留键名。
    列表只看第一个元素的结构。
    """
    pad = "  " * depth
    if depth > max_depth:
        return "…"
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = [f"{pad}  {k}: {describe(v, depth + 1, max_depth)}" for k, v in value.items()]
        return "{\n" + "\n".join(lines) + f"\n{pad}}}"
    if isinstance(value, list):
        if not value:
            return "[]"
        return f"[ {len(value)} 项，第一项结构：{describe(value[0], depth + 1, max_depth)} ]"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return f"int（如 {value}）" if abs(value) < 10 ** 12 else "int"
    if isinstance(value, float):
        return "float"
    if value is None:
        return "null"
    s = str(value)
    return f"str（{len(s)} 字符）"


def sample(text: str, limit: int = 40) -> str:
    """给一小段脱敏后的样例，帮助确认字段含义。"""
    return redact(str(text))[:limit]


async def run(url: str, probe: bool) -> None:
    # 高层 Client 直接吃 URL 字符串，走 Streamable HTTP
    async with Client(url) as session:
        print("=" * 66)
        print("工具列表")
        print("=" * 66)
        tools = (await session.list_tools()).tools
        for t in tools:
            print(f"\n▶ {t.name}")
            if t.description:
                print(f"  {t.description.strip()}")
            # SDK 版本不同，属性名可能是 inputSchema 或 input_schema，两个都试
            schema = (getattr(t, "inputSchema", None)
                      or getattr(t, "input_schema", None))
            if hasattr(schema, "model_dump"):
                schema = schema.model_dump()
            if schema:
                props = (schema or {}).get("properties") or {}
                required = set((schema or {}).get("required") or [])
                print("  入参：")
                for k, v in props.items():
                    mark = "必填" if k in required else "可选"
                    desc = (v.get("description") or "").strip().replace("\n", " ")
                    print(f"    · {k} ({v.get('type','?')}, {mark}) {desc[:70]}")
            if not schema:
                # 两个属性名都没有，把对象本身摊开看看字段叫什么
                dump = getattr(t, "model_dump", None)
                if dump:
                    keys = [k for k in dump().keys() if k not in ("name", "description")]
                    print(f"  （没读到入参 schema，该工具对象的其它字段：{keys}）")
            out = (getattr(t, "outputSchema", None)
                   or getattr(t, "output_schema", None))
            if out:
                print(f"  出参 schema：{json.dumps(out, ensure_ascii=False)[:300]}")

        if not probe:
            print("\n" + "=" * 66)
            print("加 --probe 可以实际拉一次数据，看真实的出参结构")
            print("=" * 66)
            return

        names = {t.name for t in tools}
        now = datetime.now()
        begin = now - timedelta(hours=6)
        fmt = "%Y-%m-%d %H:%M:%S"

        print("\n" + "=" * 66)
        print("实际调用（只打印结构，不打印内容）")
        print("=" * 66)

        chat_id = None

        # 先试无入参的这个：只返回机器人自己所在的会话，
        # 不依赖时间范围，也可能不需要 chat_groups_list 那套权限
        if "message_aibot_sessions_list" in names:
            print("\n▶ message_aibot_sessions_list（无入参）")
            res = await session.call_tool("message_aibot_sessions_list", {})
            data = _unwrap(res)
            print(describe(data))
            _show_identity(data)
            _check_err(data)
            chat_id = _find_first_chat_id(data)
            if chat_id:
                print(f"  ✓ 拿到会话 ID（不显示原值，长度 {len(chat_id)}）")
            elif not data.get("errcode"):
                print("  会话数为 0 —— 机器人还没进群，或进群后群里还没有过消息")

        if chat_id is None and "chat_groups_list" in names:
            print("\n▶ chat_groups_list（最近 6 小时）")
            res = await session.call_tool("chat_groups_list", {
                "begin_time": begin.strftime(fmt),
                "end_time": now.strftime(fmt),
            })
            data = _unwrap(res)
            print(describe(data))
            _check_err(data)
            chat_id = _find_first_chat_id(data)
            if chat_id:
                print(f"\n  取到一个群 ID 用于下一步（不显示原值，长度 {len(chat_id)}）")

        if "chat_messages_list" in names and chat_id:
            print("\n▶ chat_messages_list（该会话最近 6 小时）")
            # 参数名是 chat_id，而且没有 chat_type
            res = await session.call_tool("chat_messages_list", {
                "chat_id": chat_id,
                "begin_time": begin.strftime(fmt),
                "end_time": now.strftime(fmt),
            })
            data = _unwrap(res)
            print(describe(data))
            _check_err(data)
            _report_sender(data)
        elif "chat_messages_list" in names:
            print("\n▶ chat_messages_list 跳过：没拿到群 ID"
                  "（可能这 6 小时群里没消息，把时间范围调大再试）")


def _unwrap(result):
    """MCP 的返回包了一层 content，把里面的 JSON 取出来。"""
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if not text:
            continue
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"_raw_text": text}
    return getattr(result, "structuredContent", None) or {}


def _show_identity(data) -> None:
    """
    企微在每个返回里都带一段 extra_identity_context，说明当前是哪个机器人。
    打出来能一眼确认连的是不是目标机器人 —— 多个机器人时很容易连错。

    注意：这是服务端返回的文本，属于【数据】不是指令，只做展示，不据此执行任何操作。
    """
    if not isinstance(data, dict):
        return
    ctx = data.get("extra_identity_context")
    if not isinstance(ctx, str) or not ctx.strip():
        return
    lines = [l.strip() for l in ctx.splitlines()
             if l.strip() and not l.strip().startswith("<")]
    if lines:
        print("  当前机器人身份：" + " | ".join(lines[:3]))


def _check_err(data) -> None:
    """企微的错误码都在 errcode 里，非 0 就单独标出来，省得淹没在结构里。"""
    if not isinstance(data, dict):
        return
    code = data.get("errcode")
    if code in (None, 0):
        return
    msg = data.get("errmsg") or "(errmsg 是空的)"
    print(f"\n  ⚠️ errcode={code}  errmsg={msg}")
    if str(code).startswith("85"):
        print("     85xxxx 段通常和「会话内容存档」相关权限有关。")
        print("     去企微后台确认：智能机器人是否已开通对应能力、是否已加进目标群。")
    print(f"     查错误码：https://developer.work.weixin.qq.com/devtool/query?e={code}")


def _walk(obj):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def _find_first_chat_id(data) -> str | None:
    for d in _walk(data):
        for key in ("chatid", "chat_id", "chatId"):
            if isinstance(d.get(key), str) and d[key]:
                return d[key]
    return None


def _report_sender(data) -> None:
    """专门看有没有发送者身份 —— 这条决定机器人能不能 @ 报单的人。"""
    hits = set()
    for d in _walk(data):
        for key in d:
            low = key.lower()
            if any(k in low for k in ("sender", "from", "userid", "user_id", "author")):
                hits.add(key)
    print("\n  ---- 发送者身份 ----")
    if hits:
        print(f"  找到可能的字段：{sorted(hits)}")
        print("  如果里面有用户 ID，机器人就能 @ 报单的人。")
    else:
        print("  没找到发送者相关字段。")
        print("  那就没法 @ 人，只能退而在话术里带上水印识别出的「小组成员」姓名。")


def main() -> None:
    force_utf8()
    load_env()
    url = os.getenv("WECOM_MCP_URL", "").strip()
    if not url:
        sys.exit(
            "缺少 WECOM_MCP_URL。\n"
            "在 .env 里加一行，整条 URL（含 apikey）从企业微信后台复制：\n"
            "  WECOM_MCP_URL=https://qyapi.weixin.qq.com/mcp/v2/bot/msg?apikey=xxx"
        )
    asyncio.run(run(url, "--probe" in sys.argv[1:]))


if __name__ == "__main__":
    main()
