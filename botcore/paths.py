"""
代码目录和数据目录。

单独放一个没有任何项目内依赖的模块，是为了避免循环导入 ——
config 要导 stickers 的设置类，stickers 又要知道数据目录在哪。

    ROOT  代码所在目录，镜像里是只读的
    DATA  配置、凭证、状态文件、表情包图库

默认 DATA == ROOT，所以本地开发行为完全不变。
Docker 里把 BOT_DATA_DIR 指到挂载卷：代码在镜像里，数据在卷里，
重建镜像不会把统计数据冲掉。
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.getenv("BOT_DATA_DIR", "").strip() or ROOT)
CONFIG_PATH = DATA / "config.yaml"
