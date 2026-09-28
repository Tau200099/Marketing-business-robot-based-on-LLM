"""
多群（09-29 国庆版）：城区 / 农村两个区域，每区一张【支局名册】（全体人员）+ 一张【活动名册】（参加活动的人），
外加一份【支局长名单】（支局长打卡点检表用）。规则见 docs/国庆多群版功能说明.md 第二节。

    文件                        内容
    roster.json                 城区活动名册（沿用原来那张：姓名 → 分组 / 战区）
    roster_rural.json           农村活动名册（同上格式）
    branch_city.json            城区支局名册：姓名 → 支局（可带「小区营销」标记）
    branch_rural.json           农村支局名册：同上
    chiefs.json                 支局长名单：类别 / 支局 / 支局长

一个人归成一类（按人，不按群）：城区活动 / 农村活动 / 城区非活动 / 农村非活动 / 未识别。
活动名册里的人算活动人员；只在支局名册里的算非活动；哪张都没有 = 未识别。

多群模式什么时候开：config.yaml 的 groups 里配了 rural（农村活动群）或 hq（攻坚群）。
没开就是原来的单群行为（只有城区活动名册那一张）。
"""

from __future__ import annotations

import io
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .campaign import NotRosterSheet, Roster, RosterImportError
from .paths import DATA

log = logging.getLogger("botcore.regions")

CITY, RURAL = "城区", "农村"
REGIONS = (CITY, RURAL)
ACTIVITY, OTHER, UNKNOWN = "活动", "非活动", "未识别"

ACTIVITY_FILES = {CITY: "roster.json", RURAL: "roster_rural.json"}
BRANCH_FILES = {CITY: "branch_city.json", RURAL: "branch_rural.json"}
CHIEFS_FILE = "chiefs.json"

# 管理员单聊先发这句，再发 Excel：导哪张
ROSTER_COMMANDS = {
    "城区活动名册": (CITY, ACTIVITY), "农村活动名册": (RURAL, ACTIVITY),
    "城区支局名册": (CITY, "支局"), "农村支局名册": (RURAL, "支局"),
    "支局长名单": ("", "支局长"),
}

_SUFFIX = re.compile(r"(?:支局|战队|战区|分局)$")


def branch_stem(name: str) -> str:
    """「通惠支局」「通惠战队」「通惠」→「通惠」。"""
    return _SUFFIX.sub("", (name or "").strip()).strip()


def multi_enabled(store) -> bool:
    try:
        return any(g.role in ("rural", "hq") for g in store.groups())
    except Exception:
        return False


def group_role(store, chat_id: str) -> str:
    """群号 → 角色 city / rural / hq；不在 groups 里返回空串。"""
    try:
        for g in store.groups():
            if g.chat_id == chat_id:
                return g.role
    except Exception:
        pass
    return ""


def role_chats(store, role: str) -> tuple[str, ...]:
    try:
        return tuple(g.chat_id for g in store.groups() if g.role == role)
    except Exception:
        return ()


REGION_OF_ROLE = {"city": CITY, "rural": RURAL}


# ---------------- Excel 读表头 ----------------

def _rows(data: bytes):
    from openpyxl import load_workbook
    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:
        raise RosterImportError(f"文件打不开（要 .xlsx，旧的 .xls 请另存为 xlsx）：{exc}")
    for ws in wb.worksheets:
        rows = [[str(c).strip() if c is not None else "" for c in r] for r in ws.iter_rows(values_only=True)]
        yield ws.title, rows


def _find_header(rows: list[list[str]], want: dict[str, re.Pattern], need: tuple[str, ...]):
    """前 10 行里找表头：want = {字段: 正则}；need 里的字段都找到才算。返回 (表头行号, {字段: 列号})。"""
    for r, cells in enumerate(rows[:10]):
        cols = {}
        for key, pat in want.items():
            for c, v in enumerate(cells):
                if v and pat.fullmatch(v) and key not in cols:
                    cols[key] = c
        if all(k in cols for k in need):
            return r, cols
    return None, None


# ---------------- 支局名册 ----------------

