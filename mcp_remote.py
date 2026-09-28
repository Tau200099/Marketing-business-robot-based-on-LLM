#!/usr/bin/env python3
"""
TeleAgent ↔ 服务器台账的转接器（在你电脑上跑，TeleAgent 连的是它）。

    TeleAgent ──stdio──> mcp_remote.py（本机） ──SSH──> mcp_server.py（服务器，读服务器上的台账）

为什么要多这一层：
    机器人和台账 2026-09-23 起在服务器上。查数直接转发就行，但有两样涉及【文件】：
    · export_report 生成的 Excel / 表格图在服务器上，TeleAgent 在本机做周报 PPT 时找不到
      → 这里生成完就用 scp 拉回本机 reports/，把【本机路径】交给 TeleAgent
    · import_roster 给的是本机 Excel 路径，服务器上没有这个文件
      → 这里先 scp 传上去，再让服务器按服务器路径导入

安全：只走 SSH（密钥登录），服务器不开任何端口；文件只在本机和服务器之间直传。

TeleAgent 导入配置见 teleagent/mcp_config.remote.json；自检：
    python mcp_remote.py --check
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import mcp_types as types
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.lowlevel.server import Server
from mcp.server.stdio import stdio_server

# stdio 模式下 stdout 是协议通道，日志只能走 stderr
logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s %(levelname)-5s mcp_remote | %(message)s")
log = logging.getLogger("mcp_remote")

HOST = "root@47.109.158.170"
APP = "/opt/feishu-bot"
UPLOAD_DIR = f"{APP}/data/uploads"
OPENSSH = Path(r"C:\Windows\System32\OpenSSH")
SSH = str(OPENSSH / "ssh.exe") if (OPENSSH / "ssh.exe").exists() else "ssh"
SCP = str(OPENSSH / "scp.exe") if (OPENSSH / "scp.exe").exists() else "scp"
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ServerAliveInterval=30", "-o", "ConnectTimeout=15"]
LOCAL_REPORTS = Path(__file__).resolve().parent / "reports"


def _env() -> dict:
    # TeleAgent 给子进程的环境可能很精简；Windows 的 ssh.exe 缺 PROGRAMDATA 会一声不响地退出（rc=255）
    env = dict(os.environ)
    env.setdefault("PROGRAMDATA", r"C:\ProgramData")
    return env


def _scp(src: str, dst: str) -> None:
    r = subprocess.run([SCP, *SSH_OPTS, src, dst], capture_output=True, env=_env(), timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"scp 失败（{r.returncode}）：{r.stderr.decode('utf-8', 'replace')[-200:]}")


def _text_result(data: dict, is_error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(data, ensure_ascii=False, indent=2))],
        is_error=is_error)


def _payload(res) -> dict | None:
    """服务器工具返回的是一段 JSON 文本，拆成 dict；拆不出来就返回 None（原样转发）。"""
    try:
        return json.loads(res.content[0].text)
    except Exception:
        return None


async def _export_report(remote: ClientSession, args: dict):
    res = await remote.call_tool("export_report", args)
    data = _payload(res)
    if res.is_error or not data or "excel" not in data:
        return res
    LOCAL_REPORTS.mkdir(parents=True, exist_ok=True)
    for key in ("excel", "png"):
        remote_path = data.get(key)
        if not remote_path:
            continue
        local = LOCAL_REPORTS / Path(remote_path).name
        await asyncio.to_thread(_scp, f"{HOST}:{remote_path}", str(local))
        data[key] = str(local)
    data["说明"] = "文件已从服务器拉回本机 reports 目录，可以直接用来做 PPT / 发给别人"
    log.info("报表已拉回本机：%s", data.get("excel"))
    return _text_result(data)


async def _import_roster(remote: ClientSession, args: dict):
    path = str(args.get("path") or "")
    local = Path(path)
    if path and local.is_file():
        # 本机文件：先传到服务器，再按服务器路径导入
        remote_path = f"{UPLOAD_DIR}/{int(time.time())}_{local.name}"
        mk = subprocess.run([SSH, *SSH_OPTS, HOST, f"mkdir -p {UPLOAD_DIR}"],
                            capture_output=True, env=_env(), timeout=60)
        if mk.returncode != 0:
            return _text_result({"error": "连不上服务器，名册没传上去"}, True)
        await asyncio.to_thread(_scp, str(local), f"{HOST}:{remote_path}")
        log.info("名册文件已上传：%s -> %s", local, remote_path)
        args = {**args, "path": remote_path}
    return await remote.call_tool("import_roster", args)


SPECIAL = {"export_report": _export_report, "import_roster": _import_roster}


async def serve() -> None:
    params = StdioServerParameters(
        command=SSH,
        args=[*SSH_OPTS, HOST,
              f"cd {APP} && BOT_DATA_DIR={APP}/data PYTHONIOENCODING=utf-8 exec .venv/bin/python mcp_server.py"],
        env=_env())
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as remote:
            init = await remote.initialize()
            tools = (await remote.list_tools()).tools
            log.info("已连上服务器台账，%d 个工具", len(tools))

            async def on_list_tools(ctx, params):
                return types.ListToolsResult(tools=tools)

            async def on_call_tool(ctx, params):
                name, args = params.name, dict(params.arguments or {})
                try:
                    handler = SPECIAL.get(name)
                    if handler:
                        return await handler(remote, args)
                    return await remote.call_tool(name, args)
                except Exception as exc:
                    log.exception("工具 %s 出错", name)
                    return _text_result({"error": f"{name} 出错：{exc}"}, True)

            server = Server("yeji-ledger", title="业绩群台账（服务器）",
                            instructions=init.instructions,
                            on_list_tools=on_list_tools, on_call_tool=on_call_tool)
            async with stdio_server() as (rs, ws):
                await server.run(rs, ws, server.create_initialization_options())


def _self_check() -> int:
    """模拟 TeleAgent：起一个本转接器，列工具、查名册、导出一份报表看文件有没有拉回来。"""

    async def run() -> int:
        params = StdioServerParameters(command=sys.executable, args=[str(Path(__file__).resolve())],
                                       env={**_env(), "PYTHONIOENCODING": "utf-8"})
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as sess:
                await sess.initialize()
                names = [t.name for t in (await sess.list_tools()).tools]
                print(f"[OK] 经转接器连上服务器，{len(names)} 个工具")
                res = await sess.call_tool("roster", {})
                print(f"[OK] roster -> {res.content[0].text[:80].replace(chr(10), ' ')}…")
                res = await sess.call_tool("export_report", {"rank_period": "today", "ledger_period": "week"})
                data = json.loads(res.content[0].text)
                for key in ("excel", "png"):
                    p = Path(data.get(key, ""))
                    print(f"[{'OK' if p.is_file() else 'FAIL'}] {key} 本机文件：{p}（{p.stat().st_size if p.is_file() else 0} 字节）")
        return 0

    try:
        return asyncio.run(run())
    except Exception as exc:
        print(f"[FAIL] {exc}")
        return 1


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(_self_check())
    asyncio.run(serve())
