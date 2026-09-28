"""多群：config 的 groups 段能解析；管理员单聊「群号核对」往每个群发群名；权限。"""
from _harness import FakePlatform, check, done, eq, run, setup

tmp = setup()

# 测试用的 config 一定带三个群（正式 config 有没有配都不影响）
cfg = (tmp / "config.yaml").read_text(encoding="utf-8")
if "\ngroups:" not in cfg:
    cfg += """
groups:
  - {chat_id: g_city, name: 城区支局营销活动群, role: city}
  - {chat_id: g_rural, name: 农村支局营销活动群, role: rural}
  - {chat_id: g_hq, name: 綦江分公司秋促营销攻坚群, role: hq}
"""
    (tmp / "config.yaml").write_text(cfg, encoding="utf-8")

from botcore.config import ConfigStore  # noqa: E402
from botcore.message import InboundMessage  # noqa: E402
from botcore.pipeline import Pipeline  # noqa: E402

store = ConfigStore()
ADMIN = store.backup_settings().admins[0]
plat = FakePlatform()
p = Pipeline(plat, store)


def send(text, chat="p2p", sender=ADMIN) -> str:
    before = len(plat.replies)
    p._process(InboundMessage(msg_id=f"g{len(plat.replies)}{sender}{chat}", chat_id=sender if chat == "p2p" else "g",
                              chat_type=chat, text=text, sender_id=sender, mentions_bot=True), 0.0)
    return plat.replies[-1] if len(plat.replies) > before else ""


def test_config():
    groups = store.groups()
    eq(len(groups), 3, "三个群")
    eq(sorted(g.role for g in groups), ["city", "hq", "rural"], "三种角色")
    check(all(g.chat_id and g.name for g in groups), "每个群都有群号和群名")


def test_group_check():
    check("只有管理员" in send("群号核对", sender="YangTong"), "非管理员不能用")
    check("单聊" in send("群号核对", chat="group"), "群里提示单聊")
    eq(plat.sent_texts, [], "上面两次都没往群里发")
    r = send("群号核对")
    check(r.startswith("已发到 3 个群"), f"回执：{r}")
    groups = store.groups()
    eq([c for c, _t in plat.sent_texts], [g.chat_id for g in groups], "每个群发一条")
    for (_c, t), g in zip(plat.sent_texts, groups):
        check(g.name in t, f"发给 {g.name} 的消息里有群名：{t}")


run(test_config)
run(test_group_check)
done()
