"""冒烟：所有模块能 import、配置能解析、关键设置在。改了哪个文件导致启动就崩，这里最先发现。"""
import importlib
import pkgutil

from _harness import check, done, run, setup

setup()

import botcore  # noqa: E402


def test_import_all():
    for m in pkgutil.walk_packages(botcore.__path__, "botcore."):
        try:
            importlib.import_module(m.name)
            check(True, m.name)
        except Exception as exc:
            check(False, f"import {m.name} 失败：{exc}")
    for name in ("bot", "mcp_server"):
        try:
            importlib.import_module(name)
            check(True, name)
        except SystemExit:
            check(True, name)
        except Exception as exc:
            check(False, f"import {name} 失败：{exc}")


def test_config():
    from botcore.config import ConfigStore
    st = ConfigStore()
    g, matcher = st.snapshot()
    check(g.platform == "wecom", f"平台是 wecom（实际 {g.platform}）")
    check(len(matcher.rules) >= 1, "至少一条规则")
    ck = st.checkin_settings()
    check(ck.enabled and ck.vision_model, "打卡核验开着、配了视觉模型")
    check(len(ck.slots) == 3, "三个时段")
    check(st.backup_settings().admins, "配了管理员")
    check(st.campaign_settings().title, "有活动名")


run(test_import_all)
run(test_config)
done()
