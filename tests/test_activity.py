"""活动切换：命令解析、权限、活动期内外的开关、配置覆盖、名册只给管理员。"""
import io
import json
from datetime import date, datetime, timedelta

from _harness import FakePlatform, check, done, eq, run, setup

tmp = setup()

from botcore import activity  # noqa: E402
from botcore.automation import StickerAutomation  # noqa: E402
from botcore.config import ConfigStore  # noqa: E402
from botcore.message import InboundMessage  # noqa: E402
from botcore.pipeline import Pipeline  # noqa: E402

ORDER = ("支局：新盛支局\n业务类型:新增\n办理套餐：139\n业务号码:{no}\n"
         "价值积分:139\n是否加载云盒：是\n揽收人:郭静")
TODAY = date.today()
store = ConfigStore()
ADMIN = store.backup_settings().admins[0]


def _roster_xlsx() -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "分组"
    ws.append(["姓名", "分组", "战区"])
    for name, info in json.loads((tmp / "roster.json").read_text(encoding="utf-8"))["people"].items():
        ws.append([name, info["group"], info["team"]])
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


def _other_xlsx() -> bytes:
    import openpyxl
    wb = openpyxl.Workbook()
    wb.active.append(["随便", "一张表"])
    b = io.BytesIO()
    wb.save(b)
    return b.getvalue()


plat = FakePlatform(files={"roster": (_roster_xlsx(), "名册.xlsx"), "other": (_other_xlsx(), "报价.xlsx")})
p = Pipeline(plat, store)
_n = [0]


def send(text="", chat="p2p", sender=ADMIN, image=False, files=None) -> str:
    _n[0] += 1
    before = len(plat.replies)
    p._process(InboundMessage(msg_id=f"a{_n[0]}", chat_id=sender if chat == "p2p" else "g", chat_type=chat,
                              text=text, sender_id=sender, image_ids=["img"] if image else [],
                              files=files or [], mentions_bot=True), 0.0)
    return plat.replies[-1] if len(plat.replies) > before else ""


def orders() -> int:
    return len(p.campaign().orders)


def test_parse():
    title, s, e = activity.parse_new("新活动 秋促专项营销活动 10.1-10.4", date(2026, 9, 28))
    eq((title, s, e), ("秋促专项营销活动", date(2026, 10, 1), date(2026, 10, 4)), "标准写法")
    eq(activity.parse_new("新活动 10月1日至10月7日 国庆大促", date(2026, 9, 28))[0], "国庆大促", "名称写在日期后面")
    eq(activity.parse_new("新活动 存量精耕 10.1~4", date(2026, 9, 28))[2], date(2026, 10, 4), "结束只写日")
    eq(activity.parse_new("新活动 双旦 12.30-1.2", date(2026, 9, 28))[2], date(2027, 1, 2), "跨年")
    for bad in ("新活动 9.1-9.4", "新活动 秋促", "新活动 10.1-11.30", "新活动 2.30-3.1"):
        try:
            activity.parse_new(bad, date(2026, 9, 28))
            check(False, f"{bad!r} 应该被拒绝")
        except ValueError:
            check(True, bad)


def test_no_activity_file():
    check(not activity.managed(), "一开始没有 activity.json")
    check(activity.allows(datetime(2026, 10, 1, 10), [4]) and not activity.allows(datetime(2026, 9, 28, 10), [4]),
          "没有活动文件时按星期")
    check("没有设置活动" in send("当前活动"), "当前活动：没设置")


def test_commands_and_gate():
    check("单聊" in send("新活动 秋促 10.1-10.4", chat="group"), "群里切换活动提示去单聊")
    check("只有管理员" in send("新活动 秋促 10.1-10.4", sender="someone"), "非管理员不能切换")
    check("已经过了" in send("新活动 秋促 9.1-9.4"), "过去的日期被拒")
    s, e = TODAY + timedelta(days=3), TODAY + timedelta(days=6)
    r = send(f"新活动 秋促专项营销活动 {s.month}.{s.day}-{e.month}.{e.day}")
    check(r.startswith("已切换") and "还没开始" in r, f"切换到将来的活动：{r}")
    eq(store.campaign_settings().title, "秋促专项营销活动", "活动名盖到 campaign.title")
    eq(store.checkin_settings().span, f"{s.month}.{s.day}-{e.month}.{e.day}", "活动日期盖到打卡表标题")
    check(list((tmp / "backups" / "切换活动前").glob("*.zip")), "切换前打了备份")
    check("还没开始" in send("当前活动", chat="group", sender="someone"), "谁都能看当前活动")

    n = orders()
    r = send(ORDER.format(no="13800000001"), chat="group", sender="u1")
    check("不在活动期间" in r, f"活动前群里晒单回提示：{r}")
    check("不在活动期间" in send(chat="group", sender="u1", image=True), "活动前群里打卡照回提示")
    eq(orders(), n, "活动前晒单不入账")
    now = datetime.now()
    check(not activity.allows(now, [1, 2, 3, 4, 5, 6, 7]), "活动前定时任务不跑")
    check(not StickerAutomation.in_active_window(store.sticker_settings(), now.replace(hour=10)), "活动前表情包不发")

    send(f"新活动 {TODAY.month}.{TODAY.day}-{e.month}.{e.day}")
    eq(store.campaign_settings().title, "秋促专项营销活动", "名称省略时沿用上一个")
    check(activity.allows(now, []), "活动期间定时任务跑（不看星期）")
    send(ORDER.format(no="13800000002"), chat="group", sender="u1")
    eq(orders(), n + 1, "活动期间晒单入账")

    check("只有管理员" in send("结束活动", sender="someone"), "非管理员不能结束")
    check("提前结束" in send("结束活动"), "结束活动")
    check("不用再结束" in send("结束活动"), "再发结束：不用再结束")
    check("不在活动期间" in send(ORDER.format(no="13800000003"), chat="group", sender="u2"), "结束后晒单回提示")
    eq(orders(), n + 1, "结束后不入账")
    hist = json.loads((tmp / "activity.json").read_text(encoding="utf-8"))["history"]
    check(len(hist) == 1, "历史里留了上一个活动")


def test_roster_admin_only():
    check("只能管理员" in send(files=[("roster", "")], sender="someone"), "非管理员发名册被拦")
    eq(send(files=[("other", "")], sender="someone"), "", "非管理员发别的 Excel 不理")
    check(send(files=[("roster", "")]).startswith("名册已更新"), "管理员发名册导入")


run(test_parse)
run(test_no_activity_file)
run(test_commands_and_gate)
run(test_roster_admin_only)
done()
