"""单聊要表格 / 要备份：谁能要、群里要怎么回、发的是什么文件；报表存档和清理。"""
from datetime import date, timedelta
from pathlib import Path

from _harness import FakePlatform, check, done, eq, run, setup

tmp = setup()

from botcore.config import ConfigStore  # noqa: E402
from botcore.message import InboundMessage  # noqa: E402
from botcore.pipeline import Pipeline  # noqa: E402

store = ConfigStore()
ADMIN = store.backup_settings().admins[0]
plat = FakePlatform()
p = Pipeline(plat, store)
_n = [0]


def send(text, chat="p2p", sender="YangTong"):
    """返回 (新回复, 新发的文件名)。每次用不同的单聊会话，躲开 30 秒冷却。"""
    _n[0] += 1
    r0, f0 = len(plat.replies), len(plat.sent_files)
    p._process(InboundMessage(msg_id=f"r{_n[0]}", chat_id=f"{sender}-{_n[0]}" if chat == "p2p" else "g",
                              chat_type=chat, text=text, sender_id=sender, mentions_bot=True), 0.0)
    return plat.replies[r0:], [Path(f).name for _c, f in plat.sent_files[f0:]]


def test_excel():
    _r, files = send("晒单表格发我")
    check(len(files) == 1 and files[0].startswith("晒单") and files[0].endswith(".xlsx"), f"晒单表格：{files}")
    _r, files = send("打卡表格发我")
    check(len(files) == 1 and files[0].startswith("打卡"), f"打卡表格：{files}")
    _r, files = send("表格发我")
    eq(len(files), 2, "「表格发我」两份都发")
    r, files = send("晒单表格发我", chat="group")
    check(not files and r and "单聊" in r[-1], f"群里要表格：不发文件、提示单聊：{r}")
    r, files = send("今天几单")
    eq(files, [], "问业绩不当成要表格")


def test_archive_and_prune():
    from botcore import archive
    y = date.today() - timedelta(days=1)
    paths = archive.archive_day(store, y)
    eq(sorted(p.name for p in paths), sorted([f"晒单_{y}.xlsx", f"打卡_{y}.xlsx"]), "存档昨天两份")
    old = date.today() - timedelta(days=20)
    for kind in archive.KINDS:
        (archive.ARCHIVE_DIR / kind / f"{kind}_{old}.xlsx").write_bytes(b"x")
    n = archive.prune(14)
    eq(n, 2, "清掉 14 天前的两份")
    _r, files = send(f"打卡表格 {y.month}.{y.day}")
    eq(files, [f"打卡_{y}.xlsx"], "要昨天的打卡表格：给存档那份")


def test_backup():
    r, files = send("备份")
    check(r and "只发给管理员" in r[-1] and not files, "非管理员要备份被拒")
    r, files = send("备份", sender=ADMIN)
    check(len(files) == 1 and files[0].endswith(".zip"), f"管理员要今天的备份：{files}")
    r, files = send("json", sender=ADMIN)
    check(len(files) == 1 and files[0].endswith(".zip"), "「json」也是要备份")
    r, files = send("备份", chat="group", sender=ADMIN)
    eq(files, [], "群里说备份不发文件")
    import zipfile
    zf = zipfile.ZipFile(tmp / "backups" / "当前" / files_last())
    names = zf.namelist()
    check("config.yaml" in names and ".env" not in names, f"备份里有配置、没有 .env：{names}")


def files_last() -> str:
    return Path(plat.sent_files[-1][1]).name


run(test_excel)
run(test_archive_and_prune)
run(test_backup)
done()
