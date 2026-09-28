#!/usr/bin/env python3
"""
把机器人的台账能力包成 MCP 服务器，给 TeleAgent（星辰超级智能体）桌面版调用。

    python mcp_server.py            # stdio 模式，TeleAgent「导入 MCP 工具」用这个
    python mcp_server.py --http     # streamable-http 模式，http://127.0.0.1:8765/mcp
    python mcp_server.py --check    # 自检：自己起一个 stdio 服务端，列工具、调两个，看通不通

TeleAgent 导入配置（stdio）：
    {
      "mcpServers": {
        "yeji": {
          "command": "D:\\\\feishu_bot\\\\.venv\\\\Scripts\\\\python.exe",
          "args": ["D:\\\\feishu_bot\\\\mcp_server.py"]
        }
      }
    }

为什么是独立进程：机器人进程（bot.py）在群里收发消息，这个进程只读同一份
campaign.json / roster.json，每次调用都重新读文件，所以永远拿到最新的单。

⚠️ 台账（campaign.json）【只有机器人进程能写】。这边写了会被机器人下一次记单
整文件覆盖掉（审查踩出来的）。这边唯一的写操作是改名册文件 roster.json，
机器人进程看到文件变了会自己重载并重新归队。

工具和群里 @机器人 问答用的是同一套 ledger_tools —— 主管在桌面上问
「东林今天几单」和员工在群里问，查的是同一段代码、同一份台账。
"""

from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path

# stdio 模式下 stdout 是协议通道，日志只能走 stderr
logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                    format="%(asctime)s %(levelname)-5s %(name)s | %(message)s")
log = logging.getLogger("mcp_server")

from mcp.server.mcpserver import MCPServer  # noqa: E402

from botcore.bulletin import bulletin_text, render_png  # noqa: E402
from botcore.checkin import day_sheets, load_records, select, summarize  # noqa: E402
from botcore.campaign import (Campaign, NotRosterSheet, Roster, RosterImportError,  # noqa: E402
                              parse_order)
from botcore.config import ConfigStore, load_env  # noqa: E402
from botcore.ledger_tools import LedgerTools, resolve_period  # noqa: E402
from botcore.paths import DATA  # noqa: E402
from botcore.report import build_workbook, summary_title  # noqa: E402

load_env()
store = ConfigStore()


def _campaign() -> Campaign:
    """每次都重新读文件：台账是机器人进程在写，这边不能缓存。"""
    # config.yaml 改了目标值 / 活动名，这个长跑进程也要跟上（以前要重启 TeleAgent 才生效）
    store.maybe_reload()
    return Campaign(Roster(), store.campaign_settings().targets)


tools = LedgerTools(_campaign, store.campaign_settings)

server = MCPServer(
    name="yeji-ledger",
    title="业绩群台账",
    instructions=(
        "中国电信一线营销团队业绩群的台账工具。所有数字来自机器人记的清单，"
        "不要自己估算。period 可填 today / yesterday / week / last_week / month / "
        "YYYY-MM-DD / YYYY-MM-DD~YYYY-MM-DD。「积分」指价值积分；"
        "「待核对」的单是缺字段等人补，计数照常。"
    ),
)


@server.tool(description="各战队汇总：新增宽带、中高套、长续约、千兆、单号卡、云盒、积分、单数、综合进度、排名，以及目标值。")
def team_stats(period: str = "today") -> dict:
    return tools.team_stats(period)


@server.tool(description="各小组汇总（每组两人）。team 可只看某个战队；only_zero=true 只列一单都没有的小组。")
def group_stats(period: str = "today", team: str = "", only_zero: bool = False) -> dict:
    return tools.group_stats(period, team, only_zero)


@server.tool(description="某个人的业绩：单数、积分、每一单明细，以及他在名册里的小组和战队。")
def person_stats(name: str, period: str = "today") -> dict:
    return tools.person_stats(name, period)


@server.tool(description="按时间倒序列出清单里的单：时间、类型、套餐、揽收人、战队、小组、积分。team 可过滤。")
def list_orders(period: str = "today", team: str = "", limit: int = 50) -> dict:
    return tools.list_orders(period, team, limit)


@server.tool(description="查名册：某人在哪个小组和战队；或某战队有哪些小组和成员；query 留空返回所有战队。")
def roster(query: str = "") -> dict:
    return tools.roster(query)


