"""
企业微信适配器 —— 长连接 @ 模式。

收消息走【智能机器人长连接】（官方 aibot SDK，WebSocket）：
    企微只推送【@ 了机器人】的消息，文字 / 图片 / 图文混排 / 文件 / 语音都推，
    带真正的 msgid。普通没 @ 的群消息收不到 —— 这是平台限制，
    所以团队规矩是：晒单一律 @机器人。

为什么不轮询：
    企微把「拉群消息」包成了 MCP 工具（chat_groups_list / chat_messages_list），
    但那两个工具【只对 10 人以下企业开放】，62 人的企业直接 errcode 853006。
    2026-09-03 之前的版本靠轮询，之后就一条都拉不到了。

发消息分两条通道：
    · 文字      MCP 的 message_aibot_send（markdown）。MCP 没配或调用失败时
                退到长连接的 markdown 发送
    · 图片 / 文件  长连接：分片上传拿 media_id（3 天有效）→ send_message。
                长连接上传的 media_id【MCP 通道不认】（errcode 40073），
                所以图片必须用产生它的那条长连接发出去
    · 下载      长连接 download_file(url, aeskey)，本地 AES 解密。照片不出网

⚠️ 长连接给不了发送者姓名，只有 userid。清单里的揽收人以晒单模板的
   「揽收人:」字段为准；点赞里的「@姓名」在企微上退化成不带名字。

⚠️ 异步桥接（改动前先看懂）：
    SDK 是 async 的，Pipeline 是多线程同步的。start() 在主线程跑事件循环，
    长连接住在里面；download_* / send_* 从工作线程被调用，用
    run_coroutine_threadsafe 把协程扔进那个循环，再阻塞等结果。
    不要在工作线程里直接 await，也不要另开循环。

回调帧结构（2026-09 官方文档 + 实测）：
    body: {msgid, aibotid, chattype: "group"|"single", chatid（群聊才有）,
           from: {userid}, msgtype: text|image|mixed|file|voice|video,
           text: {content} | image: {url, aeskey} | file: {url, aeskey, filename}
           | mixed: {msg_item: [{msgtype, text|image}]} | voice: {content}}
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..message import InboundMessage
from ..paths import DATA
from .base import OnMessage

log = logging.getLogger("botcore.wecom")

# 企微要求分片 ≤ 512KB、≤ 100 片。文件最大 20MB、图片 10MB
_CHUNK = 512 * 1024
_MAX_UPLOAD = 20 * 1024 * 1024

# 群里 @机器人 时，文字开头会带「@机器人名 」，去掉才是正文
_LEADING_AT = re.compile(r"^(?:@\S+[   ]*)+")

# MCP 发文字用的工具名：优先 aibot 那个（以机器人身份发、支持 markdown）
_SEND_TOOLS = ("message_aibot_send", "message_send")


# 断线开始时间落盘：看门狗重启进程后，新进程连上时还知道断了多久
_OUTAGE_FILE = DATA / "wecom_outage.json"


def _watchdog_seconds() -> int:
    try:
        return int(os.getenv("WECOM_WATCHDOG_SECONDS", "180").strip() or "180")
    except ValueError:
        return 180


# SDK 打这些日志 = 连接其实已经没了。09-24 实测：19:38 心跳就失败了，SDK 到 20:04 才发「断开」事件，
# 中间 26 分钟机器人一直以为自己在线
_DEAD_SIGNS = re.compile(r"heartbeat|WebSocket not connected|connection closed", re.I)


class _SdkLog:
    """把 aibot SDK 自带的打印接到我们的 logging：非 DEBUG 时只留告警，心跳不刷屏。"""

    def __init__(self, debug: bool, on_dead=None) -> None:
        self._log = logging.getLogger("botcore.wecom.sdk")
        self._debug = debug
        self._on_dead = on_dead

    def _check(self, message) -> None:
        if self._on_dead is not None and _DEAD_SIGNS.search(str(message)):
            try:
                self._on_dead(str(message)[:80])
            except Exception:
                pass

    def debug(self, message, *args):
        if self._debug:
            self._log.debug(str(message), *args)

    def info(self, message, *args):
        if self._debug:
            self._log.info(str(message), *args)

    def warn(self, message, *args):
        self._log.warning(str(message), *args)
        self._check(message)

    def error(self, message, *args):
        self._log.error(str(message), *args)
        self._check(message)


@dataclass
class WecomConfig:
    # 长连接凭证（企微后台 -> 智能机器人 -> BotID / Secret）。收消息、发图、发文件都靠它
    bot_id: str
    bot_secret: str
    # MCP 服务器地址（含 apikey），只用来发 markdown 文字。留空就全走长连接
    mcp_url: str = ""
    # 定时任务（通报、定点表情包）要发到的群。点赞跟着消息走，不看这个
    chat_ids: tuple[str, ...] = ()
    # 单次跨线程调用的超时（秒）。上传一张几百 KB 的图 + 发送，60 秒够
    call_timeout: float = 60.0
    log_level: str = "INFO"


class WecomPlatform:
    name = "wecom"

    def __init__(self, cfg: WecomConfig) -> None:
        self.cfg = cfg
        self._stop = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws = None
        self._ws_ready = threading.Event()
        self._client = None            # MCP 客户端（可选，只发文字）
        self._send_tool: str = ""

    # ---------- 启动 ----------

    def start(self, on_message: OnMessage) -> None:
        """阻塞。事件循环跑在这个线程里，长连接住在里面。"""
        try:
            from aibot import WSClient  # noqa: F401
        except ImportError:
            raise SystemExit("企业微信需要官方 SDK：pip install wecom-aibot-python-sdk（见 requirements.txt）")
        if not (self.cfg.bot_id and self.cfg.bot_secret):
            raise SystemExit("缺少 WECOM_BOT_ID / WECOM_BOT_SECRET（企微后台 -> 智能机器人）")

        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main(on_message))
        finally:
            self._loop.close()

    async def _main(self, on_message: OnMessage) -> None:
        if self.cfg.mcp_url:
            try:
                from mcp import Client
            except ImportError:
                log.warning("没装 mcp 库，文字改走长连接发送")
                await self._serve(on_message)
                return
            try:
                async with Client(self.cfg.mcp_url) as client:
                    self._client = client
                    await self._resolve_send_tool(client)
                    await self._serve(on_message)
            except Exception as exc:
                if self._stop.is_set():
                    return
                log.warning("MCP 服务器连不上（%s），文字改走长连接发送", str(exc)[:120])
                self._client = None
                await self._serve(on_message)
        else:
            log.info("没配 WECOM_MCP_URL，文字走长连接发送")
            await self._serve(on_message)

    async def _resolve_send_tool(self, client) -> None:
        tools = [t.name for t in (await client.list_tools()).tools]
        self._send_tool = next((n for n in _SEND_TOOLS if n in tools), "")
        if self._send_tool:
            log.info("MCP 发文字用工具：%s", self._send_tool)
        else:
            log.warning("MCP 服务器没有发消息的工具（有：%s），文字改走长连接", tools)
            self._client = None

    async def _serve(self, on_message: OnMessage) -> None:
        """建长连接，注册回调，然后一直等到 close()。断线由 SDK 自动重连。"""
        from aibot import WSClient, WSClientOptions

        # max_reconnect_attempts=-1 = 无限重连。默认 10 次，网络抖半小时就放弃了，
        # 之后进程活着但永远收不到消息 —— 服务器上没人盯着日志，这种最要命
        # heartbeat_interval：企微服务端每 5 分钟掐一次连接，而且常常不发关闭帧，
        # SDK 要靠「连续 2 次心跳没回」才发现连接已死。默认 30 秒一跳 = 死了 60~90 秒
        # 才知道，这期间群里 @ 的消息全丢（实测「通报」喊两次只收到一次）。
        # 改成 10 秒一跳，死连接 20~30 秒内就能发现并重连
        client = WSClient(WSClientOptions(bot_id=self.cfg.bot_id, secret=self.cfg.bot_secret,
                                          max_reconnect_attempts=-1,
                                          heartbeat_interval=10000,
                                          logger=_SdkLog(self.cfg.log_level == "DEBUG",
                                                         on_dead=self._mark_offline)))
        authed = asyncio.Event()

        def _on_auth(*_):
            authed.set()
            self._ws_ready.set()
            log.info("企业微信长连接已就绪（@ 模式：只收 @机器人 的消息）")
            # 在事件循环线程里，不能同步发消息（会等自己）—— 另开线程
            threading.Thread(target=self._announce_recovery, name="wecom-recovered", daemon=True).start()

        client.on("authenticated", _on_auth)
        client.on("disconnected", lambda reason=None: self._mark_offline(f"断开：{reason}"))
        client.on("reconnecting", lambda attempt=None: log.info("长连接重连中（第 %s 次）", attempt))
        client.on("error", lambda err=None: log.warning("长连接出错：%s", err))
        # 一个入口接全部消息类型，按 msgtype 自己分
        client.on("message", lambda frame: self._on_frame(frame, on_message))

        await client.connect()
        # 必须等订阅确认再用：连上就发上传命令会被服务器直接断开（errcode 846609）
        try:
            await asyncio.wait_for(authed.wait(), timeout=15)
        except asyncio.TimeoutError:
            log.error("15 秒内没收到企微的认证确认，检查 WECOM_BOT_ID / SECRET")
        self._ws = client
        try:
            offline_since = None
            watchdog = _watchdog_seconds()
            while not self._stop.is_set():
                await asyncio.sleep(0.5)
                if self._ws_ready.is_set():
                    offline_since = None
                    continue
                if offline_since is None:
                    offline_since = asyncio.get_event_loop().time()
                    self._mark_offline("还没连上")
                    continue
                # 看门狗：SDK 自带的重连会卡死不报错（09-24 实测第 1 次重连后再无动静，断了 16 小时）。
                # 断线超过 watchdog 秒还没接上，就退出进程，systemd（Restart=always）10 秒后重新拉起，
                # 从头建一条新连接。WECOM_WATCHDOG_SECONDS=0 可关掉（本机调试时）
                if watchdog and asyncio.get_event_loop().time() - offline_since > watchdog:
                    log.error("⚠️ 长连接断开 %d 秒还没重连上，退出进程，由 systemd 重新拉起", watchdog)
                    logging.shutdown()
                    os._exit(3)
        finally:
            try:
                client.disconnect()
            except Exception:
                pass

    # ---------- 断线 / 恢复 ----------

    def is_ready(self) -> bool:
        """长连接现在能不能收发。定时通报先看这个：断线时别发，也别记成「已发」。"""
        return self._ws_ready.is_set()

    def _mark_offline(self, reason: str) -> None:
        """记下断线：清就绪标记；第一次断线时把开始时间落盘（进程被看门狗重启后还要用）。"""
        if self._ws_ready.is_set():
            log.warning("长连接断了（%s），等重连", reason)
        self._ws_ready.clear()
        try:
            if not _OUTAGE_FILE.exists():
                _OUTAGE_FILE.write_text(json.dumps(
                    {"since": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "reason": reason},
                    ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass

    def _announce_recovery(self) -> None:
        """重新连上后：断线超过 5 分钟就在群里说一声，让大家把这段时间 @ 的晒单、打卡重发。"""
        try:
            raw = json.loads(_OUTAGE_FILE.read_text(encoding="utf-8"))
            _OUTAGE_FILE.unlink()
        except Exception:
            return
        try:
            since = datetime.strptime(raw["since"], "%Y-%m-%d %H:%M:%S")
        except Exception:
            return
        now = datetime.now()
        minutes = int((now - since).total_seconds() // 60)
        log.info("长连接恢复，断了 %d 分钟（%s 起）", minutes, since.strftime("%m-%d %H:%M"))
        if minutes < 5:
            return
        span = (f"{since:%H:%M}–{now:%H:%M}" if since.date() == now.date()
                else f"{since:%m-%d %H:%M} – {now:%m-%d %H:%M}")
        dur = f"{minutes // 60} 小时 {minutes % 60} 分钟" if minutes >= 60 else f"{minutes} 分钟"
        text = f"机器人刚才断线 {dur}（{span}），这段时间 @我 的晒单和打卡请重新发一次"
        time.sleep(3)                      # 等连接稳住再发
        for chat in self.cfg.chat_ids:
            if not self.send_text(chat, text):
                log.error("断线提醒没发出去 -> %s", chat)

    # ---------- 收消息 ----------

    def _on_frame(self, frame: dict, on_message: OnMessage) -> None:
        try:
            inbound = self._to_inbound((frame or {}).get("body") or {})
        except Exception:
            log.exception("转换企微消息时抛异常")
            return
        if inbound is None:
            return
        # on_message 内部只做去重和布尔判断就丢进线程池，不会卡住事件循环
        on_message(inbound)

    def _to_inbound(self, body: dict) -> InboundMessage | None:
        msgtype = str(body.get("msgtype") or "")
        text = ""
        image_ids: list[str] = []
        files: list[tuple[str, str]] = []

        if msgtype == "text":
            text = str((body.get("text") or {}).get("content") or "")
        elif msgtype == "image":
            ref = _media_ref(body.get("image"))
            if ref:
                image_ids.append(ref)
        elif msgtype == "mixed":
            # @ 时带图会包成 mixed：msg_item 里 text 和 image 交错
            for item in (body.get("mixed") or {}).get("msg_item") or []:
                if not isinstance(item, dict):
                    continue
                if item.get("msgtype") == "text":
                    text += str((item.get("text") or {}).get("content") or "")
                elif item.get("msgtype") == "image":
                    ref = _media_ref(item.get("image"))
                    if ref:
                        image_ids.append(ref)
        elif msgtype == "file":
            f = body.get("file") or {}
            ref = _media_ref(f)
            if ref:
                files.append((ref, str(f.get("filename") or f.get("file_name") or "")))
        elif msgtype == "voice":
            # 企微已经把语音转成文字了，当文字处理
            text = str((body.get("voice") or {}).get("content") or "")
        else:
            log.debug("忽略 %s 类型消息", msgtype)
            return None

        text = _LEADING_AT.sub("", text).strip()
        if not text and not image_ids and not files:
            return None

        chattype = str(body.get("chattype") or "group")
        sender_id = str((body.get("from") or {}).get("userid") or "")
        chat_id = str(body.get("chatid") or "") or sender_id
        msgid = str(body.get("msgid") or "")
        if not msgid:
            # 真 msgid 不该缺；缺了就按内容合成一个，别让去重失效
            msgid = "wx_" + hashlib.sha1(f"{chat_id}|{sender_id}|{msgtype}|{text}".encode()).hexdigest()[:20]

        return InboundMessage(
            msg_id=msgid,
            chat_id=chat_id,
            chat_type="p2p" if chattype == "single" else "group",
            text=text,
            image_ids=image_ids,
            files=files,
            sender_id=sender_id,
            sender_name="",                 # 长连接不给姓名
            from_bot=False,
            mentions_bot=True,              # 能推过来的就是 @ 了机器人的
        )

    # ---------- 下载 ----------

    def download_image(self, msg: InboundMessage, image_id: str) -> bytes | None:
        return self._download(image_id, "图片")[0]

    def download_file(self, msg: InboundMessage, file_id: str) -> bytes | None:
        return self._download(file_id, "文件")[0]

    def download_file_named(self, msg: InboundMessage, file_id: str) -> tuple[bytes | None, str]:
        """
        企微推过来的文件消息【不带文件名】，名字只在下载响应头里才有。
        名册更新要靠文件名 / 内容判断是不是 Excel，所以这里连名字一起返回。
        """
        return self._download(file_id, "文件")

    def _download(self, ref: str, what: str) -> tuple[bytes | None, str]:
        """ref 是 _media_ref 编的 JSON：{url, aeskey}。SDK 下载并本地解密，返回 (bytes, 文件名)。"""
        try:
            info = json.loads(ref)
            if not self._ws_ready.wait(timeout=10):
                log.warning("长连接没就绪，%s下载跳过", what)
                return None, ""
            data, name = self._run(self._ws.download_file(info["url"], info.get("aeskey")))
            return data, (name or "")
        except Exception as exc:
            log.warning("下载%s失败：%s", what, exc)
            return None, ""

    # ---------- 发送 ----------

    def reply(self, msg: InboundMessage, text: str, at_user: str | None = None) -> bool:
        """
        往机器人所在会话发消息。企微没有「引用回复」，只能往群里发新消息。

        ⚠️ 这套接口【不支持真 @】：markdown 里写 <@userid> 会原样显示（09-24 长连接也实测过，同样不行）。
        以前靠轮询拿到的姓名做文字 @，长连接不给姓名，所以有名字才带。
        """
        name = (msg.sender_name or "").strip()
        if at_user and name:
            text = f"@{name} {text}"
        return self.send_text(msg.chat_id, text)

    def send_text(self, chat_id: str, text: str) -> bool:
        """文字：MCP markdown 优先，失败退长连接 markdown。"""
        if self._client is not None and self._send_tool:
            try:
                res = self._run(self._client.call_tool(self._send_tool, {
                    "chat_id": chat_id, "msg_type": "markdown",
                    "markdown": {"content": text}}))
                _checked(_unwrap(res), self._send_tool)
                return True
            except Exception as exc:
                if "853008" in str(exc):
                    log.error("发不出去：这个群还不是机器人的「最近会话」，让群里任意成员 @ 一下机器人即可")
                    return False
                log.warning("MCP 发文字失败（%s），改走长连接", str(exc)[:120])
        return self._ws_send(chat_id, {"msgtype": "markdown", "markdown": {"content": text}}, "文字")

    def send_image(self, chat_id: str, data: bytes, filename: str = "image.png") -> bool:
        try:
            media_id = self._run(self._upload_media(data, filename, "image"))
        except Exception as exc:
            log.error("上传图片 %s 失败：%s", filename, exc)
            return False
        return self._ws_send(chat_id, {"msgtype": "image", "image": {"media_id": media_id}}, "图片")

    def send_file(self, chat_id: str, path: Path) -> bool:
        path = Path(path)
        try:
            media_id = self._run(self._upload_media(path.read_bytes(), path.name, "file"))
        except Exception as exc:
            log.error("上传文件 %s 失败：%s", path.name, exc)
            return False
        return self._ws_send(chat_id, {"msgtype": "file", "file": {"media_id": media_id}}, "文件")

    def send_stickers(self, chat_id: str, pool, category: str, count: int = 1) -> int:
        """
        从某个分类挑 count 张发出去，返回实际发成功的张数。从工作线程调用。
        每张图是一条独立消息 —— 企微没有「一条消息多张图」。
        """
        if not self._ws_ready.wait(timeout=10):
            log.warning("长连接没就绪，表情包跳过")
            return 0
        sent = 0
        for path in pool.pick(category, count):
            if self._send_one_sticker(chat_id, pool, path):
                sent += 1
        return sent

    def _send_one_sticker(self, chat_id: str, pool, path: Path) -> bool:
        """
        media_id 有效期 3 天，缓存里有就直接用，没有或过期就现传。
        发送被拒（40073 media_id 非法）时清掉缓存重传一次 —— 覆盖
        「缓存还没到期但服务端已经失效」这种情况。
        """
        for attempt in (1, 2):
            media_id = pool.cached_media_id(path)
            try:
                if media_id is None:
                    media_id = self._run(self._upload_media(path.read_bytes(), path.name, "image"))
                    pool.remember(path, media_id)
                    log.info("表情包 %s 已上传", path.name)
                res = self._run(self._ws.send_message(
                    chat_id, {"msgtype": "image", "image": {"media_id": media_id}}))
                code = _frame_code(res)
                if code in (None, 0):
                    log.info("已发表情包 %s", path.name)
                    return True
                if code == 40073 and attempt == 1:
                    log.info("表情包 %s 的 media_id 失效了，重传一次", path.name)
                    pool.forget(path)
                    continue
                log.warning("表情包发送失败 errcode=%s errmsg=%s", code, _frame_msg(res))
                return False
            except Exception as exc:
                log.warning("表情包处理出错 %s：%s", path.name, exc)
                return False
        return False

    def _ws_send(self, chat_id: str, body: dict, what: str) -> bool:
        if not self._ws_ready.wait(timeout=10):
            log.error("长连接没就绪，%s发不出去", what)
            return False
        try:
            res = self._run(self._ws.send_message(chat_id, body))
        except Exception as exc:
            log.error("长连接发%s失败：%s", what, exc)
            return False
        code = _frame_code(res)
        if code in (None, 0):
            return True
        log.error("发%s失败 errcode=%s errmsg=%s", what, code, _frame_msg(res))
        return False

    async def _upload_media(self, data: bytes, filename: str, media_type: str) -> str:
        """分片上传（≤512KB/片），返回 media_id。media_type: image / file / voice / video。"""
        from aibot.utils import generate_req_id

        if len(data) > _MAX_UPLOAD:
            raise RuntimeError(f"{filename} 有 {len(data) // 1024} KB，超过企微 20MB 上限")
        mgr = self._ws._ws_manager
        rid = lambda: generate_req_id("aibot_upload_media")  # noqa: E731
        chunks = [data[i:i + _CHUNK] for i in range(0, len(data), _CHUNK)] or [b""]

        def unwrap(frame, what):
            code = _frame_code(frame)
            if code not in (None, 0):
                raise RuntimeError(f"{what} errcode={code} errmsg={_frame_msg(frame)}")
            return (frame or {}).get("body") or {}

        body = unwrap(await mgr.send_reply(rid(), {
            "type": media_type, "filename": filename, "total_size": len(data),
            "total_chunks": len(chunks), "md5": hashlib.md5(data).hexdigest(),
        }, cmd="aibot_upload_media_init"), "上传初始化")
        upload_id = body["upload_id"]
        for i, chunk in enumerate(chunks):
            unwrap(await mgr.send_reply(rid(), {
                "upload_id": upload_id, "chunk_index": i,
                "base64_data": base64.b64encode(chunk).decode(),
            }, cmd="aibot_upload_media_chunk"), f"上传分片 {i + 1}/{len(chunks)}")
        return unwrap(await mgr.send_reply(rid(), {"upload_id": upload_id},
                                           cmd="aibot_upload_media_finish"), "上传收尾")["media_id"]

    # ---------- 桥接 / 收尾 ----------

    def _run(self, coro):
        """把协程投进 start() 那个事件循环并等结果。"""
        if self._loop is None or self._loop.is_closed() or self._stop.is_set():
            coro.close()
            raise RuntimeError("事件循环不可用")
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(
            timeout=self.cfg.call_timeout)

    def close(self) -> None:
        self._stop.set()
        self._ws_ready.clear()


# ---------- 工具函数 ----------

def _media_ref(obj) -> str:
    """把 {url, aeskey} 编成一个字符串当媒体标识，下载时再解开。"""
    if not isinstance(obj, dict) or not obj.get("url"):
        return ""
    return json.dumps({"url": obj["url"], "aeskey": obj.get("aeskey") or ""})


def _frame_code(frame):
    if isinstance(frame, dict):
        code = frame.get("errcode")
        if code is None:
            code = ((frame.get("body") or {}) if isinstance(frame.get("body"), dict) else {}).get("errcode")
        return code
    return None


def _frame_msg(frame) -> str:
    if isinstance(frame, dict):
        return str(frame.get("errmsg") or ((frame.get("body") or {}) if isinstance(frame.get("body"), dict) else {}).get("errmsg") or "")
    return ""


def _unwrap(result) -> dict:
    """MCP 的返回包了一层 content，把里面的 JSON 取出来。"""
    for item in getattr(result, "content", []) or []:
        text = getattr(item, "text", None)
        if text:
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"_raw_text": text}
    return getattr(result, "structuredContent", None) or {}


def _checked(data: dict, what: str) -> dict:
    """errcode 非 0 就抛出去。不检查的话会静默返回空，最难查的那种。"""
    if isinstance(data, dict):
        code = data.get("errcode")
        if code not in (None, 0):
            raise RuntimeError(f"{what} 返回 errcode={code} errmsg={data.get('errmsg') or '(空)'}")
    return data if isinstance(data, dict) else {}


def from_env(log_level: str = "INFO") -> WecomPlatform:
    """按 .env 里的 WECOM_BOT_ID / WECOM_BOT_SECRET（必填）、WECOM_MCP_URL / WECOM_CHAT_IDS（可选）构造。"""
    bot_id = os.getenv("WECOM_BOT_ID", "").strip()
    bot_secret = os.getenv("WECOM_BOT_SECRET", "").strip()
    if not bot_id or not bot_secret:
        raise SystemExit(
            "缺少 WECOM_BOT_ID / WECOM_BOT_SECRET。\n"
            "企业微信后台 -> 智能机器人 -> 你的机器人 -> BotID 和 Secret，填进 .env"
        )
    chat_ids = tuple(x.strip() for x in os.getenv("WECOM_CHAT_IDS", "").split(",") if x.strip())
    return WecomPlatform(WecomConfig(
        bot_id=bot_id,
        bot_secret=bot_secret,
        mcp_url=os.getenv("WECOM_MCP_URL", "").strip(),
        chat_ids=chat_ids,
        log_level=log_level,
    ))
