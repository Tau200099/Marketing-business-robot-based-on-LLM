"""
平台适配器接口。

业务层（pipeline.py）只依赖这几个方法，不碰任何平台 SDK。
加一个新平台 = 实现这个接口，业务代码一行不用改。

两种平台的驱动方式不一样，都用 start() 统一：
    飞书    WebSocket 长连接，事件到达时回调 on_message
    企业微信 定时轮询拉取，拉到新消息时回调 on_message
对业务层来说没区别，都是「有新消息就调我」。

方法分两档：
    必需  start / download_image / reply / close —— 点赞主流程只靠这四个
    可选  send_text / send_image / send_file / send_stickers / download_file
          —— 通报、表情包、名册更新用。业务层用 hasattr 判断，
          没实现就跳过对应功能，不会崩。

定时任务（通报、定点表情包）没有「触发它的那条消息」，群要显式配：
适配器暴露 cfg.chat_ids（从 .env 的 FEISHU_CHAT_IDS / WECOM_CHAT_IDS 读）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Protocol

from ..message import InboundMessage

OnMessage = Callable[[InboundMessage], None]


class Platform(Protocol):
    name: str

    def start(self, on_message: OnMessage) -> None:
        """
        开始接收消息。阻塞运行，直到进程退出。
        每收到一条消息就调用一次 on_message。
        """
        ...

    def download_image(self, msg: InboundMessage, image_id: str) -> bytes | None:
        """按平台的图片标识下载原图。失败返回 None。"""
        ...

    def reply(self, msg: InboundMessage, text: str, at_user: str | None = None) -> bool:
        """
        回复一条消息。at_user 非空时 @ 对应的人。

        平台不支持 @（比如企微拉取模式拿不到发送者 ID）时，实现方
        应当忽略 at_user 照常发送，而不是失败 —— 少个 @ 不该导致不回复。
        """
        ...

    def close(self) -> None:
        """收尾。"""
        ...

    # ---------- 以下可选 ----------

    def send_text(self, chat_id: str, text: str) -> bool:
        """往群里发一条文字，不针对任何消息。定时通报用。"""
        ...

    def send_image(self, chat_id: str, data: bytes, filename: str = "image.png") -> bool:
        """往群里发一张图（字节）。通报图片用。"""
        ...

    def send_file(self, chat_id: str, path: Path) -> bool:
        """往群里发一个文件。Excel 清单用。"""
        ...

    def download_file(self, msg: InboundMessage, file_id: str) -> bytes | None:
        """下载消息里的附件。名册 Excel 用。"""
        ...