@server.tool(description="生成业绩通报的文字（前两名恭喜、末两名加油）。")
def bulletin_message(period: str = "today") -> dict:
    since, until, label = resolve_period(period)
    camp = _campaign()
    teams = camp.stats(since, until)
    # 查的是过去某天就按那天的 23:59 落款，别写成今天
    now = datetime.now()
    if until[:10] < now.strftime("%Y-%m-%d"):
        now = datetime.strptime(until, "%Y-%m-%d %H:%M:%S")
    text = bulletin_text(teams, store.bulletin_settings(), store.campaign_settings(), now)
    return {"范围": label, "通报文字": text}


@server.tool(description="生成 Excel（「晒单数据」+「清单」两张表，打卡核验开着时再加排名那天上午/下午/晚上三张「打卡」表）"
                         "和表格图 PNG，返回文件路径。rank_period 是排名范围，ledger_period 是清单范围。")
def export_report(rank_period: str = "today", ledger_period: str = "week") -> dict:
    now = datetime.now()
    r_since, r_until, r_label = resolve_period(rank_period, now)
    l_since, l_until, l_label = resolve_period(ledger_period, now)
    camp = _campaign()
    cfg = store.campaign_settings()
    # 「今天（9.22）」取括号里的；「9.8 至 9.14」写成「9.8-9.14」，
    # 不然标题拼出来是「9.8 至 9.14日各支局…」
    period = r_label.split("（")[-1].rstrip("）") if "（" in r_label else r_label
    title = summary_title(period.replace(" 至 ", "-"), cfg.title, now)
    out_dir = DATA / store.bulletin_settings().out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"业绩通报_{now:%m%d_%H%M%S}"
    ck = store.checkin_settings()
    sheets = day_sheets(ck, r_until[:10], cfg.title) if ck.enabled else None
    xlsx = build_workbook(camp, out_dir / f"{stem}.xlsx", title,
                          rank=(r_since, r_until), ledger=(l_since, l_until),
                          checkin_sheets=sheets, checkin_columns=ck.columns)
    png = render_png(camp.group_stats(r_since, r_until), camp.stats(r_since, r_until),
                     camp.targets, title)
    png_path = out_dir / f"{stem}.png"
    png_path.write_bytes(png)
    return {"excel": str(xlsx), "png": str(png_path), "排名范围": r_label, "清单范围": l_label,
            "单数": len([o for o in camp.orders if l_since <= o.at <= l_until])}


