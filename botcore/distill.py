"""
OCR 文本提纯：从一堆识别结果里只留下真正有业务含义的部分。

为什么必须有这一步（两个理由，都很硬）：

1. 隐私。现场照片里可能拍到客户身份证、手机号、住址。OCR 会一视同仁地
   把它们识别出来，如果原样发给模型服务商，就是把客户证件信息外传。
   实测样本里确实出现过「居民身份证 签发机关xx县公安局 有效期限...」。

2. 准确率。今日水印相机的水印里有海拔、支局、防伪码、活动名称、小组成员，
   真正有用的只有「工作内容」一行。噪声越多模型越容易判错。

策略：优先抽「工作内容」字段；抽不到就退回全文，但无论走哪条路，
手机号、身份证号一律先抹掉。宁可漏判一单，也不能把客户证件号发出去。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

# ⚠️ 水印装饰文字，提取字段【之前】先全部抹掉。
#
# 为什么必须先抹：水印是分层渲染的，OCR 的阅读顺序会把「今日水印」
# 这类标签插进工作内容中间。实测：
#   OCR 全文  …工作内容：工程师商机上门，新增 今日水印 139+15小合约 相机真实可验
# 如果把「今日水印」当成字段的结束标记，就会在那里截断，
# 【139+15小合约 这个套餐档位直接丢掉】—— 而它是最关键的业务信息，
# 丢了模型就认不出档位，这单也没法正确计数。
_CHROME = re.compile(
    r"今日水印|相机真实可验|相机真实|验真\s*/?\s*导航|防伪码?|"
    r"CHINATELECOM|中国电信|关闭境外\s*来电服务|暑促专项营销活动|"
    r"专项营销活动"
)

# 水印相机的关键字段。冒号可能是中文/英文/缺失。
# 结束标记【只留真正的字段标签】，不再包含上面那些装饰文字。
# ⚠️ 今日水印有【两套模板】，字段名不一样：
#     模板 A：支局：xx  小组成员：xx  工作内容：xx
#     模板 B：工作单位：xx  拍摄人：xx  备注：xx
# 群里两种都在用。只认模板 A 的话，模板 B 的照片会退回全文兜底 ——
# 业务内容抽不到、支局抽不到、时间也抽不到，等于白拍（实测踩过：
# 「备注：预约上门，新增宽带办理」整句没被识别成业务内容）。
# 「工作内容」优先，「备注」只是兜底 —— 两个同权取最左匹配的话，纸质单上
# 「备注」栏排在前面时真正的工作内容会被丢掉（审查踩出来的）
_WORK_TAIL = r"\s*[:：]?\s*(.{2,80}?)(?=\s*(?:小组成员|拍摄人|工作单位|时\s*间|地\s*点|海拔|支局|打卡|签到|防伪|备\s*注|$))"
_WORK_FIELD = re.compile(r"工作内容" + _WORK_TAIL)
_REMARK_FIELD = re.compile(r"备\s*注" + _WORK_TAIL)

# 兜底：没有「工作内容」标签时，找像业务描述的片段
# （套餐档位数字 + 新增/续约/改/融合/FTTR 这类词）
_WORK_FALLBACK = re.compile(
    r"[^\s]{0,12}?\d{2,3}\s*(?:改|续约|续签|新增|融合|升级|加装)[^\s]{0,20}"
)

# 第三种模板：业务内容【没有字段标签】，直接裸写在成员/单位旁边：
#     BED 打卡 14:17 … 2026.09.03星期四 预约上门 小组成员：王春燕陈渝波
#     14:45 打卡 … 拍摄人：刘青峰 吴兴国 预约上门 工作单位：新盛支局
# 所以额外找一遍「业务动作词」，抓它所在的那一小段。
# 放在最后兜底 —— 前面两条都没命中才用，避免抢走正常字段。
_WORK_BARE = re.compile(
    r"[^\s]{0,10}(?:预约上门|上门|新增|续约|续签|办理|装机|开通|加装|升级|"
    r"拆机|维系|拦截|甩单|受理|空号|宣传|查询|营销)[^\s]{0,16}"
)

# 支局。先按「支局：xxx支局」的字段形式找，找不到再在全文里捞。
# 注意必须在脱敏和截断【之前】提取 —— 支局在水印里的位置和工作内容隔得远。
_BRANCH_FIELD = re.compile(r"(?:支\s*局|工作单位)\s*[:：]\s*([一-龥]{2,6}?支局)")
# 「支局：古南」没写「支局」两个字（09-30 用户：古南、古南支局都要能认）：只认这 17 个支局名，免得抓错；
# OCR 常把字拆开（「古 南」），字中间允许空格。认出来统一写成「古南支局」
KNOWN_BRANCHES = ("通惠", "古南", "文龙", "万盛", "东林", "打通", "赶水", "东溪", "青年", "新盛", "郭扶",
                  "三江", "南桐", "安稳", "永新", "隆盛", "三角")
_BRANCH_SHORT = re.compile(r"(?:支\s*局|工作单位)\s*[:：]\s*("
                           + "|".join(r"\s*".join(n) for n in KNOWN_BRANCHES) + r")")
# 宽松匹配只在【没有字段名】时用。以前 [一-龥]{2,4} 贪心往前吃，
# 「预约文龙支局」抓成「预约文龙支局」、「去了东林支局」抓成「了东林支局」，
# 战报里同一个支局分裂成两个桶。抓到后把前面粘着的动词 / 方位词剥掉，
# 剥到只剩 2 个字为止（支局名都是两个字：文龙 / 新盛 / 东林 / 南桐 …）
_BRANCH_LOOSE = re.compile(r"([一-龥]{2,4}支局)")
_BRANCH_NOISE = re.compile(r"^(?:预约|上门|工作单位|单位|地点|位置|所属|所在|归属|去了|来到|"
                           r"去|到|在|从|了|的|是|于|和|与|及|新增|续约|办理)")


def _clean_branch(name: str) -> str:
    stem = name[:-2]
    while len(stem) > 2:
        m = _BRANCH_NOISE.match(stem)
        if not m or len(stem) - len(m.group(0)) < 2:
            break
        stem = stem[len(m.group(0)):]
    return stem + "支局"

# 小组成员。OCR 常把两个名字连在一起（「尤共超李远红」），所以不拆，
# 整段留着做子串匹配 —— 判断「霍露」是不是这个组的人。
# 值一直取到下一个字段标签为止，否则会把「工作内容」也吞进来
# （实测抓成了「尤共超李远红工作内容」）。
# 终止标记里也要放业务动作词 —— 第三种模板里业务内容裸写在成员后面
# （「拍摄人：刘青峰 吴兴国 预约上门 工作单位：…」），
# 不卡住的话成员会抓成「刘青峰吴兴国预约上门」，后面拿它做子串匹配会出错。
_MEMBERS = re.compile(
    r"(?:小组\s*成员|拍摄人)\s*[:：]\s*(.{2,24}?)"
    r"(?=\s*(?:工作内容|备\s*注|工作单位|时\s*间|地\s*点|海拔|支局|打卡|签到|防伪|"
    r"预约|上门|新增|续约|续签|办理|装机|开通|加装|升级|拆机|维系|拦截|甩单|"
    r"受理|空号|宣传|查询|营销|今日水印|$))"
)

# 水印时间。两种模板：
#   1) 日期时间挨着：「时间：2026.08.22 16:37」「时间：2026.08.2219:12」（可能没空格）
#   2) 分开摆放：「打卡20:13 ... 2026.08.22星期六」
# 必须在【脱敏之后】提取 —— 否则会抓到身份证的「有效期限2009.03.20」。
_TIME_FULL = re.compile(
    r"(20\d{2})[.\-/年](\d{1,2})[.\-/月](\d{1,2})\D{0,6}?(\d{1,2})[:：](\d{2})")
_DATE_ONLY = re.compile(r"(20\d{2})[.\-/年](\d{1,2})[.\-/月](\d{1,2})")
# 时刻可能在标签前，也可能在标签后：
#   「时间：14:45」「打卡 14:17」  -> 标签在前
#   「14:45 打卡」                -> 标签在后
_CLOCK_ONLY = re.compile(
    r"(?:打卡|签到|时\s*间)\D{0,4}(\d{1,2})[:：](\d{2})"
    r"|(?<!\d)(\d{1,2})[:：](\d{2})\s*(?:打卡|签到)")
# 模板 B：时刻单独一块、没有标签，日期在它后面
#     15:20 | 2026-09-03 星期四 多云 33°C
# 没有这条的话这类照片抽不到水印时间，判重就失效了。
_TIME_BEFORE_DATE = re.compile(
    r"(?<!\d)(\d{1,2})[:：](\d{2})\D{0,12}?(20\d{2})[.\-/年](\d{1,2})[.\-/月](\d{1,2})")

# ---- 脱敏 ----
_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
# 固话 / 宽带账号：023xxxxxxxx 这种 0 开头 10~12 位。晒单模板的「业务号码」
# 常填这个，实测「02348469166」原样进了模型请求 —— 和手机号一样是客户号码
_LANDLINE = re.compile(r"(?<!\d)0\d{9,11}(?!\d)")
# 带分隔的写法：「139 1234 5678」「139-1234-5678」「023-4846-9166」「023 48469166」。
# 只认连续数字的话这些全漏（审查踩出来的）。分隔符允许空格 / 半角减号 / 长横线
_SEP = r"[ \-—–]"
_PHONE_SPLIT = re.compile(r"(?<!\d)1[3-9]\d" + _SEP + r"?\d{4}" + _SEP + r"?\d{4}(?!\d)")
_LANDLINE_SPLIT = re.compile(r"(?<!\d)0\d{2,3}" + _SEP + r"?\d{3,4}" + _SEP + r"?\d{4}(?!\d)")
# 「业务号码:」这类字段后面不管填的什么（手机、固话、带横杠 / 空格 / +86 的），整个抹掉
_CONTACT_FIELD = re.compile(
    r"(业务号码|联系电话|客户号码|手机号码?|宽带账号|账号|电话|固话|联系方式)"
    r"\s*[:：]\s*[\d\-—– +]{6,}")
# 全角数字：「１３９１２３４５６７８」。\d 认得全角，但 1[3-9] 这种字面量不认，先转半角
_FULLWIDTH = str.maketrans("０１２３４５６７８９", "0123456789")
_IDCARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_LONG_DIGITS = re.compile(r"(?<!\d)\d{12,}(?!\d)")     # 其它超长数字串
_ANTIFAKE = re.compile(r"防伪\s*[A-Za-z0-9]{6,}")
_ID_HINT = re.compile(r"(居民身份证|签发机关|有效期限)[^\s]*")


def redact(text: str) -> str:
    """抹掉手机号、固话、身份证号、防伪码等。任何往外发的文本都要先过这一道。"""
    text = (text or "").translate(_FULLWIDTH)
    text = _IDCARD.sub("[身份证]", text)
    text = _CONTACT_FIELD.sub(lambda m: m.group(1) + ":[号码]", text)
    text = _PHONE.sub("[手机号]", text)
    text = _PHONE_SPLIT.sub("[手机号]", text)
    text = _LANDLINE.sub("[号码]", text)
    text = _LANDLINE_SPLIT.sub("[号码]", text)
    text = _ID_HINT.sub("", text)
    text = _ANTIFAKE.sub("", text)
    text = _LONG_DIGITS.sub("[数字]", text)
    return re.sub(r"\s{2,}", " ", text).strip()


@dataclass
class Distilled:
    text: str            # 给模型看的文本
    source: str          # 来源说明，只用于日志排查
    branch: str = ""     # 支局，抽不到就是空串（战报会跳过）
    stamp: str = ""      # 水印时间 "YYYY-MM-DD HH:MM"，抽不到就是空串（判重会跳过）
    members: str = ""    # 水印里的「小组成员」。不给模型看，只用来把
                         # 照片和文字晒单认成同一单（有人会替同事发晒单）
    contact: str = ""    # 客户号码的【哈希】。同一单的多条消息号码一定相同，
                         # 是最可靠的合并依据。只存哈希，原始号码不落盘


def _fmt(y: str, mo: str, d: str, hh: str, mm: str) -> str:
    """拼成统一格式，顺带做范围校验。不合法返回空串。"""
    try:
        y, mo, d, hh, mm = int(y), int(mo), int(d), int(hh), int(mm)
    except (TypeError, ValueError):
        return ""
    if not (1 <= mo <= 12 and 1 <= d <= 31 and 0 <= hh <= 23 and 0 <= mm <= 59):
        return ""
    return f"{y:04d}-{mo:02d}-{d:02d} {hh:02d}:{mm:02d}"


def extract_stamp(clean_text: str) -> str:
    """
    从【已脱敏】的 OCR 文本里抽水印时间。

    传未脱敏的文本进来会抓到身份证有效期，务必先 redact()。
    """
    m = _TIME_FULL.search(clean_text)
    if m:
        got = _fmt(*m.groups())
        if got:
            return got

    # 模板 B：时刻在前、日期在后，且时刻没有标签（15:20 | 2026-09-03）
    m = _TIME_BEFORE_DATE.search(clean_text)
    if m:
        hh, mm, y, mo, d = m.groups()
        got = _fmt(y, mo, d, hh, mm)
        if got:
            return got

    # 日期和时间分开摆、时刻带标签的模板
    d, c = _DATE_ONLY.search(clean_text), _CLOCK_ONLY.search(clean_text)
    if d and c:
        # 两种写法各占两个分组，命中哪对用哪对
        hh, mm = (c.group(1), c.group(2)) if c.group(1) else (c.group(3), c.group(4))
        return _fmt(d.group(1), d.group(2), d.group(3), hh, mm)
    return ""


def extract_branch(ocr_text: str) -> str:
    """从 OCR 全文里抽支局名。抽不到返回空串。"""
    m = _BRANCH_FIELD.search(ocr_text)
    if m:
        return m.group(1)
    m = _BRANCH_SHORT.search(ocr_text)
    if m:
        return re.sub(r"\s", "", m.group(1)) + "支局"
    m = _BRANCH_LOOSE.search(ocr_text)
    return _clean_branch(m.group(1)) if m else ""


def _members_of(raw: str) -> str:
    """抽「小组成员」，OCR 常把名字连在一起，原样留着做子串匹配。"""
    m = _MEMBERS.search(raw or "")
    return re.sub(r"[\s、,，]", "", m.group(1)) if m else ""


def _contact_key(raw: str) -> str:
    """
    客户号码的哈希，用作订单身份。

    同一单的多条消息（照片、文字晒单）号码一定相同，
    号码不同则【一定是两单】—— 这是最硬的合并依据。
    只存哈希：原始号码既不落盘也不外发。
    """
    m = _PHONE.search(raw or "")
    if not m:
        # 固话/宽带账号形如 02334205751，也当号码用
        m = re.search(r"(?<!\d)0\d{9,11}(?!\d)", raw or "")
    return hashlib.sha1(m.group(0).encode()).hexdigest()[:12] if m else ""


def distill(ocr_text: str, max_chars: int = 200) -> Distilled:
    """
    提纯：只留业务字段 + 支局，其余全丢，并抹掉手机号/身份证号。

    优先级：工作内容字段 > 业务描述片段 > 全文（都会先脱敏）。
    """
    if not ocr_text:
        return Distilled("", "空")

    branch = extract_branch(ocr_text)
    clean = redact(ocr_text)
    stamp = extract_stamp(clean)

    # 【必须在抽字段之前抹掉装饰文字】。水印分层渲染，OCR 会把
    # 「今日水印」这类标签读进工作内容中间，不先抹掉的话字段会被拦腰截断，
    # 套餐档位这种关键信息就丢了（实测「139+15小合约」整段没了）。
    # 抹成空格而不是删掉，免得把前后两个词粘成一个。
    clean = _CHROME.sub(" ", clean)

    mem = _MEMBERS.search(clean)
    members = re.sub(r"[\s、,，]", "", mem.group(1)) if mem else ""
    # 客户号码：脱敏【之前】的原文里找，只留哈希
    contact = _contact_key(ocr_text)

    m = _WORK_FIELD.search(clean) or _REMARK_FIELD.search(clean)
    if m:
        value = m.group(1).strip(" ：:，,。.")
        if value:
            return Distilled(f"工作内容：{value}", "工作内容字段",
                             branch, stamp, members, contact)

    m = _WORK_FALLBACK.search(clean)
    if m:
        return Distilled(f"工作内容：{m.group(0).strip()}", "业务片段兜底",
                         branch, stamp, members, contact)

    # 第三种模板：业务内容裸写、没有标签。抓业务动作词所在的片段。
    # 抓之前先把已经单独抽出来的成员名去掉，否则「预约上门 小组成员：王春燕」
    # 这种会把人名一起卷进来。
    hay = clean
    if members:
        # ⚠️ 只去掉成员【这一个字段】，用后视断言卡到下一个标签为止。
        # 写成 .{0,24} 会连后面的业务内容一起删掉 —— 实测
        # 「拍摄人：刘青峰 吴兴国 预约上门 工作单位：…」里的「预约上门」被吃了。
        hay = re.sub(
            r"(?:小组\s*成员|拍摄人)\s*[:：]\s*.{0,24}?"
            r"(?=\s*(?:工作内容|备\s*注|工作单位|支局|时\s*间|地\s*点|"
            r"预约|上门|新增|续约|续签|办理|装机|开通|加装|升级|拆机|维系|"
            r"拦截|甩单|受理|空号|宣传|查询|营销|今日水印|防伪|$))",
            " ", hay)
    m = _WORK_BARE.search(hay)
    if m:
        return Distilled(f"工作内容：{m.group(0).strip()}", "裸写兜底",
                         branch, stamp, members, contact)

    # 什么都没抽到，退回脱敏后的全文，截断
    return Distilled(clean[:max_chars], "全文兜底", branch, stamp, members, contact)

# 纯文字晒单里的单号，形如「8月30日第12单」。
# 这是这类消息最好的判重键：同一单重复贴，单号一定一样。
_ORDER_NO = re.compile(r"(\d{1,2}\s*月\s*\d{1,2}\s*日\s*第\s*\d+\s*单)")


def distill_text(raw: str) -> Distilled:
    """
    处理【纯文字】报单（没有图片那种），比如群里流行的「业务晒单时刻」模板：

        [庆祝]支局：文龙支局
        🚩业务类型:新增
        ✌办理套餐：19山城卡
        ☎️业务号码:1xxxxxxxxxx
        👏号卡数量:1
        🚀揽收人:郭静

    做三件事，和图片那条路保持一致：
      · 脱敏 —— 这类消息【明文带客户手机号】，不能原样发给模型或写进日志
      · 抽支局 —— 不抽的话战报会因为「没识别出支局」被跳过
      · 造判重键 —— 优先用单号；没有单号就用手机号的哈希（只存哈希，不存号码）

    比 OCR 那条路准得多：字段是打好的，不用跟识别错误较劲。
    """
    stamp = ""
    m = _ORDER_NO.search(raw)
    if m:
        stamp = re.sub(r"\s+", "", m.group(1))
    else:
        # 没有单号就退而用手机号 —— 同一单重复贴时号码一定相同。
        # 【只存哈希】，原始号码不落盘也不外发。
        tel = _PHONE.search(raw)
        if tel:
            stamp = "tel:" + hashlib.sha1(tel.group(0).encode()).hexdigest()[:12]

    clean = redact(raw)
    branch = extract_branch(clean)
    return Distilled(text=clean, branch=branch, stamp=stamp, source="文字晒单",
                     members=_members_of(raw), contact=_contact_key(raw))
