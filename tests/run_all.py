"""
全套测试：每个 tests/test_*.py 单独一个进程跑，最后再跑一遍 test_rules.py（消息分流）。
不联网、不花钱、不碰正式数据。

    python tests/run_all.py            全跑
    python tests/run_all.py activity   只跑文件名里带 activity 的

退出码：0 全过，1 有失败。deploy/release.sh 发布前会跑它，不过不让发布。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PY = sys.executable


def main(argv: list[str]) -> int:
    pick = argv[0] if argv else ""
    files = sorted(p for p in (REPO / "tests").glob("test_*.py") if pick in p.name)
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    env.pop("BOT_DATA_DIR", None)          # 每个测试自己建临时目录
    results = []
    for f in files:
        print(f"== {f.name}")
        t0 = time.time()
        r = subprocess.run([PY, str(f)], cwd=REPO, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=900)
        out = (r.stdout + r.stderr).rstrip()
        if out:
            print(out)
        results.append((f.name, r.returncode == 0, time.time() - t0))
    if not pick:
        print("== test_rules.py（消息分流冒烟）")
        r = subprocess.run([PY, "test_rules.py"], cwd=REPO, env=env, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300)
        ok = r.returncode == 0 and "条会调模型" in r.stdout
        print("  " + (r.stdout.strip().splitlines() or ["（无输出）"])[-1])
        if not ok:
            print(r.stderr[-2000:])
        results.append(("test_rules.py", ok, 0.0))

    print("\n" + "=" * 50)
    for name, ok, sec in results:
        print(f"  {'通过' if ok else '失败'}  {name}  {sec:.0f}s")
    bad = [n for n, ok, _ in results if not ok]
    print(f"共 {len(results)} 组，{'全部通过' if not bad else '失败 ' + str(len(bad)) + ' 组：' + '、'.join(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
