"""切块与规范化（手册 §5 第 4 条）：原文和检索用规范化文本分别保存，引用永远取原文。"""
from __future__ import annotations

import re
import unicodedata


def normalize_text(text: str) -> str:
    """规范化文本：NFKC、空白折叠、全角转半角（仅标点/数字/字母宽度形式）。"""
    t = unicodedata.normalize("NFKC", text)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def split_clauses(text: str, clause_re: re.Pattern) -> list[tuple[int, str]]:
    """把长段落按条款标记切分，返回 (段内起点, 条款文本)。

    无标记或仅一段时返回整段。
    """
    # 条款正则以 ^ 锚定，finditer 只能命中段首。这里去掉行首锚点再扫描，
    # 才能把同一段落里连续的「第一条…第二条…」拆成各自带段内偏移的块。
    pattern = clause_re.pattern[1:] if clause_re.pattern.startswith("^") \
        else clause_re.pattern
    scan = re.compile(pattern, clause_re.flags)
    marks = [m for m in scan.finditer(text) if m.start() > 0]
    if not marks:
        return [(0, text)]
    out: list[tuple[int, str]] = []
    starts = [0] + [m.start() for m in marks]
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else len(text)
        raw = text[s:e]
        # 去掉前导空白后，起点要同步右移，保证定位键仍是原始逻辑文本中的码点区间
        lead = len(raw) - len(raw.lstrip())
        seg = raw.strip()
        if seg:
            out.append((s + lead, seg))
    return out


def is_clause_start(text: str, clause_re: re.Pattern) -> bool:
    return bool(clause_re.match(text.strip()))
