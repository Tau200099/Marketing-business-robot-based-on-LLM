"""
拼音撞车按支局认人（09-30 用户：只在撞车时用，原逻辑不动）。
名单照 09-29 全员表里真实撞车的几组人：张斌（打通）/ 张彬（永新活动、隆盛渠道）、杨晓峰（赶水）/ 杨晓锋（万盛）、
刘军（打通）/ 柳军（古南）、两个杨鹏（东林 / 安稳）。
"""
import json

from _harness import FakePlatform, check, done, eq, run, setup

tmp = setup()

from botcore import regions  # noqa: E402
from botcore.campaign import parse_order  # noqa: E402
from botcore.config import ConfigStore  # noqa: E402
from botcore.pipeline import Pipeline  # noqa: E402


def people(rows):
    return {"people": {n: {"branch": b, "community": False} for n, b in rows}}


(tmp / "branch_city.json").write_text(json.dumps(people([("杨晓锋", "万盛"), ("柳军", "古南"), ("杨鹏", "东林"), ("李四", "通惠")]),
                                                 ensure_ascii=False), encoding="utf-8")
(tmp / "branch_rural.json").write_text(json.dumps(people([("张斌", "打通"), ("杨晓峰", "赶水"), ("刘军", "打通"), ("杨鹏", "安稳")]),
                                                  ensure_ascii=False), encoding="utf-8")
(tmp / "branch_extra.json").write_text(json.dumps({"people": [["张彬", "永新"], ["张彬", "隆盛"]]}, ensure_ascii=False),
                                       encoding="utf-8")
(tmp / "roster_rural.json").write_text(json.dumps({"teams": ["永新战队", "打通战队"], "people": {
    "张彬": {"group": "王小钢、张彬", "team": "永新战队"}, "王小钢": {"group": "王小钢、张彬", "team": "永新战队"},
    "张斌": {"group": "张斌、张崇辉", "team": "打通战队"}, "张崇辉": {"group": "张斌、张崇辉", "team": "打通战队"}}},
    ensure_ascii=False), encoding="utf-8")
(tmp / "userid_map.json").write_text(json.dumps({"LiuJun": "柳军"}, ensure_ascii=False), encoding="utf-8")

store = ConfigStore()
p = Pipeline(FakePlatform(), store)
rs = p.rosters
rs.maybe_reload()


def who(uid, branch=""):
    i = rs.identify_sender(uid, branch)
    return i.name, i.branch, i.role


def test_pick_by_branch():
    eq(who("ZhangBin", "永新支局")[:2], ("张彬", "永新"), "ZhangBin + 永新支局 → 永新张彬")
    eq(who("ZhangBin", "打通支局")[:2], ("张斌", "打通"), "ZhangBin + 打通支局 → 打通张斌")
    eq(who("ZhangBin", "隆盛")[:2], ("张彬", "隆盛"), "ZhangBin + 隆盛 → 隆盛渠道张彬")
    eq(who("YangXiaoFeng", "赶水支局")[:2], ("杨晓峰", "赶水"), "YangXiaoFeng + 赶水 → 杨晓峰")
    eq(who("YangXiaoFeng", "万盛支局")[:2], ("杨晓锋", "万盛"), "YangXiaoFeng + 万盛 → 杨晓锋")
    eq(who("YangPeng", "安稳支局")[:2], ("杨鹏", "安稳"), "两个杨鹏照旧按支局分（原逻辑）")


def test_fallback_unchanged():
    eq(who("ZhangBin")[2], regions.UNKNOWN, "撞车又没写支局：照旧认不出")
    eq(who("ZhangBin", "古南支局")[2], regions.UNKNOWN, "撞车、写的支局一个都对不上：照旧认不出")
    eq(who("LiSi", "古南支局")[:2], ("李四", "通惠"), "账号唯一对上：照旧认本人，不看写的支局")
    eq(who("LiuJun", "打通支局")[:2], ("柳军", "古南"), "userid_map 登记过的优先（登记 LiuJun → 柳军）")
    eq(who("echo.", "打通支局")[2], regions.UNKNOWN, "账号不是拼音：照旧认不出")


def test_order_and_checkin():
    camp = p.campaign()
    o = parse_order("支局：永新支局\n业务类型:新增\n办理套餐：199\n业务号码:13911112222\n揽收人:张彬\n")
    o.sender_id, o.msg_id, o.at = "ZhangBin", "zb1", "2026-09-30 10:00:00"
    camp.add(o)
    eq((o.poster, o.role, o.group), ("张彬", regions.ACTIVITY, "王小钢、张彬"), "晒单：ZhangBin 写永新 → 永新张彬的小组")
    eq(p._checkin_identity("ZhangBin", "", "2026-09-30 10:00", "打通支局"), ("ok", regions.RURAL),
       "打卡：ZhangBin 水印支局打通 → 打通张斌（活动人员），记上")
    eq(p._checkin_identity("ZhangBin", "", "2026-09-30 10:00", "隆盛支局")[0], "non_activity",
       "打卡：ZhangBin 水印隆盛 → 隆盛渠道张彬，不是活动人员")
    eq(p._checkin_identity("ZhangBin", "王小钢、张彬", "2026-09-30 10:00", "")[0], "ok",
       "打卡：水印没读到支局 → 照旧看小组成员")


def test_watermark_short_branch():
    """09-30 用户：水印「支局：古南」和「支局：古南支局」都要能认。"""
    from botcore.checkin import parse_watermark
    base = "今日水印 时间:2026-09-30 10:00 支局：{b} 小组成员：王小钢、张彬 工作内容：上门"
    for b, want in (("古南支局", "古南支局"), ("古南", "古南支局"), ("古 南", "古南支局"), ("永新", "永新支局"),
                    ("万州", ""), ("", "")):
        eq(parse_watermark(base.format(b=b))["branch"], want, f"水印「支局：{b}」→「{want}」")
    wm = parse_watermark(base.format(b="永新").replace("王小钢、张彬", ""))
    eq(p._checkin_identity("ZhangBin", wm["members"], "2026-09-30 10:00", wm["branch"]), ("ok", regions.RURAL),
       "水印只写「支局：永新」+ 账号 ZhangBin：按支局认出永新张彬，记上")


run(test_pick_by_branch)
run(test_watermark_short_branch)
run(test_fallback_unchanged)
run(test_order_and_checkin)
done()
