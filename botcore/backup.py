"""
数据备份（09-28 用户选的方案：服务器每日快照 + 写之前留底 + 管理员单聊按日期取）。

    每日快照   服务器 cron 每天 02:30 跑 `python -m botcore.backup snapshot`，
               把关键数据打成 data/backups/备份_YYYY-MM-DD.zip，保留 keep_days 天
    写之前留底 台账 / 打卡记录 / 名册每次保存前，把旧版本拷一份到 data/history/<文件名>/，保留最近 20 版。
               防一天之内写坏或误操作，不用等第二天的快照
    单聊取备份 管理员单聊机器人发「备份」「备份 9.25」「昨天的备份」，机器人把那天的备份包发过来；
               要今天的就当场打一个最新的

备份里有：台账 campaign.json、打卡 checkin.json、名册 roster.json、账号对照 userid_map.json、
         其余状态 *.json、config.yaml
不放：.env（API 密钥不能进聊天记录，本机 D:\\feishu_bot\\.env 有一份）、打卡样本照片（有证件，隐私）、
      reports/（随时能重新生成）

恢复：解压到 /opt/feishu-bot/data 覆盖对应文件，systemctl restart feishu-bot。
"""

from __future__ import annotations

import logging
import shutil
import sys
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from .paths import DATA

log = logging.getLogger("botcore.backup")

BACKUP_DIR = DATA / "backups"
HISTORY_DIR = DATA / "history"


@dataclass
class BackupSettings:
    # 能单聊取备份的人（企微账号 ID）。台账是全员业绩明细，不能谁私聊都给
    admins: list = field(default_factory=list)
    # 单聊提醒发给谁（认不出的人晒单、视觉模型额度用完 / 换模型）。和 admins 分开：
    # 管理员以后会多，提醒只发这里列的人（09-29 用户：只发黄涛）。没配才退回发给 admins
    notify: list = field(default_factory=list)
    keep_days: int = 14          # 每日快照留几天
    # 写之前留底固定每个文件留最近 20 版（keep_version 的默认值）


def _files() -> list[Path]:
    """要备份的文件：数据目录下的 *.json + config.yaml。"""
    out = sorted(p for p in DATA.glob("*.json") if p.is_file())
    cfg = DATA / "config.yaml"
    if cfg.is_file():
        out.append(cfg)
    return out


def make_zip(out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for p in _files():
            z.write(p, p.name)
    tmp.replace(out)
    try:
        out.chmod(0o600)
    except Exception:
        pass
    return out


def snapshot(keep_days: int = 14, day: date | None = None) -> Path:
    """每日快照：data/backups/备份_YYYY-MM-DD.zip；删掉 keep_days 天以前的。"""
    day = day or date.today()
    out = make_zip(BACKUP_DIR / f"备份_{day:%Y-%m-%d}.zip")
    cutoff = day - timedelta(days=keep_days)
    for p in BACKUP_DIR.glob("备份_*.zip"):
        try:
            if datetime.strptime(p.stem[3:], "%Y-%m-%d").date() < cutoff:
                p.unlink()
        except ValueError:
            continue
    log.info("每日快照已生成：%s（%d 字节）", out.name, out.stat().st_size)
    return out


def available_days() -> list[str]:
    return sorted(p.stem[3:] for p in BACKUP_DIR.glob("备份_*.zip"))


def archive_for(day: date) -> Path | None:
    """某天的备份包。今天的当场打一个最新的；过去的取当天快照，没有返回 None。"""
    if day >= date.today():
        out = make_zip(BACKUP_DIR / "当前" / f"备份_{datetime.now():%Y-%m-%d_%H%M}.zip")
        for p in sorted((BACKUP_DIR / "当前").glob("备份_*.zip"))[:-5]:   # 当场打的只留最近 5 个
            p.unlink()
        return out
    p = BACKUP_DIR / f"备份_{day:%Y-%m-%d}.zip"
    return p if p.is_file() else None


def keep_version(path: Path, keep: int = 20) -> None:
    """
    写之前留底：把 path 现在的内容拷到 data/history/<文件名>/，只留最近 keep 版。
    出任何错都不影响正常保存（留底是锦上添花，不能反过来把记单弄失败）。
    """
    try:
        path = Path(path)
        if keep <= 0 or not path.is_file():
            return
        d = HISTORY_DIR / path.stem
        d.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, d / f"{path.stem}_{datetime.now():%Y%m%d_%H%M%S_%f}{path.suffix}")
        old = sorted(d.glob(f"{path.stem}_*{path.suffix}"))
        for p in old[:-keep]:
            p.unlink()
    except Exception as exc:
        log.warning("留底失败（不影响保存）：%s", exc)


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(name)s | %(message)s")
    if argv[:1] == ["snapshot"]:
        from .config import ConfigStore
        try:
            keep = ConfigStore().backup_settings().keep_days
        except Exception:
            keep = 14
        snapshot(keep)
        return 0
    print("用法：python -m botcore.backup snapshot")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
