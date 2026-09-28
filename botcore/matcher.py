"""关键词规则引擎：编译一次，之后每条消息只做常数级匹配。"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field

# 关键词按三种方式匹配，规则见 _boundary_kind：
#
#   digit —— 纯数字，如 "79"。用 (?<!\d)79(?!\d)：前后不能是数字。
#            不能用 \b —— Python 正则里中文算单词字符，"79元" 的 9 和 元 之间
#            没有词边界，\b79\b 会匹配不上，而这恰恰是中文群里最常见的写法。
#   word  —— 纯英文单词，如 "hi"。用 \b：否则 this / shipping 全都会误命中。
#   none  —— 含中文，直接子串匹配。中文没有词边界概念，
#            "故障" 出现在 "生产故障了" 里就该算命中。
_ASCII_WORD = re.compile(r"^[A-Za-z0-9_]+$")
_PURE_DIGITS = re.compile(r"^\d+$")


@dataclass
class Rule:
    name: str
    mode: str                      # any | all | regex
    keywords: list[str]
    reply: str
    # 排除词：只要命中任意一个，本条规则一律不触发（优先级高于 keywords）。
    # 用来挡掉「关键词出现了，但语境完全不对」的情况，
    # 例如关键词「宽带」配上排除词「坏」，"宽带坏了" 就不会被点赞。
    exclude: list[str] = field(default_factory=list)
    enabled: bool = True
    ignore_case: bool = True
    cooldown: float = 0.0
    at_sender: bool = False
    # auto   = 纯数字用数字边界、纯英文用词边界、含中文按子串（推荐）
    # always = 一律加词边界（"你好" 就匹配不到 "你好啊" 了）
    # never  = 一律子串匹配（数字和英文关键词容易误伤，慎用）
    word_boundary: str = "auto"
    # 命中后按这个业务类型记进清单（新增 / 存量 / 单C）。
    # 留空表示【不计数】—— 像「链路自检」这种规则就该留空。
    # 不设的话规则命中直接 return，那单永远不会出现在清单和排名里。
    count_as: str = ""

    _patterns: list[re.Pattern] = field(default_factory=list, repr=False)
    _plain: list[str] = field(default_factory=list, repr=False)
    _bounded: list[re.Pattern] = field(default_factory=list, repr=False)
    _ex_plain: list[str] = field(default_factory=list, repr=False)
    _ex_bounded: list[re.Pattern] = field(default_factory=list, repr=False)

    def compile(self) -> "Rule":
        """预编译正则、预降大小写，避免每条消息重复开销。"""
        if self.mode not in ("any", "all", "regex"):
            raise ValueError(f"规则 {self.name!r} 的 mode={self.mode!r} 非法，应为 any/all/regex")
        if self.word_boundary not in ("auto", "always", "never"):
            raise ValueError(
                f"规则 {self.name!r} 的 word_boundary={self.word_boundary!r} 非法，"
                "应为 auto/always/never"
            )
        if not self.keywords:
            raise ValueError(f"规则 {self.name!r} 没有配置 keywords")

        flags = re.IGNORECASE if self.ignore_case else 0

        if self.mode == "regex":
            self._patterns = [re.compile(k, flags) for k in self.keywords]
        else:
            self._plain, self._bounded = self._compile_terms(self.keywords, flags)

        self._ex_plain, self._ex_bounded = self._compile_terms(self.exclude, flags)
        return self

    def _compile_terms(self, terms: list[str], flags: int) -> tuple[list[str], list[re.Pattern]]:
        plain: list[str] = []
        bounded: list[re.Pattern] = []
        for kw in terms:
            kind = self._boundary_kind(kw)
            if kind == "digit":
                bounded.append(re.compile(rf"(?<!\d){re.escape(kw)}(?!\d)", flags))
            elif kind == "word":
                # 不能用 \b：Python 里中文也算 \w，「新装FTTR一户」的 装 和 F 之间
                # 没有词边界，\bFTTR\b 永远匹配不上（审查踩出来的）。
                # 边界改成「前后不是英文字母 / 数字」—— 中文、标点、空格都算边界
                bounded.append(re.compile(
                    rf"(?<![A-Za-z0-9]){re.escape(kw)}(?![A-Za-z0-9])", flags))
            else:
                plain.append(kw.lower() if self.ignore_case else kw)
        return plain, bounded

    def _boundary_kind(self, keyword: str) -> str:
        if self.word_boundary == "never":
            return "none"
        # 纯数字永远走数字边界：79 不该命中 179 / 1799，但该命中 "79元"、"新增79"
        if _PURE_DIGITS.match(keyword):
            return "digit"
        if self.word_boundary == "always":
            return "word"
        return "word" if _ASCII_WORD.match(keyword) else "none"

    @staticmethod
    def _hit_any(plain: list[str], bounded: list[re.Pattern], haystack: str, text: str) -> bool:
        return (any(k in haystack for k in plain)
                or any(p.search(text) for p in bounded))

    def match(self, text: str) -> bool:
        haystack = text.lower() if self.ignore_case else text

        # 排除词优先：命中任意一个就直接否决，不再看 keywords
        if (self._ex_plain or self._ex_bounded) and \
                self._hit_any(self._ex_plain, self._ex_bounded, haystack, text):
            return False

        if self.mode == "regex":
            # regex 模式恒为「任一命中」
            return any(p.search(text) for p in self._patterns)

        if self.mode == "all":
            # 空列表下 all() 为 True，正好是「这一类没有关键词，不构成约束」
            return (all(k in haystack for k in self._plain)
                    and all(p.search(text) for p in self._bounded))

        return self._hit_any(self._plain, self._bounded, haystack, text)


class Matcher:
    """持有全部规则，并按 (规则, 会话) 维度做冷却限流。"""

    def __init__(self, rules: list[Rule], strategy: str = "first") -> None:
        self.rules = [r.compile() for r in rules if r.enabled]
        self.strategy = strategy
        self._last_fired: dict[tuple[str, str], float] = {}
        self._lock = threading.Lock()

    def match(self, text: str, chat_id: str) -> list[Rule]:
        """返回本条消息应触发的规则；已在冷却期内的规则会被跳过。"""
        if not text:
            return []

        fired: list[Rule] = []
        now = time.monotonic()

        for rule in self.rules:
            if not rule.match(text):
                continue
            if not self._take_token(rule, chat_id, now):
                continue
            fired.append(rule)
            if self.strategy == "first":
                break
        return fired

    def _take_token(self, rule: Rule, chat_id: str, now: float) -> bool:
        if rule.cooldown <= 0:
            return True
        key = (rule.name, chat_id)
        with self._lock:
            last = self._last_fired.get(key, 0.0)
            if now - last < rule.cooldown:
                return False
            self._last_fired[key] = now
            return True
