"""Windows 控制台默认是 GBK，中文日志会变乱码。统一强制 UTF-8 输出。"""

from __future__ import annotations

import sys


def force_utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
