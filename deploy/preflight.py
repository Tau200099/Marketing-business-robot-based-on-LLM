#!/usr/bin/env python3
"""
上线自检。部署到服务器后【先跑这个】，再启动机器人。

    python deploy/preflight.py

逐项检查依赖、配置、凭证、时区、连通性。任何一项标 ✗ 就先别启动。
"""
import importlib.util
import os
import sys

# Windows 控制台默认 GBK，输出 ✓ 会崩。服务器上一般是 UTF-8，但自检脚本
# 本身不能因为编码问题跑不起来。
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
ok_all = True


def check(name, fn):
    """
    跑一项检查。

    ⚠️ 要连 SystemExit 一起接住 —— 配置缺失时 ConfigStore 抛的是 SystemExit，
    只接 Exception 的话自检会中途崩掉，后面的项目全不跑。
    部署时最需要的是【一次看到所有问题】，而不是修一个跑一次。
    """
    global ok_all
    try:
        good, detail = fn()
    except SystemExit as exc:
        good, detail = False, str(exc).replace(chr(10), " ")[:110]
    except Exception as exc:
        good, detail = False, f"{type(exc).__name__}: {exc}"
    ok_all &= good
    print(f"  {'✓' if good else '✗'}  {name:<22} {detail}")


def c_python():
    v = sys.version_info
    return v >= (3, 10), f"{v.major}.{v.minor}.{v.micro}"


def c_deps():
    need = ["yaml", "dotenv", "requests", "rapidocr_onnxruntime",
            "onnxruntime", "mcp", "aibot", "openpyxl", "PIL"]
    missing = [m for m in need if not importlib.util.find_spec(m)]
    return not missing, "全部就位" if not missing else f"缺 {missing}"


def c_timezone():
    """
    【最容易翻车的一项】。所有定时规则用的都是本机时间：
    早上加油 8:30、下午加油 14:00、活跃时段 8:30-20:30、通报 11:50/21:00。
    服务器默认常是 UTC，那样全部会差 8 小时。
    """
    off = -time.timezone // 3600 if not time.daylight else -time.altzone // 3600
    tz = os.environ.get("TZ") or time.tzname[0]
    good = off == 8
    return good, (f"UTC+{off} ({tz}) 现在 {datetime.now():%H:%M}"
                  + ("" if good else "  ← 必须是 UTC+8，否则定时全错"))


def c_env():
    from botcore.config import load_env
    load_env()
    need = {"LLM_API_KEY": "大模型 key",
            "WECOM_BOT_ID": "长连接",
            "WECOM_BOT_SECRET": "长连接",
            "WECOM_CHAT_IDS": "目标群（定时通报要）"}
    opt = {"WECOM_MCP_URL": "可选，留空文字走长连接",
           "VISION_API_KEY": "打卡核验视觉模型"}
    miss = [f"{k}({v})" for k, v in need.items() if not os.getenv(k, "").strip()]
    lack = [k for k in opt if not os.getenv(k, "").strip()]
    detail = "必填齐全" if not miss else f"缺 {miss}"
    if lack:
        detail += f"；可选未配 {lack}"
    return not miss, detail


def c_config():
    from botcore.config import ConfigStore
    st = ConfigStore()
    s, m = st.snapshot()
    return True, f"platform={s.platform} 规则{len(m.rules)}条"


def c_state_writable():
    """
    ⚠️ 要查【数据目录】能不能写，不是当前目录。
    Docker 里代码在 /app（只读）、数据在 /data（挂载卷），两者不是一回事。
    卷没挂对的话，机器人照样能跑，但统计数据一删容器就没了。
    """
    from botcore.paths import DATA, ROOT
    files = ["tally.json", "campaign.json", "sendlog.json",
             "stickers_cache.json", "stickers_state.json", "roster.json"]
    probe = DATA / ".preflight_write_test"
    try:
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
    except Exception as exc:
        return False, f"数据目录不可写 {DATA}：{exc}"
    exist = [f for f in files if (DATA / f).exists()]
    where = "= 代码目录" if DATA == ROOT else f"= {DATA}"
    return True, f"可写（{where}）；已有状态文件 {len(exist)}/{len(files)}"


def c_stickers():
    from botcore.config import ConfigStore
    from botcore.stickers import StickerPool, CATEGORIES
    cfg = ConfigStore().sticker_settings()
    if not cfg.enabled:
        return True, "未启用（跳过）"
    pool = StickerPool(cfg)
    empty = [c for c in CATEGORIES if not pool.has(c)]
    return not empty, ("六类齐全" if not empty else f"这些分类没有图：{empty}")


def c_roster():
    """
    名册。揽收人靠它归到小组/战区，缺了的话所有单都是「未归队」，
    清单和排名全废 —— 而且日志里只会看到一行 warning，很容易漏掉。
    """
    from botcore.campaign import Roster
    r = Roster()
    if not r.people:
        return False, f"名册为空或不存在（找的是 {r.path}）"
    return len(r.teams) > 0, f"{len(r.people)} 人 / {len(r.teams)} 个战队"


def c_ledger():
    """清单文件能不能读写。存着活动期间每一单，丢了要重新录。"""
    from botcore.campaign import Campaign, Roster
    c = Campaign(Roster())
    return True, f"{len(c.orders)} 单（{c.path.name}）"


def c_font():
    """通报图片要画中文，服务器上常常一个中文字体都没有。"""
    from pathlib import Path as P
    cands = [
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/arphic/uming.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
    ]
    found = [c for c in cands if P(c).exists()]
    if found:
        return True, P(found[0]).name
    return False, "没有中文字体，通报图片会显示成方块。装：apt install fonts-wqy-zenhei"


def c_wecom():
    import socket, ssl
    host = "openws.work.weixin.qq.com"
    with socket.create_connection((host, 443), timeout=10) as raw:
        with ssl.create_default_context().wrap_socket(raw, server_hostname=host) as tls:
            return True, f"{host}:443 TLS 握手成功（{tls.version()}）"


def c_llm():
    import requests
    from botcore.config import ConfigStore, load_env
    load_env()
    st = ConfigStore()
    llm = st.llm_settings()
    key = os.getenv(llm.api_key_env, "").strip()
    if not key:
        return False, f"环境变量 {llm.api_key_env} 为空"
    r = requests.post(
        llm.base_url.rstrip("/") + "/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={"model": llm.model, "max_tokens": 4,
              "messages": [{"role": "user", "content": "hi"}]},
        timeout=25,
    )
    return r.status_code == 200, f"{llm.model} HTTP {r.status_code}"


print("\n上线自检\n" + "=" * 58)
check("Python 版本", c_python)
check("依赖", c_deps)
check("时区", c_timezone)
check("环境变量", c_env)
check("配置文件", c_config)
check("状态目录可写", c_state_writable)
check("名册", c_roster)
check("清单", c_ledger)
check("表情包图库", c_stickers)
check("中文字体", c_font)
check("企微长连接可达", c_wecom)
check("大模型连通", c_llm)
print("=" * 58)
print(("全部通过，可以启动" if ok_all else "有未通过项，先解决再启动") + "\n")
sys.exit(0 if ok_all else 1)
