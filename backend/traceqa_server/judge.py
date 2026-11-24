"""规则化结论生成与独立校验（手册 §5 第 6、7 条；模型接入前的确定性管线）。

每个子问题独立落入：有据可答 / 需补条件 / 未找到依据 / 待确认冲突 /
材料覆盖不足。校验器对金额、单位、比较关系、否定与限定词做独立核对。
"""
from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Optional

from traceqa_parsers.locator import locator_from_dict

from . import db as DB

# ---------------- 主题词表（可扩展；模型接入后由模型替代） ----------------
TOPICS: dict[str, list[str]] = {
    "住宿": ["住宿", "房费", "住宿标准", "住宿费", "住宿限额"],
    "早餐": ["早餐"],
    "餐补": ["餐补", "伙食补助", "伙食补贴", "餐费"],
    "交通补贴": ["交通补贴", "交通费", "市内交通"],
    "出差审批": ["出差审批", "审批流程"],
}

# 口语说法 → 标准主题词（规则化判断阶段使用，避免口语问句锁定不到条款行）
ORAL_TOPICS: dict[str, list[str]] = {
    "住宿": ["住一晚", "住几晚", "住店", "住宿", "住一宿", "入住", "离店"],
    "餐补": ["吃饭", "餐费", "伙食"],
    "交通补贴": ["打车", "市内交通", "车费"],
}

# 文本里抽实词时剔除的停用词（日期/疑问/量词碎片）
_STOP_WORDS = re.compile(
    r"^(各|分别|各自|按|什么|多少|标准|能报|可以|是否|请问|每月|每人|每晚|每天|"
    r"元|钱|的|了|吗|呢|吧|和|及|与|住|报|报多少|一晚|两晚|几晚)$")


def _content_words(text: str) -> list[str]:
    """从问题文本抽中文实词片段，供主题行匹配兜底。"""
    words = re.findall(r"[\u4e00-\u9fff]{2,}", text)
    return [w for w in words if not _STOP_WORDS.match(w)]


CITY_RE = re.compile(
    r"(上海|北京|广州|深圳|杭州|南京|成都|武汉|西安|重庆|天津|苏州|一类城市|二类城市|三类城市)")
# 出差目的地：介词后紧跟的城市，优先于句首城市（籍贯/出发地）
DEST_CITY_RE = re.compile(
    r"(?:在|去|到|前往|赴|至)\s*(上海|北京|广州|深圳|杭州|南京|成都|武汉|西安|重庆|天津|苏州)")
CLASS_RE = re.compile(
    r"(普通员工|一般员工|一般干部|中层干部|部门负责人|司局级|部级|"
    r"总监级|总监|经理级|主管级|高级经理|实习生)")
FULL_DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?")
YM_DATE_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月(?!\s*\d)")
MD_DATE_RE = re.compile(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]")
# 斜杠日期：2026/9/1（完整）与 9/1（缺年份）。排除后面紧跟"/数字"的分数/比值。
SLASH_FULL_RE = re.compile(r"(\d{4})\s*/\s*(\d{1,2})\s*/\s*(\d{1,2})(?!\s*/\s*\d)")
SLASH_MD_RE = re.compile(r"(?<![\d/])(\d{1,2})\s*/\s*(\d{1,2})(?!\s*/\s*\d)")
EFFECTIVE_RE = re.compile(
    r"自?\s*(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]?\s*起(?:施行|执行|实施|调整|生效)?")
AMOUNT_RE = re.compile(r"(?<![\d.,，．−-])((?:\d{1,3}(?:[,，]\d{3})+|\d+)(?:[.．]\d+)?)\s*元")
# 「限额为 800 元」「恰为 800 元」是确定值，不是「不超过 800 元」的上下限
LE_RE = re.compile(r"(不超过|上限|以内|不得高于|不高于|封顶)")
GE_RE = re.compile(r"(不低于|至少|不得低于)")
DENOM_RE = re.compile(r"(每人每[晚天月年]|每间每[晚天]|人[／/]天|元[／/][晚天])")
REPLACE_RE = re.compile(r"(废止|同时停止执行|以本(?:通知|办法|规定)为准|予以替代|替代原|原.*?同时)")
GAP_RE = re.compile(r"(详见附件\s*[0-9一二三四五六七八九十]*|见附表|标准见附件|详见.*?附件)")
# 地区分类证据：表格按「一类城市」列出标准时，必须另有条文把目标城市映射到该类
REGION_CLASS_RE = re.compile(r"(一类城市|二类城市|三类城市|一类地区|二类地区)")
REGION_LINK_RE = re.compile(r"(属于|列为|归入|划为|对应|包括|适用|执行.*?类)")
UNIT_WORDS = ("元", "美元")

CLAUSE_TITLE_RE = re.compile(r"第([一二三四五六七八九十百0-9]+)条")


@dataclass
class Fact:
    """一条结构化金额事实（手册 §5.1 数值契约）。"""
    evidence_id: str
    revision_id: str
    amount: Decimal
    currency: str = "元"
    comparator: str = "eq"          # le / ge / eq
    denominator: Optional[str] = None
    subject: Optional[str] = None   # 对象（员工类别）
    city: Optional[str] = None
    period: Optional[str] = None
    effective_from: Optional[tuple] = None   # (y, m, d)
    replaces_old: bool = False
    quote: str = ""
    raw_text: str = ""

    def key_amount(self):
        return (self.amount, self.comparator, self.denominator or "")


@dataclass
class SubQuestion:
    topic: str
    text: str
    keywords: list[str] = field(default_factory=list)