class BranchRoster:
    """姓名 → {branch: 支局, community: 小区 / 场镇营销}。"""

    WANT = {"name": re.compile(r"姓名|人员|员工姓名|营销人员"),
            "branch": re.compile(r"支局|所属支局|单位|部门"),
            "community": re.compile(r".*(?:小区|场镇).*")}

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.people: dict[str, dict] = {}
        self._mtime = None
        self.reload()

    def reload(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self.people = {}
            return
        except Exception as exc:
            log.error("支局名册 %s 读不了：%s", self.path.name, exc)
            return
        self.people = dict(raw.get("people") or {})

    def maybe_reload(self) -> bool:
        try:
            m = self.path.stat().st_mtime
        except OSError:
            return False
        if m == self._mtime:
            return False
        self.reload()
        return True

    def import_workbook(self, data: bytes, dry_run: bool = False) -> str:
        people: dict[str, dict] = {}
        found = False
        for _title, rows in _rows(data):
            hr, cols = _find_header(rows, self.WANT, ("name", "branch"))
            if hr is None:
                continue
            found = True
            last_branch = ""
            for cells in rows[hr + 1:]:
                def at(key):
                    c = cols.get(key)
                    return cells[c] if c is not None and c < len(cells) else ""
                name, branch = at("name"), at("branch") or last_branch     # 合并单元格：空的是「同上」
                if branch:
                    last_branch = branch
                if not name or name in ("姓名",):
                    continue
                if not branch:
                    raise RosterImportError(f"「{name}」这行缺支局")
                comm = at("community")
                people[name] = {"branch": branch_stem(branch),
                                "community": bool(comm) and comm not in ("否", "无", "0", "×", "-")}
            break
        if not found:
            raise NotRosterSheet("表里没有「姓名 / 支局」两列的表头")
        if len(people) < 2:
            raise RosterImportError("表头下面没读到人")
        branches = sorted({v["branch"] for v in people.values()})
        if not dry_run:
            self.path.write_text(json.dumps({"people": people}, ensure_ascii=False, indent=1), encoding="utf-8")
            self.reload()
        n_comm = sum(1 for v in people.values() if v["community"])
        return f"{len(people)} 人 / {len(branches)} 个支局" + (f"（小区 / 场镇营销 {n_comm} 人）" if n_comm else "")


# ---------------- 支局长名单 ----------------

class ChiefList:
    """[{class: 一类, branch: 通惠, name: 翁艺丹}]，顺序就是点检表的行顺序。"""

    WANT = {"name": re.compile(r"支局长|姓名"), "branch": re.compile(r"支局"),
            "cls": re.compile(r"类别|分类|类型")}

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.chiefs: list[dict] = []
        self._mtime = None
        self.reload()

    def reload(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._mtime = self.path.stat().st_mtime
        except FileNotFoundError:
            self.chiefs = []
            return
        except Exception as exc:
            log.error("支局长名单读不了：%s", exc)
            return
        self.chiefs = list(raw.get("chiefs") or [])

    def maybe_reload(self) -> bool:
        try:
            m = self.path.stat().st_mtime
        except OSError:
            return False
        if m == self._mtime:
            return False
        self.reload()
        return True

    def names(self) -> set[str]:
        return {c["name"] for c in self.chiefs}

    def of(self, name: str) -> dict | None:
        return next((c for c in self.chiefs if c["name"] == name), None)

    def import_workbook(self, data: bytes, dry_run: bool = False) -> str:
        chiefs: list[dict] = []
        found = False
        for _title, rows in _rows(data):
            # 「支局长」和「支局」都能匹配「支局」—— 先认「支局长」那列，再认剩下的「支局」
            hr = None
            for r, cells in enumerate(rows[:10]):
                if any(v == "支局长" for v in cells) and any(v == "支局" for v in cells):
                    hr = r
                    break
            if hr is None:
                continue
            found = True
            head = rows[hr]
            c_name = head.index("支局长")
            c_branch = head.index("支局")
            c_cls = next((i for i, v in enumerate(head) if self.WANT["cls"].fullmatch(v)), None)
            last_cls = last_branch = ""
            for cells in rows[hr + 1:]:
                def at(c):
                    return cells[c] if c is not None and c < len(cells) else ""
                name = at(c_name)
                branch = at(c_branch) or last_branch
                cls = at(c_cls) or last_cls
                if branch:
                    last_branch = branch
                if cls:
                    last_cls = cls
                if not name or name == "支局长":
                    continue
                chiefs.append({"class": cls, "branch": branch_stem(branch), "name": name})
            break
        if not found:
            raise NotRosterSheet("表里没有「支局 / 支局长」两列的表头")
        if not chiefs:
            raise RosterImportError("表头下面没读到支局长")
        if not dry_run:
            self.path.write_text(json.dumps({"chiefs": chiefs}, ensure_ascii=False, indent=1), encoding="utf-8")
            self.reload()
        return f"{len(chiefs)} 个支局长 / {len({c['branch'] for c in chiefs})} 个支局"


# ---------------- 认人 ----------------

@dataclass
class Identity:
    name: str = ""
    region: str = ""          # 城区 / 农村 / ""
    role: str = UNKNOWN       # 活动 / 非活动 / 未识别
    team: str = ""            # 活动人员的战队（活动名册里的「战区」）
    group: str = ""           # 活动人员的小组（成员串）
    branch: str = ""          # 支局（简称：通惠）
    community: bool = False   # 小区 / 场镇营销人员（总表里新增宽带、中高套 ×1.1）


class _AllNames:
    """给 checkin.sender_name 用：四张名册 + 支局长名单里的所有姓名。"""

    def __init__(self, people: dict) -> None:
        self.people = people


class RosterSet:
    def __init__(self, data_dir: Path = DATA) -> None:
        d = Path(data_dir)
        self.activity = {r: Roster(d / ACTIVITY_FILES[r]) for r in REGIONS}
        self.branch = {r: BranchRoster(d / BRANCH_FILES[r]) for r in REGIONS}
        self.chiefs = ChiefList(d / CHIEFS_FILE)

    def maybe_reload(self) -> bool:
        changed = False
        for r in REGIONS:
            changed |= self.activity[r].maybe_reload()
            changed |= self.branch[r].maybe_reload()
        changed |= self.chiefs.maybe_reload()
        return changed

    def all_names(self) -> _AllNames:
        people: dict[str, dict] = {}
        for r in REGIONS:
            people.update({n: {} for n in self.branch[r].people})
            people.update({n: {} for n in self.activity[r].people})
        people.update({n: {} for n in self.chiefs.names()})
        return _AllNames(people)

    def identify_name(self, name: str) -> Identity:
        name = (name or "").strip()
        if not name:
            return Identity()
        for r in REGIONS:
            ros = self.activity[r]
            if name in ros.people:
                grp, team = ros.lookup(name)
                b = self.branch[r].people.get(name) or {}
                info = ros.people.get(name) or {}
                return Identity(name, r, ACTIVITY, team, grp, b.get("branch") or branch_stem(team),
                                bool(b.get("community") or info.get("community")))
        for r in REGIONS:
            b = self.branch[r].people.get(name)
            if b:
                return Identity(name, r, OTHER, "", "", b.get("branch", ""), bool(b.get("community")))
        return Identity(name=name)

    def identify_sender(self, userid: str) -> Identity:
        from .checkin import sender_name
        return self.identify_name(sender_name(userid, self.all_names()))

    def identify_members(self, members: str) -> Identity:
        """打卡第 2 步：水印「小组成员」里出现的活动人员（按出现次数最多的那个组）。"""
        compact = re.sub(r"[\s、,，/]", "", members or "")
        if not compact:
            return Identity()
        best, hits = Identity(), 0
        for r in REGIONS:
            counts: dict[str, int] = {}
            first: dict[str, str] = {}
            for name in self.activity[r].people:
                if name and name in compact:
                    g = self.activity[r].people[name].get("group", "")
                    counts[g] = counts.get(g, 0) + 1
                    first.setdefault(g, name)
            for g, c in counts.items():
                if c > hits:
                    best, hits = self.identify_name(first[g]), c
        return best

    def import_named(self, which: str, data: bytes) -> str:
        """管理员单聊「农村活动名册」+ Excel 这种：导进对应那张。"""
        region, kind = ROSTER_COMMANDS[which]
        if kind == ACTIVITY:
            summary = self.activity[region].import_workbook(data)
        elif kind == "支局":
            summary = self.branch[region].import_workbook(data)
        else:
            summary = self.chiefs.import_workbook(data)
        # 活动名册里的人应该在本区支局名册里 —— 只提醒，不拦
        if kind == ACTIVITY and self.branch[region].people:
            missing = [n for n in self.activity[region].people if n not in self.branch[region].people]
            if missing:
                summary += f"；注意：{len(missing)} 人不在{region}支局名册里（{'、'.join(missing[:5])}{'…' if len(missing) > 5 else ''}）"
        return summary
