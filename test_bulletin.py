#!/usr/bin/env python3
"""
离线生成一份业绩通报，不联网、不发消息。

    python test_bulletin.py                    # 按 config.yaml：排名按天、清单按周
    python test_bulletin.py 2026-09-03         # 排名和清单都只算这一天
    python test_bulletin.py 2026-09-03 2026-09-05   # 都算这几天

输出到 reports/：一张 PNG、一个 Excel，文字通报打印在终端。
用来在改话术、改目标值、改名册之后先看一眼效果，再让机器人往群里发。
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor

from botcore.bulletin import Bulletin, find_font
from botcore.campaign import Campaign, Roster
from botcore.config import ConfigStore, load_env
from botcore.console import force_utf8


class _NoPlatform:
    """不发消息的占位平台。Bulletin.build() 用不到平台，publish() 才用。"""
    name = "offline"
    cfg = None


def main() -> int:
    force_utf8()
    load_env()
    store = ConfigStore()
    cs = store.campaign_settings()
    if len(sys.argv) > 1:
        cs.rank_period = cs.ledger_period = "fixed"
        cs.start = sys.argv[1]
        cs.end = sys.argv[2] if len(sys.argv) > 2 else sys.argv[1]

    font = find_font()
    print(f"字体：{font or '【没找到中文字体，图片会是方块】'}")

    camp = Campaign(Roster(), cs.targets)
    since, until = cs.window("rank")
    l_since, l_until = cs.window("ledger")
    print(f"排名范围：{since[:10]} ~ {until[:10]}  清单范围：{l_since[:10]} ~ {l_until[:10]}"
          f"  台账共 {len(camp.orders)} 单")

    b = Bulletin(_NoPlatform(), store, lambda: camp, ThreadPoolExecutor(1))
    text, png, xlsx = b.build()

    print("\n" + "=" * 60)
    print(text)
    print("=" * 60)
    print(f"\n图片：{xlsx.with_suffix('.png')}（{len(png) // 1024} KB）")
    print(f"Excel：{xlsx}")

    print("\n战队排名：")
    for t in camp.stats(since, until):
        flag = f"  {t.rank}." if t.rank else "  -."
        print(f"{flag} {t.team:<6} 宽带{t.broadband} 中高套{t.high} 云盒{t.box} "
              f"积分{t.points} 进度{t.progress:.2%}"
              + (f"  待核对{t.pending}单" if t.pending else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