def _split_multi_topic(part: str) -> list[str]:
    """一句里并列多个事项时按连接词拆开，前缀条件随第一个事项保留。

    例：“2026年7月20日上海普通员工住宿、餐补和早餐分别能报多少”
    拆成三问；日期/城市/职级只出现一次，由 subquestion_conditions 共享。
    """
    words = [w for words in TOPICS.values() for w in words]
    if len({t for t, words in TOPICS.items() if any(w in part for w in words)}) < 2:
        return [part]
    chunks = [c.strip() for c in re.split(r"[、，,]\s*|(?:和|及|与)", part) if c.strip()]
    out: list[str] = []
    carry = ""
    for chunk in chunks:
        if any(w in chunk for w in words):
            out.append(carry + chunk)
            carry = ""
        else:
            carry += chunk
    if carry:
        if out:
            out[-1] += carry
        else:
            out.append(carry)
    return out or [part]


def _split_multi_date(part: str) -> list[str]:
    """一句里并列多个日期、且问的是「各／分别」时，按日期拆成多问。

    例：“6月30日和7月1日各住一晚，分别按什么标准报？”
    拆成两问，各自带自己的日期；城市/职级等共享条件由 subquestion_conditions 合并。

    只在出现两个以上互不相同的日期、且带并列信号时拆，避免把
    “自2026年7月1日起……”这类单一条款叙述误拆。
    """
    # 完整日期与月日可能混用（“2026年6月30日入住、7月2日离店”），
    # 两类都要收集；被完整日期覆盖的月日片段按重叠丢弃。
    spans = [m.span() for m in FULL_DATE_RE.finditer(part)]
    spans += [m.span() for m in SLASH_FULL_RE.finditer(part)]
    spans += [m.span() for m in MD_DATE_RE.finditer(part)]
    spans += [m.span() for m in SLASH_MD_RE.finditer(part)]
    spans.sort(key=lambda x: x[0])
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s < merged[-1][1]:
            continue
        merged.append((s, e))
    if len(merged) < 2:
        return [part]
    values = [re.sub(r"\s+", "", part[s:e]) for s, e in merged]
    if len(set(values)) < 2:
        return [part]
    if not re.search(r"各|分别|各自|两天|两晚|两种|不同|哪天|哪个|更高|更低|入住|离店", part):
        return [part]
    head = part[:merged[0][0]].strip(" ，,、和及与")
    tail = part[merged[-1][1]:].strip(" ，,、")
    out: list[str] = []
    for i, (s, e) in enumerate(merged):
        # 保留日期后紧跟的语义（入住／离店），否则拆出的子问题丢失条件。
        mid = ""
        if i < len(merged) - 1:
            mid = part[e:merged[i + 1][0]].strip(" ，,、和及与")
        seg = head + part[s:e] + mid
        if tail:
            seg += "，" + tail
        out.append(seg.strip())
    return out or [part]


def _split_multi_city(part: str) -> list[str]:
    """一句里并列多个城市、问“哪个更贵/分别多少”时，按城市拆开各自绑定条件。"""
    spans = [m.span() for m in CITY_RE.finditer(part)]
    if len(spans) < 2:
        return [part]
    values = [re.sub(r"\s+", "", part[s:e]) for s, e in spans]
    if len(set(values)) < 2:
        return [part]
    if not re.search(r"和|与|及|、|分别|哪个|比较|更贵|更高|哪里", part):
        return [part]
    head = part[:spans[0][0]].strip(" ，,、和及与")
    tail = part[spans[-1][1]:].strip(" ，,、")
    out: list[str] = []
    for i, (s, e) in enumerate(spans):
        mid = part[e:spans[i + 1][0]].strip(" ，,、和及与") \
            if i < len(spans) - 1 else ""
        seg = head + part[s:e] + mid
        if tail:
            seg += "，" + tail
        out.append(seg.strip())
    return out or [part]


def _split_paired_date_city(part: str) -> list[str]:
    """“9月1日上海和6月30日北京住宿分别多少钱”按位置配对拆成两问。

    日期与城市各两个且数量相等时，必须先配对拆分；否则先拆日期会把
    前一个城市卷进第一段，再拆城市时复制出“同日期不同城市”的伪事项。
    """
    spans = [m.span() for m in FULL_DATE_RE.finditer(part)]
    spans += [m.span() for m in SLASH_FULL_RE.finditer(part)]
    spans += [m.span() for m in MD_DATE_RE.finditer(part)]
    spans += [m.span() for m in SLASH_MD_RE.finditer(part)]
    spans.sort(key=lambda x: x[0])
    merged: list[tuple[int, int]] = []
    for s, e in spans:
        if merged and s < merged[-1][1]:
            continue
        merged.append((s, e))
    cities = list(CITY_RE.finditer(part))
    if len(merged) != 2 or len(cities) != 2:
        return [part]
    if not re.search(r"各|分别|各自|哪天|哪个|更高|更低", part):
        return [part]
    # 把句子按“日期1…日期2…”切成两段，每段各自只取属于自己的城市：
    # 第 1 段的城市落在 [日期1末, 日期2始)，第 2 段城市落在 [日期2末, 句尾)。
    # 尾部共同的问法（住宿分别多少钱）只保留一次，避免复制出重复事项。
    head = part[:merged[0][0]].strip(" ，,、和及与")
    first_end = merged[1][0]
    second_end = merged[-1][1]
    # 第一段城市在 [日期1末, 日期2始)；第二段城市在 [日期2末, 句尾) 的最前面
    first_mid = part[merged[0][1]:first_end].strip(" ，,、和及与")
    tail = part[second_end:].strip(" ，,、和及与")
    cities = list(CITY_RE.finditer(part))
    cities_by_seg: dict[int, str] = {}
    for c in cities:
        seg = 0 if c.start() < first_end else 1
        cities_by_seg.setdefault(seg, c.group(1))
    # 从各段文本里移除城市词，只留共同问法；共同问法取第一段（去掉其城市）
    first_tail = CITY_RE.sub("", first_mid).strip(" ，,、和及与")
    common = CITY_RE.sub("", tail).strip(" ，,、和及与")
    common = common or first_tail
    # “各住一晚/分别多少”这类尾巴，两段都保留才自然；纯问法只保留一次
    keep_in_both = bool(re.search(r"各住|各报|分别住|按什么标准", first_tail + tail))
    out: list[str] = []
    for i, (s, e) in enumerate(merged):
        seg = head + part[s:e]
        if i in cities_by_seg:
            seg += "，" + cities_by_seg[i]
        if keep_in_both:
            seg += "，" + (first_tail or common)
        elif i == 1 and common:
            seg += "，" + common
        out.append(seg.strip())
    return out


