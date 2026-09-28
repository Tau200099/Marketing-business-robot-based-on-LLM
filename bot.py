#!/usr/bin/env python3
"""
入口。

    python bot.py

平台在 config.yaml 的 global.platform 里选（feishu / wecom）。
业务逻辑完全一样，只换适配器。
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time

from botcore.automation import StickerAutomation
from botcore.bulletin import Bulletin
from botcore.checkin_bulletin import CheckinBulletin
from botcore.config import ConfigStore, load_env
from botcore.console import force_utf8
from botcore.pipeline import Pipeline

log = logging.getLogger("botcore")


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-5s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    # SDK 的 DEBUG 日志很吵，除非我们自己也开 DEBUG
    if level != "DEBUG":
        logging.getLogger("lark_oapi").setLevel(logging.WARNING)
        logging.getLogger("urllib3").setLevel(logging.WARNING)
        logging.getLogger("websockets").setLevel(logging.WARNING)
        # ⚠️ 必须压掉：httpx 在 INFO 级别会打完整请求 URL，
        # 而企微的 apikey 就在 URL 的 query 里 —— 那等于每轮轮询
        # 往日志里写一次明文凭证（终端回滚、日志文件、贴给别人看的截图都会带上）。
        # httpx 有些版本的 logger 名带后缀，一并压掉。
        for noisy in ("httpx", "httpx2", "httpcore", "mcp"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        # 飞书长连接的日志器叫「Lark」，INFO 级别会把带 access_key 的
        # 连接 URL 整条打出来，一样压掉
        logging.getLogger("Lark").setLevel(logging.WARNING)


def build_platform(name: str, log_level: str):
    """按名字造平台适配器。加新平台在这里加一个分支。"""
    if name == "feishu":
        from botcore.platforms import feishu
        return feishu.from_env(log_level)
    if name == "wecom":
        from botcore.platforms import wecom
        return wecom.from_env(log_level)
    raise SystemExit(
        f"config.yaml 里 global.platform = {name!r} 不认识，可选：feishu / wecom"
    )


def start_hot_reload(store: ConfigStore, interval: float, stop: threading.Event) -> None:
    if interval <= 0:
        return

    def loop() -> None:
        while not stop.wait(interval):
            try:
                if store.maybe_reload():
                    _settings, matcher = store.snapshot()
                    log.info("config.yaml 已热加载，当前 %d 条规则生效", len(matcher.rules))
            except Exception as exc:
                log.error("config.yaml 解析失败，继续沿用旧配置：%s", exc)

    threading.Thread(target=loop, name="hot-reload", daemon=True).start()


def main() -> None:
    force_utf8()
    log_level = load_env()
    setup_logging(log_level)

    store = ConfigStore()
    settings, matcher = store.snapshot()

    platform = build_platform(settings.platform, log_level)
    pipeline = Pipeline(platform, store)
    pipeline.warmup()

    stop = threading.Event()
    start_hot_reload(store, settings.hot_reload, stop)

    signal_count = {"n": 0}

    def on_signal(*_):
        """
        两段式退出。

        以前这里是「同步做完全部收尾再 sys.exit(0)」，有两个问题：
          · 主线程正卡在 platform.start() 的事件循环里，在这儿 sys.exit 出不去
          · pipeline.shutdown() 会死等在途任务（OCR+大模型可能几十秒）
        表现就是按了 Ctrl+C 没反应，再按还是没反应。

        现在：第一次只【发信号】并起个看门狗，第二次直接硬退。
        """
        signal_count["n"] += 1
        if signal_count["n"] >= 2:
            log.warning("再次收到退出信号，强制退出")
            os._exit(1)

        log.info("收到退出信号，正在关闭……（再按一次 Ctrl+C 强制退出）")
        stop.set()
        # 顺序要紧：先停平台断掉消息来源，再关线程池。
        # 反过来的话，轮询还在往已关闭的线程池投任务，
        # 会刷一串 cannot schedule new futures after shutdown。
        automation.close()
        bulletin.close()
        platform.close()
        pipeline.shutdown()

        # 看门狗：宽限期一过就硬退。线程池的工作线程不是守护线程，
        # 一个跑了一半的 OCR 能把进程拖住不让退，这里兜底。
        def _force_exit():
            time.sleep(8)
            log.warning("收尾超时，强制退出")
            os._exit(1)
        threading.Thread(target=_force_exit, daemon=True).start()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    # Windows 上 Ctrl+Break 走的是另一个信号，一并接管
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, on_signal)

    log.info(
        "平台=%s | %d 条规则 | 群消息=%s 单聊=%s 需要@=%s",
        platform.name, len(matcher.rules),
        settings.enable_group, settings.enable_p2p, settings.require_at,
    )
    # 定时表情包：冷清鼓劲和每日定点。点赞和捷报跟着消息走，不靠它。
    automation = StickerAutomation(platform, store, pipeline._pool)
    pipeline.automation = automation
    automation.start()
    # 业绩通报：定点 + 跟单 + 群里喊「通报」
    bulletin = Bulletin(platform, store, pipeline.campaign, pipeline.pool)
    pipeline.bulletin = bulletin
    bulletin.start()
    # 打卡通报：18:00 前每小时、18:00 后每一个半小时，发「文字 + 打卡表图」；群里喊「打卡通报」立刻发
    checkin_bulletin = CheckinBulletin(platform, store, pipeline.campaign, pipeline.pool)
    checkin_bulletin.recorder = pipeline.checkin
    pipeline.checkin_bulletin = checkin_bulletin
    checkin_bulletin.start()
    # 攻坚群（多群模式）：定点 / 喊「通报」发支局总表 + 支局长打卡点检表；喊「打卡通报」只发点检表
    from botcore.hq import HqPublisher
    hq = HqPublisher(platform, store, pipeline.campaign, getattr(pipeline, "chief_board", None))
    bulletin.hq_publish = hq.publish
    checkin_bulletin.hq_command = hq.publish_chiefs

    # deploy/push.sh 部署时写的版本号（git 提交），看日志就知道服务器跑的是哪一版
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION"), encoding="utf-8") as f:
            log.info("代码版本：%s", f.read().strip())
    except OSError:
        log.info("代码版本：未知（不是 push.sh 部署的）")
    log.info("启动中……（Ctrl+C 退出）")

    platform.start(pipeline.on_message)     # 阻塞


if __name__ == "__main__":
    main()
