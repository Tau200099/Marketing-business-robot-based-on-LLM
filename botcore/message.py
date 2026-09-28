"""
平台无关的消息模型。

飞书和企业微信的消息结构完全不同（飞书是事件推送的 JSON，企微是轮询拉回来的
记录），但业务流程只关心这么几件事：谁发的、发了什么文字、带了几张图。
适配器负责把各自平台的原始结构翻译成这个模型，业务层就不用知道底下是谁。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class InboundMessage:
    # ---- 必需 ----
    msg_id: str                      # 平台的消息 ID，用于去重和回复定位
    chat_id: str                     # 会话 ID（群 ID 或单聊 ID）
    chat_type: str = "group"         # "group" | "p2p"

    # ---- 内容 ----
    text: str = ""                   # 纯文本部分，没有就是空串
    image_ids: list[str] = field(default_factory=list)   # 平台的图片标识
    # 文件消息：[(平台的文件标识, 文件名), ...]。
    # 目前只有一个用途：群里上传名册 Excel，机器人据此更新分组。
    files: list[tuple[str, str]] = field(default_factory=list)

    # ---- 发送者 ----
    # 两个都可能为空：企业微信的拉取接口不一定返回发送者身份。
    # 空的时候业务层会退化成「不 @ 人」，其余功能照常。
    sender_id: str = ""              # 平台用户 ID，能 @ 人就靠它
    sender_name: str = ""            # 显示名，@ 不了的时候至少能在文字里带上

    # ---- 判断用 ----
    from_bot: bool = False           # 机器人/应用发的，防自问自答死循环
    mentions_bot: bool = False       # 有没有 @ 机器人

    @property
    def has_image(self) -> bool:
        return bool(self.image_ids)

    @property
    def has_file(self) -> bool:
        return bool(self.files)