def _split_question_sentence(sentence: str) -> list[str]:
    parts = _split_multi_topic(sentence.strip())
    parts = [p3 for p2 in parts for p3 in _split_paired_date_city(p2)]
    parts = [p3 for p2 in parts for p3 in _split_multi_date(p2)]
    parts = [p4 for p3 in parts for p4 in _split_multi_city(p3)]
    # “2026年6月30日入住、7月2日离店”拆开后，第二个日期省略了年份；
    # 同一句里的日期共享年份，否则后半段会退化成 ----MMDD 而被当成年份未知。
    full_years = {match.group(1) for pattern in (FULL_DATE_RE, SLASH_FULL_RE)
                  for match in pattern.finditer(sentence)}
    if len(parts) > 1 and len(full_years) == 1:
        year = next(iter(full_years))
        patched: list[str] = []
        for p in parts:
            if FULL_DATE_RE.search(p) or SLASH_FULL_RE.search(p):
                patched.append(p)
                continue
            md = MD_DATE_RE.search(p)
            sm = SLASH_MD_RE.search(p)
            if md:
                patched.append(MD_DATE_RE.sub(
                    lambda m, y=year: f"{y}年{m.group(1)}月{m.group(2)}日", p, count=1))
            elif sm:
                patched.append(SLASH_MD_RE.sub(
                    lambda m, y=year: f"{y}年{m.group(1)}月{m.group(2)}日", p, count=1))
            else:
                patched.append(p)
        return patched
    return parts


def split_subquestions(question: str) -> list[SubQuestion]:
    sentences = re.split(r"[？?；;。\r\n]+\s*", question)
    parts = [part for sentence in sentences if sentence.strip()
             for part in _split_question_sentence(sentence)]
    # 多片段时，丢弃“给我一个数”“谢谢”这类无主题/无条件的纯指令片段，
    # 否则会被当成独立事项报“未找到依据”。单片段时保留，避免误删整个问题。
    def _has_substance(text: str) -> bool:
        return bool(
            any(w in text for words in TOPICS.values() for w in words)
            or any(w in text for words in ORAL_TOPICS.values() for w in words)
            or CITY_RE.search(text) or CLASS_RE.search(text)
            or FULL_DATE_RE.search(text) or MD_DATE_RE.search(text)
            or SLASH_FULL_RE.search(text) or SLASH_MD_RE.search(text)
            or AMOUNT_RE.search(text))

    if len(parts) > 1:
        # 反问/否定引语（“不是500元吧？”）是语气片段，不是独立事项
        parts = [p for p in parts
                 if _has_substance(p) and not re.search(r"不是.{0,20}(?:吧|吗|么)", p)] or parts
    out: list[SubQuestion] = []
    for p in parts:
        p = p.strip(" ，,、")
        if not p:
            continue
        topic = None
        kws: list[str] = []
        for t, words in TOPICS.items():
            hit = [w for w in words if w in p]
            if hit:
                topic = t
                kws = hit
                break
        if topic is None:
            # 口语说法归一到已知主题（如“住一晚”→住宿），保证拆分后
            # 仍能用主题词锁定条款行；否则主题词退化为问题里的实词。
            for t, words in TOPICS.items():
                oral = [w for w in ORAL_TOPICS.get(t, []) if w in p]
                if oral:
                    topic = t
                    # 条款原文用标准术语，口语词锁不到行；用主题词本身匹配。
                    kws = list(dict.fromkeys([t] + words[:2]))
                    break
        if topic is None:
            topic = p[:12]
            kws = [w for w in _content_words(p) if len(w) >= 2] or [p[:6]]
        out.append(SubQuestion(topic=topic, text=p, keywords=kws))
    if not out:
        out.append(SubQuestion(topic=question[:12], text=question, keywords=[question[:6]]))
    # 反问/语气片段被丢弃时，其主题词要继承给保留下来的事项，
    # 否则“住宿标准不是500元吧？8月上海按多少报”会丢失“住宿”主题而答不出来。
    global_topics = [t for t, words in TOPICS.items() if any(w in question for w in words)]
    for sq in out:
        if sq.topic not in TOPICS and global_topics:
            t = global_topics[0]
            sq.topic = t
            sq.keywords = list(dict.fromkeys([t] + TOPICS[t][:2] + sq.keywords))
    return out


