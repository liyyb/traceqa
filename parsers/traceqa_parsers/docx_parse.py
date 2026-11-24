"""DOCX 解析：从 OOXML 生成结构树（手册 §5 第 3 条、§5.1）。

定位键 = 文件修订 + 文档部件 + 块路径 + 段落内文字区间（Unicode 码点，
起点包含、终点排除）。表格额外记录行、列及合并关系。
逻辑文本定义：段落内全部 w:t 文本按文档顺序连接，w:tab 记 \\t、w:br 记 \\n。
解析器与前端阅读器共用该定义，保证区间一致。
"""
from __future__ import annotations

import re
import zipfile
from typing import Any, Optional

from lxml import etree

from .locator import CoverageReport, ParsedEvidence, TableContext
from .chunk import normalize_text, split_clauses

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W_NS}


def _w(tag: str) -> str:
    return f"{{{W_NS}}}{tag}"


CLAUSE_RE = re.compile(
    r"^(第[一二三四五六七八九十百零〇0-9]+条"
    r"|[一二三四五六七八九十]+、"
    r"|（[一二三四五六七八九十0-9]+）"
    r"|\([一二三四五六七八九十0-9]+\)"
    r"|\d+[\.、])"
)
CAPTION_RE = re.compile(r"^(附表|附件|标准表|一览表)[一二三四五六七八九十0-9]*\s*[：:]?")
UNIT_RE = re.compile(r"(单位[:：]?\s*[^\s，。；]+|元/[^，。；\s]+|每人每[晚天月年]|每间每[晚天])")


class DocxParseError(Exception):
    pass


def _para_logical_text(p_el) -> tuple[str, list[dict]]:
    """返回段落逻辑文本与带偏移的文本段（供阅读器渲染）。"""
    parts: list[str] = []
    segs: list[dict] = []
    pos = 0

    def walk(el, bold: bool):
        nonlocal pos
        for child in el:
            tag = child.tag
            if tag == _w("t"):
                t = child.text or ""
                if t:
                    segs.append({"t": t, "start": pos, "bold": bold})
                    parts.append(t)
                    pos += len(t)
            elif tag == _w("tab"):
                parts.append("\t")
                pos += 1
            elif tag == _w("br"):
                parts.append("\n")
                pos += 1
            elif tag in (_w("r"), _w("hyperlink"), _w("smartTag")):
                b = bold or child.find(f"{_w('rPr')}/{_w('b')}") is not None
                walk(child, b)

    for r in p_el:
        if r.tag == _w("r"):
            bold = r.find(f"{_w('rPr')}/{_w('b')}") is not None
            walk(r, bold)
        elif r.tag in (_w("hyperlink"), _w("smartTag")):
            walk(r, False)
    return "".join(parts), segs


def _load_styles(zf: zipfile.ZipFile) -> dict[str, dict]:
    """styleId -> {name, outline_level, heading_level}"""
    styles: dict[str, dict] = {}
    try:
        data = zf.read("word/styles.xml")
    except KeyError:
        return styles
    root = etree.fromstring(data)
    for st in root.findall(_w("style")):
        sid = st.get(_w("styleId"))
        if not sid:
            continue
        name_el = st.find(_w("name"))
        name = name_el.get(_w("val")) if name_el is not None else ""
        lvl_el = st.find(f"{_w('pPr')}/{_w('outlineLvl')}")
        outline = int(lvl_el.get(_w("val"))) if lvl_el is not None else None
        heading = 0
        if outline is not None:
            heading = outline + 1
        else:
            m = re.match(r"(?:heading|标题)\s*([1-9])", (name or "").strip(), re.I)
            if m:
                heading = int(m.group(1))
            elif re.fullmatch(r"[1-9]", sid) and "标题" in (name or ""):
                heading = int(sid)
        styles[sid] = {"name": name or sid, "heading_level": heading}
    return styles


def _footnotes(zf: zipfile.ZipFile) -> dict[str, str]:
    notes: dict[str, str] = {}
    try:
        data = zf.read("word/footnotes.xml")
    except KeyError:
        return notes
    root = etree.fromstring(data)
    for fn in root.findall(_w("footnote")):
        fid = fn.get(_w("id")) or ""
        texts = [t.text or "" for t in fn.iter(_w("t"))]
        notes[fid] = "".join(texts).strip()
    return notes


