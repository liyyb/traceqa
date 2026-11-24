"""PDF 解析回退后端：只用 PyMuPDF，无模型、无网络（手册 §1 CPU 路径）。

与 Docling 后端写入同一份定位契约：
  - 物理页序号 + 未旋转可见裁切区域的归一化证据框（左上原点，右下 (1,1)）
  - 印刷页码探测后写入 locator.printed_page
  - 表格单元携带表名、行列标题、单位
后端名称写入 parse_revisions.parser_version，历史引用始终指向当时的修订。
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

from .locator import CoverageReport, ParsedEvidence, PdfBox, TableContext
from .chunk import normalize_text, split_clauses

CLAUSE_RE = re.compile(
    r"^(第[一二三四五六七八九十百零〇0-9]+条"
    r"|[一二三四五六七八九十]+、"
    r"|（[一二三四五六七八九十0-9]+）"
    r"|\([一二三四五六七八九十0-9]+\)"
    r"|\d+[\.、])"
)
CAPTION_RE = re.compile(r"^(附表|附件|标准表|一览表)[一二三四五六七八九十0-9]*\s*[：:]?")
UNIT_RE = re.compile(r"(单位[:：]?\s*[^\s，。；]+|元/[^，。；\s]+|元／[^，。；\s]+|每人每[晚天月年]|每间每[晚天])")
PAGE_NO_RE = re.compile(r"^\s*(?:第\s*)?([0-9]{1,4})\s*(?:页|／|/)?\s*$")
HEADING_RE = re.compile(r"^(第[一二三四五六七八九十百0-9]+[条章节目]|[一二三四五六七八九十]+、)")


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


class _Geom:
    """页面几何：裁切框（PyMuPDF 左上原点）与旋转变换。"""

    def __init__(self, page):
        self.width = page.rect.width
        self.height = page.rect.height
        self.rotation = page.rotation
        crop = page.cropbox
        self.crop = (crop.x0, crop.y0, crop.x1, crop.y1)
        self.derot = page.derotation_matrix
        self.printed_page: Optional[str] = None

    def norm(self, rect) -> PdfBox:
        """显示坐标矩形 → 未旋转裁切区域归一化框。"""
        fitz_mod = _import_fitz()
        r = fitz_mod.Rect(rect) * self.derot
        x0, y0, x1, y1 = self.crop
        w = max(x1 - x0, 1e-6)
        h = max(y1 - y0, 1e-6)
        rx0, rx1 = min(r.x0, r.x1), max(r.x0, r.x1)
        ry0, ry1 = min(r.y0, r.y1), max(r.y0, r.y1)
        return PdfBox(
            x0=_clamp01((rx0 - x0) / w),
            y0=_clamp01((ry0 - y0) / h),
            x1=_clamp01((rx1 - x0) / w),
            y1=_clamp01((ry1 - y0) / h),
        )


def _overlaps(a, b) -> bool:
    """两个显示坐标矩形是否明显重叠。"""
    return not (a.x1 < b.x0 or b.x1 < a.x0 or a.y1 < b.y0 or b.y1 < a.y0)


def _import_fitz():
    """兼容 PyMuPDF 1.x/2.x：优先新包名 pymupdf。"""
    try:
        import pymupdf as fitz
    except ImportError:  # pragma: no cover
        import fitz  # type: ignore[no-redef]
    return fitz


def parse_pdf_fitz(path: str) -> dict[str, Any]:
    try:
        fitz = _import_fitz()
    except ImportError as e:  # pragma: no cover - 依赖缺失时由上层提示
        raise RuntimeError("缺少 PyMuPDF，无法解析 PDF") from e

    doc = fitz.open(path)
    geoms: list[_Geom] = []
    try:
        page_count = doc.page_count
        for page in doc:
            geoms.append(_Geom(page))

        coverage = CoverageReport(total_pages=page_count)
        evidences: list[ParsedEvidence] = []
        heading_path: list[tuple[int, str]] = []
        pending_caption: Optional[str] = None
        pages_with_text: set[int] = set()
        chunk_no = 0

        def cur_path() -> list[str]:
            return [h[1] for h in heading_path]

        def push_heading(level: int, text: str):
            while heading_path and heading_path[-1][0] >= level:
                heading_path.pop()
            heading_path.append((level, text))

        def emit_text(text: str, g: _Geom, page_index: int, boxes: list[PdfBox],
                      footnotes: Optional[list[str]] = None):
            nonlocal chunk_no
            if not text.strip():
                return
            loc: dict[str, Any] = {
                "kind": "pdf", "page_index": page_index,
                "boxes": [b.to_dict() for b in boxes],
                "raw": {"backend": "fitz", "boxes": [b.to_dict() for b in boxes]},
            }
            if g.printed_page:
                loc["printed_page"] = g.printed_page
            evidences.append(ParsedEvidence(
                chunk_no=chunk_no,
                text_orig=text.strip(),
                text_norm=normalize_text(text),
                heading_path=cur_path(),
                locator=loc,
                page_no=page_index + 1,
                meta={"footnotes": footnotes or [], "backend": "fitz"},
            ))
            pages_with_text.add(page_index)
            chunk_no += 1

        for pi, page in enumerate(doc):
            g = geoms[pi]
            data = page.get_text("dict")

            # ---- 印刷页码探测：页眉/页脚区域的独立数字行 ----
            for blk in data.get("blocks", []):
                if blk.get("type") != 0:
                    continue
                for line in blk.get("lines", []):
                    txt = "".join(s.get("text", "") for s in line.get("spans", [])).strip()
                    m = PAGE_NO_RE.match(txt)
                    if not m:
                        continue
                    r = fitz.Rect(line["bbox"])
                    if r.y0 < g.height * 0.12 or r.y1 > g.height * 0.88:
                        g.printed_page = m.group(1)
                        break
                if g.printed_page:
                    break

            # ---- 表格（有边框/对齐线时）----
            table_rects: list[fitz.Rect] = []
            try:
                finder = page.find_tables()
                tables = list(finder.tables)
            except Exception:
                tables = []
            for tab in tables:
                tb = fitz.Rect(tab.bbox)
                table_rects.append(tb)
                try:
                    cells = tab.extract()
                except Exception:
                    cells = []
                if not cells:
                    continue
                n_rows = len(cells)
                n_cols = max(len(r) for r in cells)
                col_headers = [str(c or "").strip() for c in cells[0]] if n_rows else []
                unit_m = UNIT_RE.search(" ".join(col_headers) + " " + (pending_caption or ""))
                for ri, row in enumerate(cells):
                    row_head = str(row[0] or "").strip() if row else ""
                    for ci in range(n_cols):
                        txt = str(row[ci] or "").strip() if ci < len(row) else ""
                        if not txt:
                            continue
                        rect = None
                        try:
                            rect = tab.rows[ri].cells[ci]
                        except (IndexError, AttributeError):
                            rect = None
                        if rect is None:
                            rect = fitz.Rect(
                                tb.x0 + (tb.x1 - tb.x0) * ci / n_cols,
                                tb.y0 + (tb.y1 - tb.y0) * ri / n_rows,
                                tb.x0 + (tb.x1 - tb.x0) * (ci + 1) / n_cols,
                                tb.y0 + (tb.y1 - tb.y0) * (ri + 1) / n_rows,
                            )
                        box = g.norm(rect)
                        ev = ParsedEvidence(
                            chunk_no=chunk_no,
                            text_orig=txt,
                            text_norm=normalize_text(txt),
                            heading_path=cur_path(),
                            locator={"kind": "pdf", "page_index": pi,
                                     "boxes": [box.to_dict()],
                                     "raw": {"backend": "fitz", "grid": [ri, ci]}},
                            table_ctx=TableContext(
                                table_name=pending_caption or (
                                    cur_path()[-1] if cur_path() else None),
                                row_headers=[row_head] if ci != 0 and row_head else [],
                                col_headers=[col_headers[ci]]
                                if ci < len(col_headers) and col_headers[ci] else [],
                                unit=unit_m.group(0) if unit_m else None,
                                footnotes=[],
                            ).to_dict(),
                            page_no=pi + 1,
                            meta={"backend": "fitz"},
                        )
                        if g.printed_page:
                            ev.locator["printed_page"] = g.printed_page
                        evidences.append(ev)
                        pages_with_text.add(pi)
                        chunk_no += 1
                pending_caption = None

            # ---- 正文文本块 ----
            for blk in data.get("blocks", []):
                if blk.get("type") != 0:
                    continue
                brect = fitz.Rect(blk["bbox"])
                if any(_overlaps(brect, tr) for tr in table_rects):
                    continue
                lines: list[tuple[str, fitz.Rect, float]] = []
                for line in blk.get("lines", []):
                    txt = "".join(s.get("text", "") for s in line.get("spans", [])).strip()
                    if not txt:
                        continue
                    size = max((float(s.get("size", 12)) for s in line.get("spans", [])),
                               default=12.0)
                    lines.append((txt, fitz.Rect(line["bbox"]), size))
                if not lines:
                    continue
                # 页眉页脚的孤立页码不作为正文证据
                if len(lines) == 1 and PAGE_NO_RE.match(lines[0][0]):
                    continue
                para = "".join(t for t, _, _ in lines)
                boxes = [g.norm(r) for _, r, _ in lines]
                sizes = [s for _, _, s in lines]
                max_size = max(sizes)

                if CAPTION_RE.search(para):
                    pending_caption = para
                    # 题注本身也作为证据，便于「附表一」类引用；同时作为后续
                    # 表格/金额行的标题路径，让不含主题词的单元格仍能归属主题。
                    emit_text(para, g, pi, boxes)
                    push_heading(2, para)
                    continue
                if len(lines) == 1 and max_size >= 15 and not CLAUSE_RE.match(para):
                    push_heading(1, para)
                    pages_with_text.add(pi)
                    continue
                if len(lines) == 1 and HEADING_RE.match(para) and max_size >= 13:
                    push_heading(2, para)
                    pages_with_text.add(pi)
                    continue

                clauses = split_clauses(para, CLAUSE_RE)
                if len(clauses) > 1:
                    for _, c_text in clauses:
                        emit_text(c_text, g, pi, boxes)
                else:
                    emit_text(para, g, pi, boxes)

        # 扫描页判定：完全没有文本层的页
        skipped: list[dict] = []
        missing = [i for i in range(page_count) if i not in pages_with_text]
        if missing:
            runs: list[list[int]] = []
            for p in missing:
                if runs and p == runs[-1][-1] + 1:
                    runs[-1].append(p)
                else:
                    runs.append([p])
            for r in runs:
                skipped.append({"pages": [p + 1 for p in r], "reason": "无文本层（扫描页）"})
        coverage.skipped = skipped
        coverage.covered_pages = page_count - len(missing)
        coverage.evidence_count = len(evidences)

        page_geom_out = [{
            "page_index": i, "width": g.width, "height": g.height,
            "rotation": g.rotation, "printed_page": g.printed_page,
            "crop": list(g.crop),
        } for i, g in enumerate(geoms)]

        return {
            "evidences": evidences,
            "coverage": coverage,
            "page_count": page_count,
            "page_geom": page_geom_out,
            "backend": "fitz",
        }
    finally:
        doc.close()