def extract_conditions(question: str, user_conditions: dict) -> dict:
    conds: dict[str, Any] = {}
    full = FULL_DATE_RE.search(question)
    full_slash = SLASH_FULL_RE.search(question)
    m = full or full_slash
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        # “7月1日之前/以前”是区间倒挂：真正适用的是前一天的标准。
        end = m.end()
        if re.match(r"(?:之?前|以前|之前|号的?前)", question[end:end + 4]):
            try:
                prev = date(y, mo, d) - timedelta(days=1)
                y, mo, d = prev.year, prev.month, prev.day
            except ValueError:
                pass
        conds["travel_date"] = f"{y:04d}-{mo:02d}-{d:02d}"
        conds["date_year_known"] = True
    else:
        my = YM_DATE_RE.search(question)
        if my:
            # “2026年8月”只有年月：以该月首日为代表，足以判断已过7/1生效日。
            conds["travel_date"] = f"{int(my.group(1)):04d}-{int(my.group(2)):02d}-01"
            conds["date_year_known"] = True
        else:
            m2 = MD_DATE_RE.search(question) or SLASH_MD_RE.search(question)
            if m2:
                conds["travel_date"] = f"----{int(m2.group(1)):02d}{int(m2.group(2)):02d}"
                conds["date_year_known"] = False
    # 出差地优先取介词（在/去/到/前往/赴/至）之后的城市：
    # “上海普通员工在北京出差”里上海是籍贯，北京才是出差地。
    dm = DEST_CITY_RE.search(question)
    cm = dm or CITY_RE.search(question)
    if cm:
        conds["city"] = cm.group(1)
    sm = CLASS_RE.search(question)
    if sm:
        conds["employee_class"] = sm.group(1)
    for k, v in (user_conditions or {}).items():
        if v:
            conds[k] = v
            if k == "travel_date" and re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(v)):
                conds["date_year_known"] = True
    return conds


