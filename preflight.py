#!/usr/bin/env python3
"""
启动前自检：把「群里说话没反应，日志还干干净净」这类静默失败提前暴露出来。

    python preflight.py

依次检查：配置文件 -> 凭证 -> 连通性与机器人身份 -> 权限 -> 配置与权限是否自洽。
最后一项最关键：require_at: false 却没拿到 im:message.group_msg 权限时，
飞书根本不会推送消息，程序不会报任何错，只能靠这里查出来。
"""

from __future__ import annotations

import json
import os
import sys

import lark_oapi as lark

from botcore.config import CONFIG_PATH, ConfigStore, load_env
from botcore.console import force_utf8
from botcore.llm import LLMJudge

OK, BAD, INFO = "  [OK]  ", "  [!!]  ", "   ·    "

# 权限 -> (说明, 什么时候必需)
SCOPES = {
    "im:message:send_as_bot": "以机器人身份发消息",
    "im:message.group_msg": "接收群里所有消息",
    "im:message.group_at_msg:readonly": "接收群里 @ 机器人的消息",
    "im:message.p2p_msg:readonly": "接收单聊消息",
    "im:resource": "下载消息里的图片/文件（OCR 用）",
}


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def ok(self, msg: str) -> None:
        print(f"{OK}{msg}")

    def bad(self, msg: str, hint: str = "") -> None:
        print(f"{BAD}{msg}")
        if hint:
            for line in hint.splitlines():
                print(f"         {line}")
        self.failures.append(msg)

    def info(self, msg: str) -> None:
        print(f"{INFO}{msg}")


def step(n: int, total: int, title: str) -> None:
    print(f"\n[{n}/{total}] {title}")


def main() -> int:
    force_utf8()
    print("=" * 60)
    print("飞书机器人 · 启动前自检")
    print("=" * 60)
    r = Report()
    total = 6

    # ---------- 1. 配置文件 ----------
    step(1, total, "配置文件")
    try:
        store = ConfigStore()
        settings, matcher = store.snapshot()
        names = "、".join(rule.name for rule in matcher.rules)
        r.ok(f"config.yaml 解析正常，{len(matcher.rules)} 条规则启用：{names}")
        mode = "全量群消息" if not settings.require_at else "仅 @ 机器人的消息"
        r.info(f"监听模式：{mode}（require_at: {str(settings.require_at).lower()}）")
        r.info(f"群聊={settings.enable_group}  单聊={settings.enable_p2p}  "
               f"默认冷却={settings.default_cooldown}s")
    except Exception as exc:
        r.bad(f"config.yaml 有问题：{exc}",
              f"文件位置：{CONFIG_PATH}\n正则关键词记得用单引号，YAML 双引号会把 \\d 当非法转义")
        return finish(r)

    # ---------- 2. 凭证 ----------
    step(2, total, "凭证")
    load_env()
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        r.bad("飞书凭证缺失",
              "把 .env.example 复制为 .env，填入 FEISHU_APP_ID / FEISHU_APP_SECRET。\n"
              "凭证在 https://open.feishu.cn/app 的「凭证与基础信息」。")
        return finish(r)
    masked = app_id[:8] + "…" if len(app_id) > 8 else app_id
    r.ok(f".env 已加载，App ID = {masked}")

    client = lark.Client.builder().app_id(app_id).app_secret(app_secret).build()

    # ---------- 3. 连通性与机器人身份 ----------
    step(3, total, "连通性与机器人身份")
    bot_ok = False
    try:
        req = (lark.BaseRequest.builder()
               .http_method(lark.HttpMethod.GET)
               .uri("/open-apis/bot/v3/info")
               .token_types({lark.AccessTokenType.TENANT})
               .build())
        resp = client.request(req)
        body = json.loads(resp.raw.content.decode("utf-8"))

        if body.get("code") == 0:
            bot = body.get("bot") or {}
            r.ok("凭证有效，已成功取得 tenant_access_token")
            r.ok(f"机器人能力已开启：{bot.get('app_name', '?')}（open_id: {bot.get('open_id', '?')}）")
            bot_ok = True
        else:
            code, msg = body.get("code"), body.get("msg")
            hint = "检查 .env 里的 App ID / App Secret 是否填对、有无多余空格"
            if code in (10003, 10013):
                hint = "多半是「添加应用能力 -> 机器人」没有启用，去后台开启后重新发布版本"
            r.bad(f"接口返回错误 code={code} msg={msg}", hint)
    except Exception as exc:
        r.bad(f"请求飞书失败：{exc}", "检查网络，以及是否需要代理才能访问 open.feishu.cn")

    if not bot_ok:
        return finish(r)

    # ---------- 4. 权限 ----------
    step(4, total, "权限")
    granted = fetch_scopes(client, r)

    if granted is None:
        r.info("无法自动获取权限列表，请到开放平台「权限管理」页面手动核对下列权限")
        for name, desc in SCOPES.items():
            r.info(f"{name:<38}{desc}")
    else:
        for name, desc in SCOPES.items():
            if name in granted:
                r.ok(f"{name:<38}{desc}")
            else:
                r.info(f"{name:<38}{desc}  （未授权）")

    # ---------- 5. 配置与权限一致性 ----------
    step(5, total, "配置与权限一致性")
    check_consistency(settings, granted, r)

    # ---------- 6. 模型 ----------
    step(6, total, "模型判断（agent）")
    check_llm(store, r)

    return finish(r)


