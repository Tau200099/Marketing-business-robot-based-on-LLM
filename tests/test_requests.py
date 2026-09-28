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
    from botcore import regions
    if not regions.multi_enabled(store):
        _r, files = send("晒单表格发我")
        check(len(files) == 1 and files[0].startswith("晒单"), f"晒单表格：{files}")
        return
    # 多群模式（09-29）：表格只给管理员；按城区 / 农村分，没说就两个区都发
    r, files = send("晒单表格发我")
    check(not files and r and "只有管理员" in r[-1], f"非管理员要表格被拒：{r}")
    _r, files = send("晒单表格发我", sender=ADMIN)
    eq(sorted(f.split("_2")[0] for f in files), ["晒单_农村", "晒单_城区"], "没说区域：城区、农村晒单各一份")
    _r, files = send("城区打卡表格发我", sender=ADMIN)
    eq([f.split("_2")[0] for f in files], ["打卡_城区"], "城区打卡表格")
    _r, files = send("农村晒单表格", sender=ADMIN)
    eq([f.split("_2")[0] for f in files], ["晒单_农村"], "农村晒单表格")
    _r, files = send("表格发我", sender=ADMIN)
    eq(len(files), 4, "「表格发我」：两个区 × 晒单 / 打卡")
    r, files = send("晒单表格发我", chat="group", sender=ADMIN)
    check(not files and r and "单聊" in r[-1], f"群里要表格：不发文件、提示单聊：{r}")
    r, files = send("今天几单", sender=ADMIN)
    eq(files, [], "问业绩不当成要表格")


def test_archive_and_prune():
    from botcore import archive
    y = date.today() - timedelta(days=1)
    paths = archive.archive_day(store, y)
    want = sorted(f"{archive._name(k, r)}_{y}.xlsx" for k, r in archive.specs(store))
    eq(sorted(p.name for p in paths), want, "存档昨天的每一张")
    old = date.today() - timedelta(days=20)
    for k, r in archive.specs(store):
        path = archive.archive_path(k, old, r)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
    n = archive.prune(14)
    eq(n, len(archive.specs(store)), "清掉 14 天前的每一张")
    _r, files = send(f"城区打卡表格 {y.month}.{y.day}", sender=ADMIN)
    eq(files, [f"打卡_城区_{y}.xlsx"] if archive.specs(store)[0][1] else [f"打卡_{y}.xlsx"], "要昨天的打卡表格：给存档那份")


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
