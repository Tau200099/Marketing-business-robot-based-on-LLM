"""
飞书适配器。

用 WebSocket 长连接接收事件：不需要公网 IP、不需要内网穿透、不需要配回调地址，
本地起进程就能收消息。

飞书这条路的能力比企业微信宽松得多：
  · 群里【所有人】的消息都能收到（需要 im:message.group_msg 权限）
  · 图片消息直接推过来，能拿到 image_key 下载原图
  · 文件消息也推，能下载 —— 群里传名册 Excel 靠这个
  · 事件里带发送者 open_id，能真的 @ 人
  · 图片 / 文件上传后拿到的 key 长期有效，不像企微 media_id 三天过期

事件里【没有发送者姓名】，只有 open_id。姓名要另查：
    先查群成员列表（im:chat:readonly），机器人在群里就能查；
    查不到再退到通讯录接口（contact:user.base:readonly）。
    结果按群缓存，一个群只查一次，后来的人再补查。
姓名是清单里「揽收人」的兜底来源（文字晒单没写揽收人、或图片单），
拿不到也不致命，只是那单要人工补。

需要的权限（开放平台 -> 权限管理）：
    im:message                      发消息
    im:message.group_msg            收群里所有消息（要审批）
    im:message:send_as_bot          以机器人身份发
    im:resource                     下载消息里的图片 / 文件
    im:chat:readonly                查群成员姓名
    contact:user.base:readonly      （可选）群成员查不到时退到通讯录
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import lark_oapi as lark
from lark_oapi.api.im.v1 import (
    CreateFileRequest,
    CreateFileRequestBody,
    CreateImageRequest,
    CreateImageRequestBody,
    CreateMessageRequest,
    CreateMessageRequestBody,
    GetChatMembersRequest,
    GetMessageResourceRequest,
    P2ImMessageReceiveV1,
    ReplyMessageRequest,
    ReplyMessageRequestBody,
)

from ..message import InboundMessage
from ..platforms.base import OnMessage

log = logging.getLogger("botcore.feishu")

# 消息里被 @ 的人会渲染成 @_user_1 / @_all 占位符，匹配前剔掉，
# 否则「在吗」这类短关键词容易被占位符干扰。
_AT_PLACEHOLDER = re.compile(r"@_(?:user_\d+|all)\b")

# 飞书上传文件时要声明类型。xlsx 归到 xls；不认识的一律 stream。
_FILE_TYPES = {
    ".xls": "xls", ".xlsx": "xls", ".doc": "doc", ".docx": "doc",
    ".ppt": "ppt", ".pptx": "ppt", ".pdf": "pdf", ".mp4": "mp4", ".opus": "opus",
}

# 同一个群的成员列表多久内不重复拉。新人进群后最多这么久才认得出名字。
_MEMBER_REFRESH_SECONDS = 60.0


@dataclass
class FeishuConfig:
    app_id: str
    app_secret: str
    # 定时任务（通报、定点表情包）要发到的群。点赞跟着消息走，不看这个。
    chat_ids: tuple[str, ...] = ()
    log_level: str = "INFO"


class FeishuPlatform:
    name = "feishu"

    def __init__(self, cfg: FeishuConfig) -> None:
        self.cfg = cfg
        self.app_id = cfg.app_id
        self.app_secret = cfg.app_secret
        self.log_level = cfg.log_level
        self.client = (lark.Client.builder()
                       .app_id(cfg.app_id).app_secret(cfg.app_secret).build())
        self.bot_open_id: str | None = None
        self._ws = None
        # chat_id -> {open_id: 姓名}；以及每个群上次拉成员的时刻
        self._names: dict[str, dict[str, str]] = {}
        self._names_at: dict[str, float] = {}
        self._names_lock = threading.Lock()

    # ---------- 启动 ----------

    def resolve_identity(self) -> None:
        """取机器人自己的 open_id，用于判断「是否被 @」和防自问自答。"""
        try:
            req = (
                lark.BaseRequest.builder()
                .http_method(lark.HttpMethod.GET)
                .uri("/open-apis/bot/v3/info")
                .token_types({lark.AccessTokenType.TENANT})
                .build()
            )
            resp = self.client.request(req)
            body = json.loads(resp.raw.content.decode("utf-8"))
            self.bot_open_id = (body.get("bot") or {}).get("open_id")
        except Exception as exc:                      # 拿不到不致命，降级即可
            log.warning("获取机器人信息失败（不影响运行）：%s", exc)

        if self.bot_open_id:
            log.info("机器人 open_id = %s", self.bot_open_id)
        else:
            log.warning(
                "未取得机器人 open_id：require_at 将无法精确判断被 @。"
                "若应用只有 im:message.group_at_msg:readonly 权限，收到的本来就只有 @ 消息，可忽略。"
            )

    def start(self, on_message: OnMessage) -> None:
        self.resolve_identity()

        def _handle(data: P2ImMessageReceiveV1) -> None:
            try:
                on_message(self._to_inbound(data))
            except Exception:
                log.exception("转换飞书消息时抛异常")

        dispatcher = (
            lark.EventDispatcherHandler.builder("", "")   # 长连接模式下这两个参数留空
            .register_p2_im_message_receive_v1(_handle)
            .build()
        )

        log.info("正在建立长连接……")
        # SDK 会按这个参数重设它自己的日志级别。INFO 会把带 access_key 的
        # 连接 URL 整条打出来，所以非 DEBUG 时只留 WARNING（断线重连仍会打）
        self._ws = lark.ws.Client(
            self.app_id, self.app_secret, event_handler=dispatcher,
            log_level=lark.LogLevel.DEBUG if self.log_level == "DEBUG" else lark.LogLevel.WARNING,
        )
        self._ws.start()          # 阻塞；断线由 SDK 自动重连

    # ---------- 消息转换 ----------

    def _to_inbound(self, data: P2ImMessageReceiveV1) -> InboundMessage:
        event = data.event
        msg = event.message
        sender_id = self._sender_open_id(event)
        chat_id = msg.chat_id or ""

        sender_type = getattr(getattr(event, "sender", None), "sender_type", None)
        from_bot = bool(
            (sender_type and sender_type != "user")
            or (sender_id and sender_id == self.bot_open_id)
        )

        # 姓名要另查，只对真人查。机器人 / 应用发的没必要，也查不到
        sender_name = ""
        if sender_id and not from_bot and msg.chat_type == "group":
            sender_name = self._name_of(chat_id, sender_id)

        return InboundMessage(
            msg_id=msg.message_id,
            chat_id=chat_id,
            chat_type=msg.chat_type or "group",
            text=_extract_text(msg.message_type, msg.content),
            image_ids=_extract_image_keys(msg.message_type, msg.content),
            files=_extract_files(msg.message_type, msg.content),
            sender_id=sender_id or "",
            sender_name=sender_name,
            from_bot=from_bot,
            mentions_bot=self._is_at_bot(msg),
        )

    def _is_at_bot(self, msg) -> bool:
        mentions = getattr(msg, "mentions", None) or []
        if not mentions:
            return False
        if not self.bot_open_id:
            # 没拿到自身身份，只能认「有人被 @ 了」，宽松放行
            return True
        return any(
            getattr(getattr(m, "id", None), "open_id", None) == self.bot_open_id
            for m in mentions
        )

    @staticmethod
    def _sender_open_id(event) -> str | None:
        sender_id = getattr(getattr(event, "sender", None), "sender_id", None)
        return getattr(sender_id, "open_id", None)

    # ---------- 姓名 ----------

    def _name_of(self, chat_id: str, open_id: str) -> str:
        """
        open_id -> 姓名。先看缓存，缓存没有就重拉一次群成员（限频）。

        查不到返回空串，业务层会退化成「揽收人待补」。
        """
        with self._names_lock:
            name = self._names.get(chat_id, {}).get(open_id)
            if name:
                return name
            stale = time.time() - self._names_at.get(chat_id, 0) > _MEMBER_REFRESH_SECONDS
        if stale:
            self._refresh_members(chat_id)
            with self._names_lock:
                name = self._names.get(chat_id, {}).get(open_id)
                if name:
                    return name
        # 群成员里没有（或没权限），退到通讯录
        name = self._contact_name(open_id)
        if name:
            with self._names_lock:
                self._names.setdefault(chat_id, {})[open_id] = name
        return name

    def _refresh_members(self, chat_id: str) -> None:
        found: dict[str, str] = {}
        page_token = None
        try:
            while True:
                b = (GetChatMembersRequest.builder()
                     .chat_id(chat_id).member_id_type("open_id").page_size(100))
                if page_token:
                    b = b.page_token(page_token)
                resp = self.client.im.v1.chat_members.get(b.build())
                if not resp.success():
                    log.warning("拉群成员失败 code=%s msg=%s（揽收人只能从消息文字里取）",
                                resp.code, resp.msg)
                    break
                for m in (resp.data.items or []):
                    if m.member_id and m.name:
                        found[m.member_id] = m.name
                if not resp.data.has_more or not resp.data.page_token:
                    break
                page_token = resp.data.page_token
        except Exception as exc:
            log.warning("拉群成员出错：%s", exc)
        with self._names_lock:
            self._names.setdefault(chat_id, {}).update(found)
            self._names_at[chat_id] = time.time()
        if found:
            log.debug("群 %s 成员 %d 人", chat_id, len(found))

    def _contact_name(self, open_id: str) -> str:
        try:
            from lark_oapi.api.contact.v3 import GetUserRequest
            req = (GetUserRequest.builder()
                   .user_id(open_id).user_id_type("open_id").build())
            resp = self.client.contact.v3.user.get(req)
            if resp.success() and resp.data and resp.data.user:
                return resp.data.user.name or ""
            log.debug("通讯录查不到 %s：code=%s msg=%s", open_id, resp.code, resp.msg)
        except Exception as exc:
            log.debug("通讯录查询出错：%s", exc)
        return ""

    # ---------- 下载 ----------

    def download_image(self, msg: InboundMessage, image_id: str) -> bytes | None:
        return self._download(msg, image_id, "image")

    def download_file(self, msg: InboundMessage, file_id: str) -> bytes | None:
        return self._download(msg, file_id, "file")

    def _download(self, msg: InboundMessage, key: str, kind: str) -> bytes | None:
        req = (
            GetMessageResourceRequest.builder()
            .message_id(msg.msg_id)
            .file_key(key)
            .type(kind)
            .build()
        )
        resp = self.client.im.v1.message_resource.get(req)
        if not resp.success():
            log.error("下载%s失败 code=%s msg=%s log_id=%s",
                      "图片" if kind == "image" else "文件",
                      resp.code, resp.msg, resp.get_log_id())
            return None
        return resp.file.read()

    # ---------- 发送 ----------

    def reply(self, msg: InboundMessage, text: str, at_user: str | None = None) -> bool:
        """引用回复，挂在原消息下面。at_user 非空时在开头 @ 他。"""
        if at_user:
            text = f'<at user_id="{at_user}"></at> {text}'

        req = (
            ReplyMessageRequest.builder()
            .message_id(msg.msg_id)
            .request_body(
                ReplyMessageRequestBody.builder()
                .msg_type("text")
                .content(json.dumps({"text": text}, ensure_ascii=False))
                .reply_in_thread(False)   # True 则开话题串，群里会折叠
                .build()
            )
            .build()
        )

        resp = self.client.im.v1.message.reply(req)
        if not resp.success():
            log.error("回复失败 code=%s msg=%s log_id=%s",
                      resp.code, resp.msg, resp.get_log_id())
            return False
        return True

    def send_text(self, chat_id: str, text: str) -> bool:
        return self._send(chat_id, "text", {"text": text})

    def send_image(self, chat_id: str, data: bytes, filename: str = "image.png") -> bool:
        try:
            key = self._upload_image(data)
        except Exception as exc:
            log.error("上传图片 %s 失败：%s", filename, exc)
            return False
        return self._send(chat_id, "image", {"image_key": key})

    def send_file(self, chat_id: str, path: Path) -> bool:
        path = Path(path)
        try:
            key = self._upload_file(path)
        except Exception as exc:
            log.error("上传文件 %s 失败：%s", path.name, exc)
            return False
        return self._send(chat_id, "file", {"file_key": key})

    def send_stickers(self, chat_id: str, pool, category: str, count: int = 1) -> int:
        """
        从某个分类挑 count 张发出去，返回实际发成功的张数。

        image_key 长期有效，但仍复用 StickerPool 的缓存 —— 省一次上传。
        发送被拒时清掉缓存重传一次，覆盖「缓存的 key 是另一个平台的」
        （企微和飞书共用一个 cache 文件时切换平台会遇到）。
        """
        sent = 0
        for path in pool.pick(category, count):
            if self._send_one_sticker(chat_id, pool, path):
                sent += 1
        return sent

    def _send_one_sticker(self, chat_id: str, pool, path: Path) -> bool:
        for attempt in (1, 2):
            key = pool.cached_media_id(path)
            try:
                if key is None:
                    key = self._upload_image(path.read_bytes())
                    pool.remember(path, key)
                    log.info("表情包 %s 已上传", path.name)
                if self._send(chat_id, "image", {"image_key": key}):
                    log.info("已发表情包 %s", path.name)
                    return True
                if attempt == 1:
                    log.info("表情包 %s 的 image_key 发不出去，重传一次", path.name)
                    pool.forget(path)
                    continue
                return False
            except Exception as exc:
                log.warning("表情包处理出错 %s：%s", path.name, exc)
                return False
        return False

    def _send(self, chat_id: str, msg_type: str, content: dict) -> bool:
        req = (
            CreateMessageRequest.builder()
            .receive_id_type("chat_id")
            .request_body(
                CreateMessageRequestBody.builder()
                .receive_id(chat_id)
                .msg_type(msg_type)
                .content(json.dumps(content, ensure_ascii=False))
                .build()
            )
            .build()
        )
        resp = self.client.im.v1.message.create(req)
        if not resp.success():
            log.error("发送 %s 失败 code=%s msg=%s log_id=%s",
                      msg_type, resp.code, resp.msg, resp.get_log_id())
            return False
        return True

    def _upload_image(self, data: bytes) -> str:
        req = (
            CreateImageRequest.builder()
            .request_body(
                CreateImageRequestBody.builder()
                .image_type("message")
                .image(io.BytesIO(data))
                .build()
            )
            .build()
        )
        resp = self.client.im.v1.image.create(req)
        if not resp.success() or not resp.data or not resp.data.image_key:
            raise RuntimeError(f"code={resp.code} msg={resp.msg} log_id={resp.get_log_id()}")
        return resp.data.image_key

    def _upload_file(self, path: Path) -> str:
        with path.open("rb") as f:
            req = (
                CreateFileRequest.builder()
                .request_body(
                    CreateFileRequestBody.builder()
                    .file_type(_FILE_TYPES.get(path.suffix.lower(), "stream"))
                    .file_name(path.name)
                    .file(f)
                    .build()
                )
                .build()
            )
            resp = self.client.im.v1.file.create(req)
        if not resp.success() or not resp.data or not resp.data.file_key:
            raise RuntimeError(f"code={resp.code} msg={resp.msg} log_id={resp.get_log_id()}")
        return resp.data.file_key

    def close(self) -> None:
        pass


# ---------- 飞书消息体解析 ----------

def _load(content) -> dict:
    try:
        data = json.loads(content) if isinstance(content, str) else (content or {})
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _extract_text(msg_type: str, content: str) -> str:
    """把 text / post 各类消息体拍平成纯文本。图片走 image_ids。"""
    data = _load(content)
    if msg_type == "text":
        return _clean(data.get("text", ""))
    if msg_type == "post":
        return _clean(_flatten_post(data))
    return ""


def _extract_image_keys(msg_type: str, content: str) -> list[str]:
    data = _load(content)
    if msg_type == "image":
        key = data.get("image_key")
        return [key] if key else []

    if msg_type == "post":
        return [
            el["image_key"]
            for line in _post_lines(data)
            for el in line
            if isinstance(el, dict) and el.get("tag") == "img" and el.get("image_key")
        ]
    return []


def _extract_files(msg_type: str, content: str) -> list[tuple[str, str]]:
    """文件消息：{"file_key": "...", "file_name": "xxx.xlsx"}。"""
    if msg_type != "file":
        return []
    data = _load(content)
    key = data.get("file_key")
    return [(key, data.get("file_name") or "")] if key else []


def _post_lines(data: dict) -> list[list]:
    """post 结构在不同场景下有两种形态，统一成 [[element, ...], ...]。"""
    content = data.get("content")
    if isinstance(content, list):
        return [line for line in content if isinstance(line, list)]
    # 带语言分组的形态: {"zh_cn": {"title": ..., "content": [[...]]}}
    for value in data.values():
        if isinstance(value, dict) and isinstance(value.get("content"), list):
            return [line for line in value["content"] if isinstance(line, list)]
    return []


def _flatten_post(data: dict) -> str:
    parts: list[str] = []
    title = data.get("title")
    if isinstance(title, str) and title:
        parts.append(title)
    for line in _post_lines(data):
        for el in line:
            if isinstance(el, dict) and el.get("tag") in ("text", "a", "md"):
                parts.append(str(el.get("text", "")))
    return " ".join(p for p in parts if p)


def _clean(text: str) -> str:
    return _AT_PLACEHOLDER.sub(" ", text or "").strip()


def from_env(log_level: str = "INFO") -> FeishuPlatform:
    """按 .env 里的 FEISHU_APP_ID / FEISHU_APP_SECRET / FEISHU_CHAT_IDS 构造。"""
    app_id = os.getenv("FEISHU_APP_ID", "").strip()
    app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
    if not app_id or not app_secret:
        raise SystemExit(
            "缺少飞书凭证：请把 .env.example 复制为 .env，"
            "并填入 FEISHU_APP_ID / FEISHU_APP_SECRET。\n"
            "凭证在 https://open.feishu.cn/app 里你的自建应用 -> 凭证与基础信息。"
        )
    chat_ids = tuple(x.strip() for x in os.getenv("FEISHU_CHAT_IDS", "").split(",")
                     if x.strip())
    return FeishuPlatform(FeishuConfig(app_id, app_secret, chat_ids, log_level))