def check_llm(store: ConfigStore, r: Report) -> None:
    llm = store.llm_settings()

    if not llm.enabled:
        r.info("agent 未启用（config.yaml 里 agent.enabled: false），跳过")
        return

    r.info(f"服务商：{llm.base_url}")
    r.info(f"模型：{llm.model}   超时 {llm.timeout}s   限速 {llm.max_calls_per_minute}/分钟")

    api_key = os.getenv(llm.api_key_env, "").strip()
    if not api_key:
        r.bad(
            f"agent 已启用，但环境变量 {llm.api_key_env} 为空",
            f"在 .env 里加一行：{llm.api_key_env}=你的APIKey\n"
            "DeepSeek 的 key 在 https://platform.deepseek.com 注册充值后获取",
        )
        return
    r.ok(f"{llm.api_key_env} 已配置（{api_key[:6]}…）")

    if not llm.prefilter:
        r.info("没有配 prefilter，意味着每条群消息都会调模型 —— 费用会很高，建议配上")
    else:
        r.ok(f"粗筛词 {len(llm.prefilter)} 个，只有命中的消息才会调模型")

    # 真调一次，验 key、网络、以及返回格式。花费极小，但能挡掉上线后才发现的问题。
    probe = "今天新增199一个"
    r.info(f"正在实测调用（用例：{probe}）……")

    judge = LLMJudge(llm, api_key)
    result = judge.judge(probe)

    if result is None:
        r.bad(
            "模型调用失败，详见上面几行 WARNING 日志",
            "常见原因：API Key 错、余额不足、base_url 或 model 填错、网络不通。\n"
            "注意 base_url 和 model 要配套 —— 换服务商两个都得改。",
        )
        return

    if not result["is_report"]:
        r.bad(
            "模型把明显的报单判成了「不是报单」",
            "检查 config.yaml 里 agent.context 的业务背景描述是否写清楚了。",
        )
        return

    r.ok("模型调用成功，返回格式正确")
    r.info(f"识别结果：新增={result['is_new']}  套餐={result['package']}  单量={result['count']}")
    r.info(f"生成回复：{result['reply']}")


def fetch_scopes(client: lark.Client, r: Report) -> set[str] | None:
    """取应用已授权的权限集合。取不到返回 None（降级为手动核对）。"""
    try:
        from lark_oapi.api.application.v6 import ListScopeRequest

        resp = client.application.v6.scope.list(ListScopeRequest.builder().build())
        if not resp.success():
            r.info(f"权限列表接口返回 code={resp.code} msg={resp.msg}")
            return None

        granted: set[str] = set()
        for scope in (getattr(resp.data, "scopes", None) or []):
            name = getattr(scope, "scope_name", None)
            if not name:
                continue
            status = getattr(scope, "grant_status", None)
            # grant_status: 1 = 已授权。拿不到该字段时保守地认为已授权。
            if status is None or status == 1:
                granted.add(name)
        return granted
    except Exception as exc:
        r.info(f"权限列表获取失败：{exc}")
        return None


def check_consistency(settings, granted: set[str] | None, r: Report) -> None:
    if granted is None:
        r.info("跳过（权限列表未知）。请务必自行确认下面这条：")
        if not settings.require_at:
            r.info("require_at: false 必须拿到 im:message.group_msg，否则群里将完全无反应")
        return

    if not settings.require_at and "im:message.group_msg" not in granted:
        r.bad(
            "require_at: false 需要 im:message.group_msg，但该权限未授权",
            "后果：飞书不会推送非 @ 的群消息，机器人在群里完全无反应，\n"
            "      且程序不报错、日志干净，极易误判成代码 bug。\n"
            "处理：① 去开放平台申请该权限，等管理员审批通过；\n"
            "      ② 或先把 config.yaml 改成 require_at: true，\n"
            "         用 @ 机器人的方式验证全链路（该权限申请即批）。",
        )
    elif settings.require_at and "im:message.group_at_msg:readonly" not in granted:
        r.bad(
            "require_at: true 需要 im:message.group_at_msg:readonly，但该权限未授权",
            "处理：去开放平台「权限管理」申请，这个权限申请即批。",
        )
    else:
        r.ok("监听模式与已授权权限匹配")

    if "im:message:send_as_bot" not in granted:
        r.bad(
            "缺少 im:message:send_as_bot，机器人能收到消息但发不出回复",
            "处理：去开放平台「权限管理」申请该权限。",
        )

    if settings.enable_p2p and "im:message.p2p_msg:readonly" not in granted:
        r.info("enable_p2p: true 但缺 im:message.p2p_msg:readonly，单聊消息收不到（不影响群聊）")


def finish(r: Report) -> int:
    print()
    print("=" * 60)
    if r.failures:
        print(f"结论：{len(r.failures)} 项未通过，先处理上面标 [!!] 的问题再启动")
        print("=" * 60)
        return 1
    print("结论：全部通过。可以启动了 -> python bot.py")
    print("      启动后在群里发一句「机器人自检」验证全链路")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
