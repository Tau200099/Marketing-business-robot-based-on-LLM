"""
国庆每天的活动名单：从城区 / 农村两张「活动计划表」生成 data/roster_days.json（09-29 用户：提前做好放后台，到哪天用哪天）。

    python deploy/make_roster_days.py --city 城区计划.xlsx --rural 农村计划.xlsx \\
        --fixes /opt/feishu-bot/data/roster_days_fixes.json --out /opt/feishu-bot/data/roster_days.json

fixes 是用户确认过的改正（错字、某天某组换人、新盛算农村），放数据目录、不进 git（里面是人名）。
名单文件在数据根目录，每日备份会带上；机器人发现文件变了自动重新读，不用重启。
"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from botcore import regions  # noqa: E402

CLASS_ORDER = ["通惠", "古南", "文龙", "万盛", "东林", "打通", "赶水", "东溪", "青年", "新盛", "郭扶",
               "三江", "南桐", "安稳", "永新", "隆盛", "三角"]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--city")
    ap.add_argument("--rural")
    ap.add_argument("--fixes")
    ap.add_argument("--out", required=True)
    ap.add_argument("--year", type=int, default=date.today().year)
    a = ap.parse_args()
    fixes = json.loads(Path(a.fixes).read_text(encoding="utf-8")) if a.fixes else {}
    parts, notes = [], []
    for path, region in ((a.city, regions.CITY), (a.rural, regions.RURAL)):
        if path:
            got, n = regions.parse_daily_plan(Path(path).read_bytes(), region, a.year, fixes)
            parts.append(got)
            notes += n
    data = regions.merge_daily(parts, CLASS_ORDER)
    out = Path(a.out)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out)
    for day, regs in data["days"].items():
        line = []
        for reg, ros in regs.items():
            groups = {(p["team"], p["group"]) for p in ros["people"].values()}
            line.append(f"{reg} {len(ros['teams'])} 队 {len(groups)} 组 {len(ros['people'])} 人")
        print(day, "；".join(line))
    for n in notes:
        print("提醒：", n)
    print("已写", out)


if __name__ == "__main__":
    main()
