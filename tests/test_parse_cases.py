"""
晒单解析回归：tests/cases/ 里的真实写法，严格解析（不调模型）出来的字段要和预期一致。
  business.json  标准模板的各种业务
  format.json    字段名写歪、顺序打乱；need_llm=true 的只有模型容错才解析得出，离线跳过
  noise.json     转发空模板、催晒单、问问题 —— 一律不能入清单（expect: null）
改了解析 / 积分口径后预期变了：先确认是规则本意，再改 cases 里的 expect，并在 why 里写原因。
expect 里的 branch（支局）不比：Order 早就不存支局了，归组按发送人 / 揽收人查名册（09-23 起）。
"""
import json
from pathlib import Path

from _harness import check, done, run, setup

setup()

from botcore.campaign import parse_order  # noqa: E402

CASES = Path(__file__).with_name("cases")
_NOT_ON_ORDER = {"branch"}


def _run_file(name: str):
    cases = json.loads((CASES / name).read_text(encoding="utf-8"))
    skipped = 0
    for i, c in enumerate(cases, 1):
        o = parse_order(c["text"], msg_id=f"{name}-{i}", at="2026-09-14 10:00:00")
        tag = f"{name} #{i} {c['text'][:24]!r}"
        if c.get("expect") is None:
            check(o is None or not (o.kind or o.fee), f"{tag} 不该入清单，却解析成 {o and (o.kind, o.fee)}")
            continue
        if o is None:
            if c.get("need_llm"):
                skipped += 1
                continue
            check(False, f"{tag} 严格解析没认出来")
            continue
        for k, want in c["expect"].items():
            if k in _NOT_ON_ORDER:
                continue
            got = getattr(o, k, None)
            if isinstance(want, bool):
                got = bool(got)
            check(got == want, f"{tag} 字段 {k}：期望 {want!r}，实际 {got!r}")
    if skipped:
        print(f"  - {name}：{skipped} 条要模型容错，离线跳过")


run(lambda: _run_file("business.json"))
run(lambda: _run_file("format.json"))
run(lambda: _run_file("noise.json"))
done()
