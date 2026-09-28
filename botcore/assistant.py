"""
问答助手：群里 @机器人 问「今天各战队情况」「杨桐这周多少分」「哪些组还没晒单」，
模型带着几个【查台账的工具】去答。

和 llm.py 那个「判报单」的模型是两回事：
    判报单   每条报单都过，固定 JSON 出参，一次调用，追求快和便宜
    问答     只有 @ 机器人问问题才过，工具调用循环（查数 -> 组织语言），
             追求答得准 —— 所有数字必须来自工具，模型不许自己算

为什么数字必须来自工具：
    台账、排名、积分都是代码算好的，模型只负责「听懂问题 -> 选工具 -> 把结果说成人话」。
    让模型自己加总一定会错，而群里的回答是要被人拿去对的。

隐私：工具只返回姓名 / 小组 / 战队 / 套餐 / 积分，台账里本来就没有客户号码。
      提问文本发出去前也过一遍 redact()。

出参给飞书的是纯文本消息，不渲染 markdown，所以系统提示词里要求
「不要表格、不要井号」，一行一条。
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime

import requests

from .campaign import Campaign, CampaignSettings
from .distill import redact
from .ledger_tools import LedgerTools

log = logging.getLogger("botcore.assistant")

# 模型判定「这不是提问，是报单」时的约定回复，pipeline 看到它就走点赞那条路
NOT_A_QUESTION = "#报单#"

# 「像在提问」的本地判断：命中任一就交给助手。宁可宽一点 —— 漏掉的助手也会
# 用 #报单# 退回来，只是多花一次调用
_QUESTION_HINT = re.compile(
    r"[?？]|几单|几个|几笔|多少|哪些|哪个|哪几|谁|怎么|怎样|什么|是否|有没有|有几|"
    r"排名|排第|第几|情况|进度|统计|汇总|明细|通报|清单|名册|查一?下|问一?下|^问|^查|^看")


def looks_like_question(text: str) -> bool:
    return bool(_QUESTION_HINT.search((text or "").strip()))


@dataclass
class AssistantSettings:
    enabled: bool = False
    # 群里只有 @ 了机器人才当提问；私聊一律当提问
    only_when_mentioned: bool = True
    # 只把「像在提问」的文字交给助手（有问号 / 几 / 多少 / 哪些 / 谁 / 排名 …）。
    # 企微上每条消息都是 @ 来的，不加这道门的话每条口语报单都要先白调一次助手模型
    question_only: bool = True
    # 工具调用最多几轮（一轮 = 模型调一次工具再看结果）。问题再复杂 4 轮也够了
    max_rounds: int = 4
    timeout: float = 20.0
    temperature: float = 0.2
    max_tokens: int = 600
    # 每分钟最多回答几个问题，防刷
    max_calls_per_minute: int = 20
    # 留空就复用 agent 段的服务商和模型
    base_url: str = ""
    model: str = ""
    api_key_env: str = ""
    # 回答时 @ 提问的人
    at_sender: bool = True
    # 模型出错时回这句；留空就不回
    error_reply: str = "我这会儿查不了，稍后再问一次。"
    # 额外的业务说明，拼进系统提示词
    context: str = ""


SYSTEM_PROMPT = """你是中国电信一线营销团队业绩群的助手。群成员会 @ 你问业绩相关的问题，你用工具查台账后回答。

现在是 {now}（{weekday}）。活动名：{title}。
统计口径：排名和综合进度按【{rank_period}】算，清单记录按【{ledger_period}】算。
战队：{teams}。

【必须遵守】
1. 所有数字（单数、积分、排名、进度）只能来自工具返回，不能自己估算或加总。没查到就说没查到。
2. 问「今天」用 period=today，问「这周/本周」用 period=week，问「昨天」用 yesterday，问具体日期用 YYYY-MM-DD。
   没说时间的，业绩类问题默认 today，「一共 / 累计」类问题默认 week。
3. 回答用纯文本，一行一条，不要 markdown 表格、不要 # 和 * 符号，总共不超过 12 行。
   先给结论，再给关键数字。数字为 0 的项可以省略，但被明确问到的要答。
4. 「积分」指价值积分。「待核对」的单是缺字段等人补，计数照常。
5. 如果这条消息不是提问，而是在报告刚办成的业务（晒单 / 报单）、或只是点赞附和闲聊，
   不要回答，只输出 {not_a_question} 这四个字符。
6. 问的事情工具查不到（比如打卡、考勤、政策细节），直说查不到，不要编。