def parse_docx(path: str) -> dict[str, Any]:
    """解析 DOCX，返回 evidence/coverage/tree/footnotes。

    tree 供前端 Word 结构阅读器渲染，与定位契约同源。
    """
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as e:
        raise DocxParseError(f"DOCX 包损坏或不是有效的 .docx：{e}") from e

    with zf:
        names = set(zf.namelist())
        if "word/document.xml" not in names:
            raise DocxParseError("缺少 word/document.xml，无法解析")
        encrypted = any(n.startswith("EncryptionInfo") or n == "EncryptedPackage" for n in names)
        if encrypted:
            raise DocxParseError("文件已加密，请导入未加密版本")

        styles = _load_styles(zf)
        footnotes = _footnotes(zf)
        doc_root = etree.fromstring(zf.read("word/document.xml"))
        body = doc_root.find(_w("body"))
        if body is None:
            raise DocxParseError("document.xml 缺少 body")

        coverage = CoverageReport()
        unsupported: list[str] = []
        for tag, label in (("txbxContent", "文本框"), ("ins", "修订痕迹（插入）"),
                           ("del", "修订痕迹（删除）")):
            if doc_root.iter(_w(tag)) and next(doc_root.iter(_w(tag)), None) is not None:
                unsupported.append(label)

        blocks: list[dict] = []
        flat_paras: list[dict] = []  # 供切块使用

        def para_info(p_el, path: list[int], in_table=False) -> dict:
            text, segs = _para_logical_text(p_el)
            ppr = p_el.find(_w("pPr"))
            style_id = ""
            if ppr is not None:
                ps = ppr.find(_w("pStyle"))
                if ps is not None:
                    style_id = ps.get(_w("val")) or ""
            st = styles.get(style_id, {})
            heading = st.get("heading_level", 0)
            fn_ids = [r.get(_w("id")) for r in p_el.iter(_w("footnoteReference"))]
            info = {
                "type": "p", "path": list(path), "text": text, "segs": segs,
                "style": st.get("name", style_id), "heading_level": heading,
                "footnote_ids": [i for i in fn_ids if i],
            }
            return info

        def table_info(tbl_el, path: list[int]) -> dict:
            rows_out = []
            active_merges = {}
            grid_cols = len(tbl_el.findall(f"{_w('tblGrid')}/{_w('gridCol')}"))
            for ri, tr in enumerate(tbl_el.findall(_w("tr"))):
                row_out = []
                next_merges = {}
                col_cursor = 0
                for tc in tr.findall(_w("tc")):
                    tcpr = tc.find(_w("tcPr"))
                    span = 1
                    vmerge = None
                    if tcpr is not None:
                        gs = tcpr.find(_w("gridSpan"))
                        if gs is not None:
                            span = int(gs.get(_w("val")) or 1)
                        vm = tcpr.find(_w("vMerge"))
                        if vm is not None:
                            vmerge = vm.get(_w("val")) or "continue"
                    paras = []
                    for k, p_el in enumerate(tc.findall(_w("p"))):
                        paras.append(para_info(p_el, path + [ri, col_cursor, k], True))
                    cell_text = "\n".join(p["text"] for p in paras)
                    cell = {
                        "type": "cell", "path": path + [ri, col_cursor],
                        "row": ri, "col": col_cursor,
                        "col_span": span,
                        "vmerge": vmerge,
                        "paras": paras, "text": cell_text, "row_span": 1,
                    }
                    merge_key = (col_cursor, span)
                    if vmerge == "restart":
                        next_merges[merge_key] = cell
                    elif vmerge == "continue" and merge_key in active_merges:
                        anchor = active_merges[merge_key]
                        anchor["row_span"] += 1
                        cell["covered_by"] = anchor["row"]
                        cell["inherited_text"] = anchor["text"]
                        next_merges[merge_key] = anchor
                    row_out.append(cell)
                    col_cursor += span
                rows_out.append(row_out)
                active_merges = next_merges
            return {"type": "tbl", "path": list(path), "rows": rows_out,
                    "grid_cols": grid_cols}

        idx = 0
        for child in body:
            if child.tag == _w("p"):
                info = para_info(child, [idx])
                blocks.append(info)
                flat_paras.append(info)
                idx += 1
            elif child.tag == _w("tbl"):
                tinfo = table_info(child, [idx])
                blocks.append(tinfo)
                idx += 1
            # sectPr 等其余元素跳过

        # ---------------- 切块（标题、条款、表格行） ----------------
        evidences: list[ParsedEvidence] = []
        heading_path: list[tuple[int, str]] = []
        pending_caption: Optional[str] = None
        chunk_no = 0

        def cur_heading_path() -> list[str]:
            return [h[1] for h in heading_path]

        def push_heading(level: int, text: str):
            while heading_path and heading_path[-1][0] >= level:
                heading_path.pop()
            heading_path.append((level, text))

        def emit_para_chunk(paras: list[dict], start: Optional[int] = None,
                            end: Optional[int] = None):
            nonlocal chunk_no
            if not paras:
                return
            text = "\n".join(p["text"] for p in paras if p["text"].strip())
            if not text.strip():
                return
            first = paras[0]
            fn_texts = [footnotes[i] for p in paras for i in p["footnote_ids"]
                        if i in footnotes and footnotes[i]]
            ev = ParsedEvidence(
                chunk_no=chunk_no,
                text_orig=text,
                text_norm=normalize_text(text),
                heading_path=cur_heading_path(),
                locator={
                    "kind": "docx", "part": "document",
                    "block_path": first["path"],
                    # Unicode 码点区间：起点包含、终点排除
                    "text_start": start if start is not None else 0,
                    "text_end": end if end is not None else len(first["text"]),
                },
                meta={"para_paths": [p["path"] for p in paras],
                      "footnotes": fn_texts},
            )
            evidences.append(ev)
            chunk_no += 1

        for blk in blocks:
            if blk["type"] == "p":
                raw_text = blk["text"]
                lead = len(raw_text) - len(raw_text.lstrip())
                text = raw_text.strip()
                lvl = blk["heading_level"]
                if lvl and text:
                    emit_para_chunk([])  # 标题前收束
                    push_heading(lvl, text)
                    pending_caption = None
                    continue
                if not text:
                    continue
                if CAPTION_RE.search(text):
                    pending_caption = text
                    # 题注本身也作为证据，便于“附表一”类引用
                    emit_para_chunk([blk])
                    continue
                # 条款切分
                clauses = split_clauses(text, CLAUSE_RE)
                if len(clauses) > 1:
                    for c_start, c_text in clauses:
                        pseudo = dict(blk)
                        pseudo["text"] = c_text
                        emit_para_chunk([pseudo], lead + c_start,
                                        lead + c_start + len(c_text))
                else:
                    emit_para_chunk([blk])
            else:  # 表格
                table_name = pending_caption or (
                    cur_heading_path()[-1] if cur_heading_path() else None)
                rows = blk["rows"]
                col_headers = [c["text"].strip() for c in rows[0]
                               for _ in range(c["col_span"])] if rows else []
                header_blob = " ".join(col_headers)
                unit_src = header_blob + " " + (pending_caption or "")
                unit_m = UNIT_RE.search(unit_src)
                for ri, row in enumerate(rows):
                    for cell in row:
                        txt = cell["text"].strip()
                        if not txt or "covered_by" in cell:
                            continue
                        row_head = row[0].get("inherited_text", row[0]["text"]).strip()
                        tctx = TableContext(
                            table_name=table_name,
                            row_headers=[row_head] if cell["col"] != 0 else [],
                            col_headers=[col_headers[cell["col"]]]
                            if cell["col"] < len(col_headers) else [],
                            unit=unit_m.group(0) if unit_m else None,
                            footnotes=[],
                        )
                        ev = ParsedEvidence(
                            chunk_no=chunk_no,
                            text_orig=txt,
                            text_norm=normalize_text(txt),
                            heading_path=cur_heading_path(),
                            locator={
                                "kind": "docx", "part": "document",
                                "block_path": blk["path"],
                                "text_start": 0, "text_end": len(txt),
                                "table_row": cell["row"], "table_col": cell["col"],
                                "row_span": cell["row_span"], "col_span": cell["col_span"],
                            },
                            table_ctx=tctx.to_dict(),
                            meta={"cell_path": cell["path"]},
                        )
                        evidences.append(ev)
                        chunk_no += 1
                pending_caption = None

        coverage.evidence_count = len(evidences)
        coverage.unsupported = unsupported
        # Word 参考页数不可靠，留空由导入端按固定字体参考登记
        return {
            "evidences": evidences,
            "coverage": coverage,
            "tree": {"blocks": blocks},
            "footnotes": footnotes,
        }