def _parse_iso_date(s: Optional[str]) -> Optional[tuple]:
    if not isinstance(s, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", s):
        return None
    try:
        parsed = date.fromisoformat(s)
    except ValueError:
        return None
    return parsed.year, parsed.month, parsed.day


def subquestion_conditions(subqs: list[SubQuestion], supplied: dict) -> list[dict]:
    """Share an unambiguous condition, keeping explicit conditions local to each item."""
    parsed = [extract_conditions(sq.text, {}) for sq in subqs]
    fields = ("city", "employee_class", "travel_date")
    common = {}
    for key in fields:
        values = {item[key] for item in parsed if key in item}
        if len(values) == 1:
            common[key] = values.pop()
    common.update({key: supplied[key] for key in fields if supplied.get(key)})
    result = []
    for index, item in enumerate(parsed):
        conditions = {**common, **item}
        explicit_date = supplied.get(f"travel_date:{index}")
        if explicit_date:
            conditions["travel_date"] = explicit_date
        elif len(subqs) == 1 and supplied.get("travel_date"):
            # Keep the old single-question API usable for clients that supply an override.
            conditions["travel_date"] = supplied["travel_date"]
        value = conditions.get("travel_date")
        if value:
            conditions["date_year_known"] = _parse_iso_date(value) is not None
        result.append(conditions)
    return result


def _gap_applies(row: sqlite3.Row, conditions: dict) -> bool:
    context = " ".join((row["text_orig"], row["heading_path_json"] or ""))
    cities = set(CITY_RE.findall(context))
    employees = set(CLASS_RE.findall(context))
    city = conditions.get("city")
    employee = conditions.get("employee_class")
    return ((not city or not cities or city in cities or bool(REGION_CLASS_RE.search(context)))
            and (not employee or not employees or employee in employees))


def _extract_facts_from_evidence(row: sqlite3.Row) -> list[Fact]:
    """从单条证据抽取金额事实。"""
    text = row["text_orig"]
    ctx = json.loads(row["table_ctx_json"]) if row["table_ctx_json"] else {}
    facts: list[Fact] = []
    for m in AMOUNT_RE.finditer(text):
        amount = Decimal(unicodedata.normalize("NFKC", m.group(1)).replace(",", ""))
        window = text[max(0, m.start() - 20):min(len(text), m.end() + 12)]
        comparator = "eq"
        if LE_RE.search(window) or LE_RE.search(text[:m.start() + 1][-30:] if m.start() else ""):
            comparator = "le"
        elif GE_RE.search(window):
            comparator = "ge"
        denom = DENOM_RE.search(text) or DENOM_RE.search(ctx.get("unit") or "")
        city = None
        subject = None
        row_headers = ctx.get("row_headers", [])
        col_headers = ctx.get("col_headers", [])
        for h in row_headers + col_headers + [text]:
            cm = CITY_RE.search(h)
            if cm and city is None:
                city = cm.group(1)
            sm = CLASS_RE.search(h)
            if sm and subject is None:
                subject = sm.group(1)
        eff = EFFECTIVE_RE.search(text)
        facts.append(Fact(
            evidence_id=row["id"],
            revision_id=row["revision_id"],
            amount=amount,
            currency="元",
            comparator=comparator,
            denominator=denom.group(1) if denom else (ctx.get("unit") or None),
            subject=subject,
            city=city,
            effective_from=(int(eff.group(1)), int(eff.group(2)), int(eff.group(3))) if eff else None,
            replaces_old=bool(REPLACE_RE.search(text)),
            quote=text.strip(),
            raw_text=text,
        ))
    return facts


def _doc_effective(conn: sqlite3.Connection, revision_id: str) -> Optional[tuple]:
    """文档级生效日期：在整份修订内找“自…起施行/执行”。"""
    rows = conn.execute(
        "SELECT text_orig FROM evidence WHERE revision_id=?", (revision_id,)).fetchall()
    for r in rows:
        m = EFFECTIVE_RE.search(r["text_orig"])
        if m and re.search(r"(施行|执行|实施|生效)", r["text_orig"][m.end() - 2:m.end() + 6]):
            return (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def _revision_meta(conn: sqlite3.Connection, revision_ids: list[str]) -> dict[str, dict]:
    out = {}
    for rid in revision_ids:
        row = conn.execute(
            """SELECT r.id AS rev_id, v.id AS version_id, v.file_name, d.title,
                      d.id AS document_id,
                      v.format, v.sha256
               FROM parse_revisions r JOIN versions v ON v.id = r.version_id
               JOIN documents d ON d.id = v.document_id WHERE r.id=?""", (rid,)).fetchone()
        if row:
            notes = conn.execute(
                "SELECT field, value, source FROM metadata_notes WHERE version_id=?",
                (row["version_id"],)).fetchall()
            out[rid] = {
                "revision_id": rid, "version_id": row["version_id"],
                "document_id": row["document_id"],
                "file_name": row["file_name"], "title": row["title"],
                "format": row["format"], "sha256": row["sha256"],
                "metadata_notes": [dict(n) for n in notes],
                "doc_effective": None,
            }
    for rid in out:
        rows = conn.execute(
            "SELECT * FROM evidence WHERE revision_id=? ORDER BY chunk_no", (rid,)).fetchall()
        texts = [r["text_orig"] or "" for r in rows]
        out[rid]["doc_effective"] = _doc_effective(conn, rid)
        # §2.4 / §5.2：显式替代条款可能单独成行，金额句本身未必带“停止执行”。
        # 修订级保留这个信号，避免标点/换行把替代关系拆散后误判为冲突。
        out[rid]["replacement_clauses"] = [
            (row, _norm(" ".join(texts[max(0, i - 3):i + 2])))
            for i, row in enumerate(rows)
            if REPLACE_RE.search(texts[i])
        ]
    return out


def judge_run(conn: sqlite3.Connection, question: str, user_conditions: dict,
              revision_ids: list[str], candidates: dict[str, list[sqlite3.Row]]
              ) -> tuple[list[dict], dict]:
    """对每个子问题给出终态。返回 (claims, 条件)。

    candidates: sub_question_index -> 检索候选证据行（已限范围）。
    """
    meta = _revision_meta(conn, revision_ids)
    subqs = split_subquestions(question)
    local_conditions = subquestion_conditions(subqs, user_conditions or {})
    claims: list[dict] = []

    for idx, (sq, conds) in enumerate(zip(subqs, local_conditions, strict=True)):
        travel = _parse_iso_date(conds.get("travel_date"))
        entered_date = conds.get("travel_date")
        if entered_date and travel is None and not re.fullmatch(r"----\d{4}", str(entered_date)):
            claims.append({"seq_no": idx, "topic": sq.topic, "question_text": sq.text,
                           "status": DB.CLAIM_NEEDS_CONDITION,
                           "followup": {"missing": "travel_date", "prompt": "请输入有效的出差日期。"}})
            continue
        rows = candidates.get(str(idx), [])
        facts: list[Fact] = []
        for row in rows:
            facts.extend(_extract_facts_from_evidence(row))

        # 主题/城市/对象过滤
        # 只在“该材料本身区分城市/职级”时才要求金额行标注并匹配。
        # 整份材料都不分档（餐补、无标注的同期规定）时，金额视为通用标准；
        # 否则“广州住宿”会套用上海的金额，或把无替代关系的冲突金额误删。
        rev_has_city = {f.revision_id for f in facts if f.city}
        rev_has_class = {f.revision_id for f in facts if f.subject}

        def relevant(f: Fact) -> bool:
            if conds.get("city") and f.revision_id in rev_has_city:
                if not f.city or f.city != conds["city"]:
                    return False
            if conds.get("employee_class") and f.revision_id in rev_has_class:
                if not f.subject or f.subject != conds["employee_class"]:
                    return False
            return True

        facts = [f for f in facts if relevant(f)]
        # 确认式问法（“不低于500元吗”）里金额是用户猜想值：若材料按城市分档
        # 而用户没给城市，不能把各城市档位列成冲突。仅在问句自带金额且完全
        # 无条件时触发，避免误伤“6月30日部门负责人住宿”这类正常缺年份追问。
        _ask_amount = bool(AMOUNT_RE.search(sq.text or ""))
        if (not conds.get("city") and not conds.get("employee_class")
                and not conds.get("travel_date") and _ask_amount):
            cities_in_facts = {f.city for f in facts if f.city}
            if len(cities_in_facts) > 1:
                claim = {"seq_no": idx, "topic": sq.topic, "question_text": sq.text,
                         "status": DB.CLAIM_NO_EVIDENCE,
                         "conclusion_text": "所选材料按城市列出不同标准，"
                                            "请在问题中明确出差城市后再核查。",
                         "groups": []}
                claims.append(claim)
                continue
        # 表格金额及缺失附件都需要与当前事项建立关联。
        keywords = TOPICS.get(sq.topic, sq.keywords + [sq.topic])
        topic_rows = [
            r for r in rows if any(k in " ".join((
                r["text_orig"], r["heading_path_json"] or "", r["table_ctx_json"] or ""
            )) for k in keywords if k)
        ]
        kw_rows = {r["id"] for r in topic_rows}
        topic_facts = [f for f in facts if f.evidence_id in kw_rows]
        gap_hits = [r for r in topic_rows
                    if GAP_RE.search(r["text_orig"]) and _gap_applies(r, conds)]
        claim = _decide(conn, sq, conds, travel, topic_facts, gap_hits, rows, meta,
                        topic_rows)
        claim["seq_no"] = idx
        claims.append(claim)

    return claims, local_conditions[0]


def _topic_label(sq: SubQuestion) -> str:
    """结论文案里的主语：已知主题用短名，否则用完整问题文本，绝不截断。"""
    if sq.topic in TOPICS:
        return sq.topic
    return sq.text or sq.topic


def scope_example(conn: sqlite3.Connection, revision_ids: list[str]) -> str | None:
    """Build a short question from an indexed clause in the requested scope."""
    if not revision_ids:
        return None
    marks = ",".join("?" for _ in revision_ids)
    rows = conn.execute(
        f"""SELECT e.*, d.title FROM evidence e
            JOIN parse_revisions r ON r.id=e.revision_id AND r.status='ready'
            JOIN versions v ON v.id=r.version_id JOIN documents d ON d.id=v.document_id
            WHERE e.revision_id IN ({marks}) AND length(trim(e.text_orig)) > 0
            ORDER BY random() LIMIT 64""", revision_ids).fetchall()
    for row in rows:
        context = " ".join((row["text_orig"], row["heading_path_json"] or "",
                            row["table_ctx_json"] or ""))
        topics = [name for name, words in TOPICS.items() if any(word in context for word in words)]
        facts = _extract_facts_from_evidence(row)
        if len(topics) != 1 or not facts or _is_meta_line(row["text_orig"]):
            continue
        fact = facts[0]
        when = fact.effective_from or _doc_effective(conn, row["revision_id"])
        prefix = f"{when[0]}年{when[1]}月{when[2]}日，" if when else ""
        who = f"{fact.city or ''}{fact.subject or ''}"
        return f"{prefix}{who + '的' if who else ''}{topics[0]}标准是多少？"
    for row in rows:
        text = re.sub(r"\s+", " ", row["text_orig"]).strip()
        if _is_meta_line(text) or len(text) < 8:
            continue
        headings = json.loads(row["heading_path_json"] or "[]")
        title = row["title"][:38]
        part = headings[-1][:38] if headings else text[:24].rstrip("，、。；")
        return f"《{title}》中「{part}」有哪些规定？"
    return None


_META_LINE_RE = re.compile(
    r"^\s*(Department|Approval|Review|Policy\s*No|Issue\s*Date|Effective\s*Date|"
    r"Record\s*No|Attachment|Version|Note|编制|审核|批准|会签)\s*[:：]", re.I)


def _is_meta_line(text: str) -> bool:
    """页眉/封面里的元信息行（英文名 + 冒号 + 编号/日期），不是制度条款。"""
    return bool(_META_LINE_RE.match(text or ""))


def _region_mapping_required(conds: dict, rows: list) -> bool:
    """§5.2：使用「一类城市」表格时，抽走地区分类证据必须失去确定状态。

    目标城市与分类名不同（例如上海 vs 一类城市）时，候选里必须有一条
    明确把该城市归入该分类的条文；否则不能给出确定金额。
    """
    city = conds.get("city")
    if not city or REGION_CLASS_RE.search(city):
        return False
    has_class = any(REGION_CLASS_RE.search(
        (r["text_orig"] or "") + " " + (r["table_ctx_json"] or ""))
        for r in rows)
    if not has_class:
        return False
    for r in rows:
        t = r["text_orig"] or ""
        if city in t and REGION_CLASS_RE.search(t) and REGION_LINK_RE.search(t):
            return False
    return True


def _decide(conn, sq: SubQuestion, conds: dict, travel: Optional[tuple],
            facts: list[Fact], gap_hits: list, rows: list, meta: dict,
            topic_rows: Optional[list] = None) -> dict:
    base = {"topic": sq.topic, "question_text": sq.text}

    for row in gap_hits:
        ref_name = _guess_attachment_name(row["text_orig"])
        revision_id = row["revision_id"]
        if ref_name and not _attachment_supports(conn, revision_id, ref_name, facts, meta):
            return {**base, "status": DB.CLAIM_COVERAGE_GAP,
                    "conclusion_text": f"材料覆盖不足：已读条款指向{ref_name}，未找到适用的附件依据。",
                    "gap": {"location": _human_location(row, meta),
                            "clause": row["text_orig"].strip(),
                            "attachment": ref_name,
                            "revision_id": revision_id,
                            "file_name": meta.get(revision_id, {}).get("file_name", "")},
                    "groups": [{"relation": "related", "items": [_ev_item(row, meta)]}]}

    if not facts:
        # 无金额但有主题相关文本 → 依据不足；完全无 → 未找到
        # 只展示与主题真正相关的行，且排除页眉/封面里的英文元信息行
        # （Department:/Approval: 等），避免“未找到依据”却挂着无关引用。
        related = [r for r in (topic_rows or [])
                   if not _is_meta_line(r["text_orig"] or "")][:3]
        conclusion = "所选材料未找到该事项的明确规定。"
        if related:
            return {**base, "status": DB.CLAIM_NO_EVIDENCE,
                    "conclusion_text": conclusion,
                    "groups": [{"relation": "related",
                                "items": [_ev_item(r, meta) for r in related]}]}
        return {**base, "status": DB.CLAIM_NO_EVIDENCE,
                "conclusion_text": conclusion, "groups": []}

    # §5.2：缺少地区分类依据时，不允许把按类别列出的标准当作确定结论
    if _region_mapping_required(conds, rows):
        return {**base, "status": DB.CLAIM_NEEDS_CONDITION,
                "conclusion_text": None,
                "followup": {"missing": "region_class",
                             "prompt": f"所选材料按地区分类列出{conds.get('city', '')}"
                                       f"适用的标准，但未找到该地区所属分类的条文，"
                                       f"暂不能给出确定金额。"},
                "groups": [{"relation": "related",
                            "items": [_ev_item(r, meta) for r in rows[:3]]}]}

    # 生效期过滤
    # 缺年份的月日（----MMDD）不能用于判定生效期：生效日是完整年月日，
    # “9月1日”可能是生效前一年，也可能是生效当年。只要标准受生效日约束，
    # 就必须追问年份，绝不能按月日盲判给出确定金额（否则 9/1 会误答尚未生效的标准）。
    raw_date = str(conds.get("travel_date") or "")
    missing_year = travel is None and re.fullmatch(r"----\d{4}", raw_date) is not None
    md = raw_date[4:] if missing_year else ""
    applicable: list[Fact] = []
    boundary_facts: list[Fact] = []
    for f in facts:
        eff = f.effective_from or meta.get(f.revision_id, {}).get("doc_effective")
        f.effective_from = f.effective_from or (
            meta.get(f.revision_id, {}).get("doc_effective"))
        if travel is not None and f.effective_from and f.effective_from > travel:
            boundary_facts.append(f)   # 完整日期晚于标准生效日之前 → 尚未生效
            continue
        applicable.append(f)

    if missing_year and any(f.effective_from for f in applicable + boundary_facts):
        # 缺年份 + 标准受生效日约束 → 不同年份结论可能不同，统一追问年份。
        eff = next((f.effective_from for f in applicable + boundary_facts
                    if f.effective_from), None)
        return {**base, "status": DB.CLAIM_NEEDS_CONDITION,
                "conclusion_text": None,
                "followup": {"missing": "travel_year",
                             "month_day": f"{md[:2]}-{md[2:]}",
                             "prompt": f"年份影响结果，请明确出差年份。"
                                       f"相关标准自{_fmt_date(eff)}起执行。"}}

    if not applicable and boundary_facts:
        # 已有完整日期但早于所有标准生效日：给确定结论，避免反复追问（死循环）。
        f = boundary_facts[0]
        return {**base, "status": DB.CLAIM_NO_EVIDENCE,
                "conclusion_text": f"所选出差日期早于现有标准生效日"
                                   f"（{_fmt_date(f.effective_from)}），所选材料中无适用标准。",
                "groups": []}

    if travel is None and len({f.key_amount() for f in applicable}) > 1 and \
            any(f.effective_from for f in applicable):
        # 完全未指定日期却有多版标准：说明分界日，请用户给出日期。
        dates = sorted({f.effective_from for f in applicable if f.effective_from})
        if len(dates) >= 2:
            b = dates[-1]
            return {**base, "status": DB.CLAIM_NEEDS_CONDITION,
                    "conclusion_text": None,
                    "followup": {"missing": "travel_date",
                                 "prompt": f"两版标准以 {_fmt_date(b)} 为界。你的出差日期是哪天？"}}

    # 金额归组
    groups: dict[tuple, list[Fact]] = {}
    for f in applicable:
        groups.setdefault(f.key_amount(), []).append(f)

    if len(groups) == 1:
        f = applicable[0]
        return _answered(base, sq, conds, f, meta)

    # 多个金额：判定替代/更新关系
    resolution = _resolve_priority(applicable, travel, meta)
    if resolution is not None:
        winner, clauses = resolution
        result = _answered(base, sq, conds, winner, meta,
                           superseded=[f for f in applicable if f is not winner])
        result["groups"].append({"relation": "related",
                                 "items": [_ev_item(row, meta) for row in clauses]})
        return result

    # 无法判定优先关系 → 待确认冲突，双方证据并列
    items_groups = []
    for key, fl in groups.items():
        items = []
        for f in fl[:2]:
            row = _row_by_id(rows, f.evidence_id)
            if row is not None:
                items.append(_ev_item(row, meta, quote=f.quote))
        items_groups.append({"relation": "conflict", "items": items})
    amounts_txt = "、".join(f"{f.amount} 元" for f in (fl[0] for fl in groups.values()))
    return {**base, "status": DB.CLAIM_CONFLICT,
            "conclusion_text": f"{_topic_label(sq)}标准存在待确认冲突：所选材料分别规定 {amounts_txt}，"
                               f"优先关系无法判定。",
            "groups": items_groups}


def _resolve_priority(facts: list[Fact], travel: Optional[tuple], meta: dict):
    """只有能追溯到替代条款的不同金额才可自动裁决。"""
    if travel is None:
        return None
    resolutions = []
    for candidate in facts:
        if not candidate.effective_from or candidate.effective_from > travel:
            continue
        info = meta[candidate.revision_id]
        proof = []
        competitors = [old for old in facts if old.key_amount() != candidate.key_amount()]
        for old in competitors:
            if old.effective_from and old.effective_from >= candidate.effective_from:
                break
            old_info = meta[old.revision_id]
            same_document = info["document_id"] == old_info["document_id"]
            old_title = re.sub(r"^\d+[-_、 ]+", "", old_info["title"] or "")
            old_title = _norm(re.sub(r"[（(].*?[）)]$", "", old_title))
            clause = next((row for row, context in info["replacement_clauses"]
                           if same_document or (old_title and f"《{old_title}》" in context)), None)
            if clause is None:
                break
            if not any(row["id"] == clause["id"] for row in proof):
                proof.append(clause)
        else:
            if proof:
                resolutions.append((candidate, proof))
    if len({candidate.key_amount() for candidate, _ in resolutions}) == 1:
        return resolutions[0]
    return None


def _answered(base, sq, conds, f: Fact, meta, superseded: list[Fact] = None) -> dict:
    cond_txt = []
    if conds.get("city"):
        cond_txt.append(conds["city"])
    if conds.get("employee_class"):
        cond_txt.append(conds["employee_class"])
    if conds.get("travel_date") and conds.get("date_year_known"):
        cond_txt.append(conds["travel_date"])
    comp_txt = {"le": "不超过", "ge": "不低于", "eq": "为"}[f.comparator]
    denom = f.denominator
    unit = f"元／{denom}" if denom else "元"
    concl = (f"{_topic_label(sq)}：{'、'.join(cond_txt) if cond_txt else '所选范围'}"
             f"的标准{comp_txt} {f.amount} {unit}。")
    if f.effective_from:
        concl += f"（{_fmt_date(f.effective_from)}起执行）"
    row_items = []
    ev_row = f._row if hasattr(f, "_row") else None
    items = [{"evidence_id": f.evidence_id, "revision_id": f.revision_id,
              "quote": f.quote, "doc": meta.get(f.revision_id, {}).get("title", "")}]
    groups = [{"relation": "support", "items": items}]
    if superseded:
        for s in superseded[:1]:
            groups.append({"relation": "related", "items": [{
                "evidence_id": s.evidence_id, "revision_id": s.revision_id,
                "quote": s.quote,
                "doc": meta.get(s.revision_id, {}).get("title", "")}]})
    return {**base, "status": DB.CLAIM_ANSWERED,
            "conclusion_text": concl,
            "conditions": {k: _human_condition(k, conds[k]) for k in
                           ("city", "employee_class", "travel_date")
                           if conds.get(k) and _human_condition(k, conds[k])},
            "amount": str(f.amount), "comparator": f.comparator,
            "denominator": denom,
            "groups": groups}


def _ev_item(row: sqlite3.Row, meta: dict, quote: Optional[str] = None) -> dict:
    return {"evidence_id": row["id"], "revision_id": row["revision_id"],
            "quote": (quote or row["text_orig"]).strip(),
            "doc": meta.get(row["revision_id"], {}).get("title", "")}


def _row_by_id(rows: list, evidence_id: str):
    for r in rows:
        if r["id"] == evidence_id:
            return r
    return None


def _human_location(row: sqlite3.Row, meta: dict) -> str:
    loc = json.loads(row["locator_json"])
    doc = meta.get(row["revision_id"], {}).get("title", "")
    heading = " › ".join(json.loads(row["heading_path_json"] or "[]"))
    if loc.get("kind") == "pdf":
        page = loc.get("page_index", 0) + 1
        return f"{doc} · 物理第 {page} 页" + (f" · {heading}" if heading else "")
    if loc.get("table_row") is not None:
        return f"{doc} · {heading} · 表 行{loc['table_row'] + 1}／列{loc['table_col'] + 1}"
    return f"{doc} · {heading}" if heading else doc


def _guess_attachment_name(text: str) -> Optional[str]:
    m = re.search(r"附件\s*[0-9一二三四五六七八九十]*\s*[《]([^》]+)[》]", text)
    if m:
        return m.group(1)
    m = re.search(r"(附件\s*[0-9一二三四五六七八九十]*)", text)
    return m.group(1) if m else None


def _attachment_supports(conn, revision_id: str, name: str,
                         facts: list[Fact], meta: dict) -> bool:
    fact_revisions = {fact.revision_id for fact in facts} - {revision_id}
    for fact_revision in fact_revisions:
        material = meta.get(fact_revision, {})
        if any(name in (material.get(key) or "") for key in ("title", "file_name")):
            return True
    if not fact_revisions:
        return False
    links = conn.execute(
        "SELECT gap_json, supplement_revision_id FROM supplements "
        "WHERE original_revision_id=? AND status='linked'", (revision_id,))
    return any(link["supplement_revision_id"] in fact_revisions
               and json.loads(link["gap_json"]).get("attachment") == name
               for link in links)


def _fmt_date(d: Optional[tuple]) -> str:
    if not d:
        return ""
    return f"{d[0]} 年 {d[1]} 月 {d[2]} 日"


def _human_condition(key: str, value) -> str:
    """展示给用户的条件文本；内部占位日期（----MMDD）不外露。"""
    text = str(value)
    if key == "travel_date":
        parsed = _parse_iso_date(text)
        if parsed:
            return _fmt_date(parsed)
        m = re.fullmatch(r"----(\d{2})(\d{2})", text)
        if m:
            return f"{int(m.group(1))}月{int(m.group(2))}日（年份待确认）"
        return ""  # 无法识别的编码一律不外露
    return text


# ---------------------------------------------------------------- 校验器

def verify_claim(claim: dict, conn: sqlite3.Connection,
                 scope_revision_ids: set[str]) -> tuple[bool, str]:
    """独立校验（手册 §5 第 6 条）：ID 在范围内、引用可解析、原句确实存在、
    金额/单位/比较关系一致。"""
    support = set()
    conflict_groups = []
    for group in claim.get("groups", []):
        amounts = set()
        for item in group.get("items", []):
            eid = item.get("evidence_id")
            row = conn.execute("SELECT * FROM evidence WHERE id=?", (eid,)).fetchone()
            if row is None or row["revision_id"] not in scope_revision_ids:
                return False, "引用不存在或不在所选范围内"
            if item.get("revision_id", row["revision_id"]) != row["revision_id"]:
                return False, "引用修订不一致"
            quote = _norm(item.get("quote") or "")
            if not quote or quote not in _norm(row["text_orig"]):
                return False, "摘录与原文不一致"
            try:
                locator_from_dict(json.loads(row["locator_json"]))
            except (ValueError, TypeError, KeyError):
                return False, "原文定位无效"
            if group["relation"] in {"support", "conflict"}:
                amounts.update(fact.key_amount() for fact in _extract_facts_from_evidence(row)
                               if _norm(fact.quote) in quote)
                if group["relation"] == "support":
                    support.add(eid)
        if group["relation"] == "conflict":
            conflict_groups.append(amounts)
    if claim["status"] == DB.CLAIM_CONFLICT:
        if (len(conflict_groups) < 2 or any(len(group) != 1 for group in conflict_groups)
                or len(set.union(*conflict_groups)) != len(conflict_groups)):
            return False, "冲突各侧缺少能区分主张的证据"
    if claim["status"] == DB.CLAIM_ANSWERED:
        if not support:
            return False, "缺少主证据"
        if claim.get("amount") is not None:
            expected = (Decimal(claim["amount"]), claim["comparator"],
                        claim.get("denominator") or "")
            for item in (i for g in claim["groups"] if g["relation"] == "support" for i in g["items"]):
                row = conn.execute("SELECT * FROM evidence WHERE id=?", (item["evidence_id"],)).fetchone()
                if any(fact.key_amount() == expected and _norm(fact.quote) in _norm(item["quote"])
                       for fact in _extract_facts_from_evidence(row)):
                    break
            else:
                return False, "金额、比较关系或计费单位未与主证据匹配"
    return True, ""



def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s)