{context}"""

# ---------- 工具定义（OpenAI function calling 格式）----------

_PERIOD_DESC = ("时间范围：today / yesterday / week（本周一到周日）/ last_week / month / last_month / "
                "YYYY-MM-DD（某一天）/ YYYY-MM-DD~YYYY-MM-DD（一段）。写法不对会返回 error，换正确写法重试")

TOOLS = [
    {"type": "function", "function": {
        "name": "team_stats",
        "description": "各战队的汇总：新增宽带、中高套、长续约、千兆、单号卡、云盒、积分、单数、综合进度、排名。问「各战队情况」「谁第一」「排名」用这个。",
        "parameters": {"type": "object", "properties": {
            "period": {"type": "string", "description": _PERIOD_DESC},
        }, "required": ["period"]},
    }},
    {"type": "function", "function": {
        "name": "group_stats",
        "description": "各小组的汇总（每组两个人）。问「哪些组还没晒单」「某战队各组情况」用这个。可按战队过滤。",
        "parameters": {"type": "object", "properties": {
            "period": {"type": "string", "description": _PERIOD_DESC},
            "team": {"type": "string", "description": "只看这个战队，如「东林」；不填看全部"},
            "only_zero": {"type": "boolean", "description": "true 时只返回一单都没有的小组"},
        }, "required": ["period"]},
    }},
    {"type": "function", "function": {
        "name": "person_stats",
        "description": "某个人的业绩：单数、积分、每一单明细，以及他在名册里的小组和战队。问「杨桐今天几单」「某人多少分」用这个。",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "姓名"},
            "period": {"type": "string", "description": _PERIOD_DESC},
        }, "required": ["name", "period"]},
    }},
    {"type": "function", "function": {
        "name": "list_orders",
        "description": "按时间倒序列出清单里的单：时间、类型、套餐、揽收人、战队、小组、积分。问「最近几单」「今天都有哪些单」「东林今天办了什么」用这个。",
        "parameters": {"type": "object", "properties": {
            "period": {"type": "string", "description": _PERIOD_DESC},
            "team": {"type": "string", "description": "只看这个战队；不填看全部"},
            "limit": {"type": "integer", "description": "最多几条，默认 20"},
        }, "required": ["period"]},
    }},
    {"type": "function", "function": {
        "name": "roster",
        "description": "查名册：某人在哪个小组、哪个战队；或某战队有哪些小组和成员；不填 query 返回所有战队。",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "姓名或战队名，如「杨桐」「东林」"},
        }},
    }},
    {"type": "function", "function": {
        "name": "send_bulletin",
        "description": "把业绩通报（文字 + 晒单情况表格图，不含 Excel）发到当前群。用户明确要「发通报」「发一下排名图」时才用。要 Excel 表格的，告诉他单聊机器人说「晒单表格发我」。",
        "parameters": {"type": "object", "properties": {}},
    }},
]


class Assistant:
    """一个提问 -> 工具循环 -> 一段回答。线程安全：每次 ask 独立，不共享状态。"""

    def __init__(self, settings: AssistantSettings, llm_settings, api_key: str,
                 campaign_getter, campaign_settings_getter, bulletin=None) -> None:
        self.s = settings
        self.base_url = (settings.base_url or llm_settings.base_url).rstrip("/")
        self.model = settings.model or llm_settings.model
        self.api_key = api_key
        self._campaign = campaign_getter
        self._camp_cfg = campaign_settings_getter
        self.tools = LedgerTools(campaign_getter, campaign_settings_getter)
        self.bulletin = bulletin
        self._session = requests.Session()
        self._lock = threading.Lock()
        self._window_start = 0.0
        self._calls = 0

    # ---------- 入口 ----------

    def take_budget(self) -> bool:
        limit = self.s.max_calls_per_minute
        if limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            if now - self._window_start >= 60:
                self._window_start, self._calls = now, 0
            if self._calls >= limit:
                return False
            self._calls += 1
            return True

    def ask(self, question: str, chat_id: str, asker: str = "") -> str | None:
        """
        回答一个问题。返回：
          · 回答文本
          · NOT_A_QUESTION —— 模型认为这是报单不是提问，调用方走点赞那条路
          · None —— 出错（网络 / 超轮数 / 解析失败 / 模型返回了畸形结构）
        """
        try:
            return self._ask(question, chat_id, asker)
        except Exception:
            # 模型偶尔返回怪东西（tool_calls 不是列表、function 是 None …），
            # 以前异常穿出去连「我这会儿查不了」都不回（审查踩出来的）
            log.exception("助手回答时出错")
            return None

    def _ask(self, question: str, chat_id: str, asker: str) -> str | None:
        question = redact(question)
        messages = [
            {"role": "system", "content": self._system_prompt()},
            {"role": "user", "content": f"{asker + '：' if asker else ''}{question}"},
        ]
        for _round in range(self.s.max_rounds + 1):
            reply = self._chat(messages)
            if reply is None:
                return None
            calls = reply.get("tool_calls") or []
            content = (reply.get("content") or "").strip()
            if not calls:
                return content or None
            messages.append({"role": "assistant", "content": reply.get("content") or "",
                             "tool_calls": calls})
            for call in calls:
                name = call.get("function", {}).get("name", "")
                try:
                    args = json.loads(call.get("function", {}).get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = self._run_tool(name, args, chat_id)
                log.info("助手工具 %s(%s) -> %d 字", name, json.dumps(args, ensure_ascii=False), len(result))
                messages.append({"role": "tool", "tool_call_id": call.get("id", ""),
                                 "content": result})
        log.warning("助手工具循环超过 %d 轮还没给出回答", self.s.max_rounds)
        return None

    # ---------- 模型 ----------

    def _system_prompt(self) -> str:
        camp_cfg: CampaignSettings = self._camp_cfg()
        camp: Campaign = self._campaign()
        now = datetime.now()
        names = {"day": "天", "week": "周", "fixed": "活动期"}
        return SYSTEM_PROMPT.format(
            now=now.strftime("%Y-%m-%d %H:%M"),
            weekday="周" + "一二三四五六日"[now.weekday()],
            title=camp_cfg.title,
            rank_period=names.get(camp_cfg.rank_period, camp_cfg.rank_period),
            ledger_period=names.get(camp_cfg.ledger_period, camp_cfg.ledger_period),
            teams="、".join(camp.roster.teams) or "（名册为空）",
            not_a_question=NOT_A_QUESTION,
            context=(self.s.context or "").strip(),
        )

    def _chat(self, messages: list) -> dict | None:
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": TOOLS,
            "tool_choice": "auto",
            "temperature": self.s.temperature,
            "max_tokens": self.s.max_tokens,
            "stream": False,
        }
        try:
            resp = self._session.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json"},
                json=payload, timeout=self.s.timeout)
        except requests.RequestException as exc:
            log.warning("助手调用模型失败（网络）：%s", exc)
            return None
        if resp.status_code != 200:
            log.warning("助手调用模型失败 HTTP %s：%s", resp.status_code, resp.text[:200])
            return None
        try:
            return resp.json()["choices"][0]["message"]
        except (KeyError, IndexError, ValueError) as exc:
            log.warning("助手模型响应结构异常：%s", exc)
            return None

    # ---------- 工具 ----------
    # 查数的实现在 ledger_tools.LedgerTools 里，和 MCP 服务器共用，
    # 这里只做「模型给的参数 -> 方法调用」的转接

    def _run_tool(self, name: str, args: dict, chat_id: str) -> str:
        try:
            if name == "send_bulletin":
                result = self._send_bulletin(chat_id)
            else:
                fn = getattr(self.tools, name, None)
                if fn is None or name.startswith("_") or not callable(fn):
                    return json.dumps({"error": f"没有 {name} 这个工具"}, ensure_ascii=False)
                # 只传函数签名里有的参数：模型偶尔会多编一个键，别让它把调用炸掉
                params = inspect.signature(fn).parameters
                result = fn(**{k: v for k, v in (args or {}).items() if k in params})
            return json.dumps(result, ensure_ascii=False)
        except ValueError as exc:
            # 时间范围写歪之类：把原因给模型，它会换写法重试或告诉用户
            log.info("助手工具 %s 参数不对：%s", name, exc)
            return json.dumps({"error": str(exc)}, ensure_ascii=False)
        except Exception as exc:
            log.exception("助手工具 %s 出错", name)
            return json.dumps({"error": f"工具出错：{exc}"}, ensure_ascii=False)

    def _send_bulletin(self, chat_id: str) -> dict:
        # 和群里喊「通报」同一道门：通报开关、60 秒冷却、重置跟单计时
        if self.bulletin is None or not self.bulletin.request(chat_id, "助手「发通报」"):
            return {"error": "通报功能没启用"}
        return {"ok": True, "说明": "通报已开始生成，几秒后会发到本群；一分钟内再要不会重复发"}