@server.tool(description="把一段晒单文字按机器人的口径解析成清单字段（不入清单，只解析），用来核对某条晒单会怎么记。")
def parse_order_text(text: str) -> dict:
    o = parse_order(text, "mcp", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    if o is None:
        return {"能否严格解析": False, "说明": "不是固定格式的晒单（或是提问 / 多单），机器人会交给模型容错解析或不入清单"}
    return {"能否严格解析": True, "类型": o.kind, "套餐": o.fee, "支局": o.team, "揽收人": o.collector,
            "新增宽带": o.broadband, "中高套": o.high, "长续约": o.renew, "千兆": o.giga,
            "单号卡": o.card, "FTTR": o.fttr, "云盒AI智屏": o.box, "积分": o.points,
            "待核对": o.needs_review, "备注": o.note}


@server.tool(description="打卡情况：每张今日水印照片算一次打卡。按小组统计业务打卡（工作内容里有办成的业务，表里标绿）、"
                         "有效打卡（现场拦截/设备演示/板夹/算账等）、无效打卡（标红）、待判定，并列出一条打卡都没有的小组。"
                         "slot 可填 上午 / 下午 / 晚上，留空是全天；team 可只看某个战队。")
def checkin_stats(period: str = "today", slot: str = "", team: str = "") -> dict:
    since, until, label = resolve_period(period)
    ck = store.checkin_settings()
    records = select(load_records(DATA / ck.store_file), slot=slot, since=since, until=until, slots=ck.slots)
    camp = _campaign()
    out = summarize(records, camp.roster)
    if team:
        key = team.strip().removesuffix("战队").removesuffix("支局").removesuffix("战区")
        out["各小组"] = [g for g in out["各小组"] if key in g["战队"]]
        out["没有打卡的小组"] = [g for g in out["没有打卡的小组"] if key in g]
    out = {"范围": label + (f" {slot}" if slot else ""), "时段": [s.get("name") for s in ck.slots], **out}
    if not ck.enabled:
        out["说明"] = "打卡核验没开启（config.yaml 的 checkin.enabled）"
    return out


@server.tool(description="把完整通报（文字 + 表格图 + Excel）发到业绩群。只有明确要求「发到群里」时才用。")
def send_bulletin_to_group() -> dict:
    store.maybe_reload()
    settings = store.snapshot()[0]
    if settings.platform != "feishu":
        # 企微发图 / 发文件必须走长连接，而长连接只在机器人进程里建着；
        # 这里另起一条会和机器人抢同一个 BotID 的连接。企微上请在群里喊「通报」
        return {"error": "企微平台下请在群里 @机器人 发「通报」，由机器人进程发送；TeleAgent 这里只能查数和导出文件"}
    from botcore.platforms import feishu as plat
    try:
        platform = plat.from_env()
    except SystemExit as exc:
        # from_env 缺凭证是 SystemExit —— 不接住会把整个 MCP 进程退出，
        # TeleAgent 里 10 个工具一起消失（审查踩出来的）
        return {"error": f"平台凭证不全，发不了群：{exc}"}
    chats = tuple(getattr(platform.cfg, "chat_ids", ()) or ())
    if not chats:
        return {"error": "没配 FEISHU_CHAT_IDS / WECOM_CHAT_IDS，不知道发到哪个群"}
    from concurrent.futures import ThreadPoolExecutor
    from botcore.bulletin import Bulletin
    b = Bulletin(platform, store, _campaign, ThreadPoolExecutor(1))
    res = b.publish(chats, "TeleAgent")
    if res.get("error"):
        return {"error": res["error"]}
    if res["failed"]:
        return {"error": f"发送失败的群：{res['failed']}（看机器人日志）", "发到": res["ok"]}
    return {"ok": True, "发到": res["ok"]}


@server.tool(description="从 Excel 文件更新名册（姓名 | 分组 | 战区 三列的「分组」sheet）。path 是本机文件路径。机器人会自动按新名册重新归队。")
def import_roster(path: str) -> dict:
    p = Path(path)
    if not p.is_file():
        return {"error": f"文件不存在：{path}"}
    # 只改名册文件，不碰台账：台账由机器人进程按新名册重新归队（它看 roster.json 的 mtime）
    try:
        summary = Roster().import_workbook(p.read_bytes())
    except NotRosterSheet:
        return {"error": "这个 Excel 里没有「分组」sheet（姓名 | 分组 | 战区 三列），不是名册表，名册没动"}
    except RosterImportError as exc:
        return {"error": f"名册没更新成：{exc}"}
    return {"ok": True, "名册": summary, "说明": "机器人进程会在下一次记单 / 通报时按新名册重新归队"}


def _self_check() -> int:
    """用官方 MCP 客户端连自己（stdio），列出工具并调两个。导入 TeleAgent 前先跑这个。"""
    import asyncio

    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def run() -> int:
        params = StdioServerParameters(
            command=sys.executable, args=[str(Path(__file__).resolve())],
            cwd=str(Path(__file__).resolve().parent), env={"PYTHONIOENCODING": "utf-8"})
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as sess:
                await sess.initialize()
                names = [t.name for t in (await sess.list_tools()).tools]
                print(f"[OK] 服务端启动正常，{len(names)} 个工具：{'、'.join(names)}")
                for name, args in (("roster", {}), ("team_stats", {"period": "today"})):
                    res = await sess.call_tool(name, args)
                    text = res.content[0].text if res.content else ""
                    print(f"[OK] {name}({args}) -> {text[:160].replace(chr(10), ' ')}…")
        print()
        print("可以导入 TeleAgent 了。配置里 command 填（JSON 里反斜杠要写成两个）：")
        print("  " + __import__("json").dumps(sys.executable))
        print("args 填：")
        print("  " + __import__("json").dumps([str(Path(__file__).resolve())]))
        return 0

    try:
        return asyncio.run(run())
    except Exception as exc:
        print(f"[FAIL] {exc}")
        return 1


if __name__ == "__main__":
    if "--check" in sys.argv:
        sys.exit(_self_check())
    if "--http" in sys.argv:
        log.info("MCP 服务器（streamable-http）：http://127.0.0.1:8765/mcp")
        server.run(transport="streamable-http", host="127.0.0.1", port=8765)
    else:
        server.run(transport="stdio")
