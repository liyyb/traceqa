"""PDF 解析：Docling 布局与表格 + 定位契约（手册 §5 第 2 条、§5.1）。

坐标契约：证据框统一到未旋转的可见裁切区域，左上为原点、右下为 (1,1)。
Docling 的 bbox 为 PDF 用户坐标（左下原点），本模块借助 PyMuPDF 的页面几何
（裁切框、旋转角）做一次变换；原始解析器坐标保留在 raw 供诊断。
"""
from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
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
UNIT_RE = re.compile(r"(单位[:：]?\s*[^\s，。；]+|元/[^，。；\s]+|每人每[晚天月年]|每间每[晚天])")
PAGE_NO_RE = re.compile(r"^\s*(?:第\s*)?([0-9]{1,4})\s*(?:页|／|/)?\s*$")


class PdfParseError(Exception):
    pass


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def _bootstrap_artifacts() -> Optional[Path]:
    """模型缓存引导（手册 §8 离线分发）：

    DOCLING_ARTIFACTS_PATH 指向数据目录。首次为空时改用默认缓存联网下载，
    成功后复制回数据目录；此后完全离线。返回需要回填的目录（或 None）。
    """
    target = os.environ.get("DOCLING_ARTIFACTS_PATH")
    if not target:
        return None
    t = Path(target)
    if t.exists() and any(t.iterdir()):
        return None  # 已就绪
    t.mkdir(parents=True, exist_ok=True)
    os.environ.pop("DOCLING_ARTIFACTS_PATH", None)
    return t


def _fill_artifacts(target: Path):
    default_models = Path.home() / ".cache" / "docling" / "models"
    if default_models.exists():
        shutil.copytree(default_models, target, dirs_exist_ok=True)


class _PageGeom:
    __slots__ = ("width", "height", "rotation", "crop", "printed_page")

    def __init__(self, width: float, height: float, rotation: int, crop: tuple):
        self.width = width
        self.height = height
        self.rotation = rotation
        self.crop = crop  # (x0, y0, x1, y1) 未旋转裁切框，PDF 坐标（左下原点）
        self.printed_page: Optional[str] = None

    def to_norm_box(self, l: float, b: float, r: float, t: float) -> PdfBox:
        """PDF 用户坐标（左下原点，未旋转）→ 归一化左上原点框。"""
        x0, y0, x1, y1 = self.crop
        w = max(x1 - x0, 1e-6)
        h = max(y1 - y0, 1e-6)
        return PdfBox(
            x0=_clamp01((l - x0) / w),
            y0=_clamp01((y1 - t) / h),
            x1=_clamp01((r - x0) / w),
            y1=_clamp01((y1 - b) / h),
        )


def parse_pdf(path: str, backend: Optional[str] = None) -> dict[str, Any]:
    """解析 PDF。backend=auto 时优先 Docling，未安装自动降级 PyMuPDF。

    手册 §1 要求全流程提供 CPU 路径、断网可跑；模型包可独立离线分发，
    因此 Docling 是可选增强而非启动前置条件。
    """
    want = (backend or os.environ.get("TRACEQA_PDF_BACKEND", "auto")).lower().strip()
    if want == "fitz":
        return _parse_with_fitz(path)

    try:
        from .pdf_fitz import _import_fitz
        fitz = _import_fitz()  # PyMuPDF：仅取页面几何与诊断扫描页
    except ImportError as e:
        raise PdfParseError("缺少 PyMuPDF") from e

    try:
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.datamodel.base_models import InputFormat
    except ImportError as e:
        if want == "docling":
            raise PdfParseError("缺少 Docling，无法解析 PDF") from e
        return _parse_with_fitz(path)

    bootstrap_target = _bootstrap_artifacts()

    # 页面几何与印刷页码探测
    geoms: list[_PageGeom] = []
    doc_fitz = fitz.open(path)
    try:
        for page in doc_fitz:
            crop = page.cropbox  # 未旋转裁切框
            g = _PageGeom(page.rect.width, page.rect.height, page.rotation,
                          (crop.x0, crop.y0, crop.x1, crop.y1))
            geoms.append(g)
        page_count = doc_fitz.page_count
    finally:
        doc_fitz.close()

    pipeline_options = PdfPipelineOptions()
    pipeline_options.do_table_structure = True
    pipeline_options.do_ocr = False  # 首版依赖文本层；扫描页记入覆盖缺口
    converter = DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
    )
    try:
        result = converter.convert(path)
    except Exception as e:
        raise PdfParseError(f"Docling 解析失败：{e}") from e
    if bootstrap_target is not None:
        try:
            _fill_artifacts(bootstrap_target)
        except OSError:
            pass
    doc = result.document

    coverage = CoverageReport(total_pages=page_count)
    evidences: list[ParsedEvidence] = []
    heading_path: list[tuple[int, str]] = []
    pending_caption: Optional[str] = None
    pending_footnotes: list[str] = []
    chunk_no = 0
    pages_with_text: set[int] = set()

    def cur_path() -> list[str]:
        return [h[1] for h in heading_path]

    def push_heading(level: int, text: str):
        while heading_path and heading_path[-1][0] >= level:
            heading_path.pop()
        heading_path.append((level, text))

    def prov_of(item):
        return item.prov[0] if getattr(item, "prov", None) else None

    def locator_of(item, extra_raw: Optional[dict] = None) -> Optional[dict]:
        prov = prov_of(item)
        if prov is None or prov.page_no < 1 or prov.page_no > len(geoms):
            return None
        g = geoms[prov.page_no - 1]
        bb = prov.bbox
        box = g.to_norm_box(bb.l, bb.b, bb.r, bb.t)
        raw = {"l": bb.l, "b": bb.b, "r": bb.r, "t": bb.t,
               "page_no": prov.page_no, "charspan": getattr(prov, "charspan", None)}
        if extra_raw:
            raw.update(extra_raw)
        loc: dict[str, Any] = {"kind": "pdf", "page_index": prov.page_no - 1,
                               "boxes": [box.to_dict()], "raw": raw}
        if g.printed_page:
            loc["printed_page"] = g.printed_page
        return loc

    def emit_text_chunk(text: str, locator_item, para_locator=None):
        nonlocal chunk_no
        if not text.strip():
            return
        loc = locator_of(locator_item) if locator_item is not None else None
        if loc is None and para_locator:
            loc = para_locator
        ev = ParsedEvidence(
            chunk_no=chunk_no,
            text_orig=text,
            text_norm=normalize_text(text),
            heading_path=cur_path(),
            locator=loc or {"kind": "pdf", "page_index": 0, "boxes": []},
            meta={"footnotes": list(pending_footnotes)},
        )
        if ev.locator.get("page_index") is not None:
            pages_with_text.add(ev.locator["page_index"])
        evidences.append(ev)
        chunk_no += 1
        pending_footnotes.clear()

    acc: list[tuple[str, Any]] = []  # (text, item)

    def flush_acc():
        if not acc:
            return
        text = "\n".join(t for t, _ in acc if t.strip())
        first_item = next((it for t, it in acc if t.strip()), None)
        clauses = split_clauses(text, CLAUSE_RE)
        if len(clauses) > 1:
            for _, c_text in clauses:
                emit_text_chunk(c_text, first_item)
        else:
            emit_text_chunk(text, first_item)
        acc.clear()

    for item, _level in doc.iterate_items():
        label = getattr(item, "label", None)
        if label in ("page_header", "page_footer"):
            # 页眉页脚仅用于印刷页码探测
            prov = prov_of(item)
            txt = (getattr(item, "text", "") or "").strip()
            if prov is not None:
                m = PAGE_NO_RE.match(txt)
                if m:
                    geoms[prov.page_no - 1].printed_page = m.group(1)
            continue
        if label == "section_header":
            flush_acc()
            txt = (item.text or "").strip()
            if txt:
                push_heading(getattr(item, "level", 1) or 1, txt)
                pages_with_text.add(prov_of(item).page_no - 1 if prov_of(item) else 0)
            continue
        if label == "caption":
            flush_acc()
            txt = (item.text or "").strip()
            if txt:
                pending_caption = txt
                emit_text_chunk(txt, item)
            continue
        if label == "footnote":
            txt = (item.text or "").strip()
            if txt:
                pending_footnotes.append(txt)
            continue
        if label == "table":
            flush_acc()
            _emit_table(item, geoms, cur_path(), pending_caption, evidences)
            chunk_no = len(evidences)
            pending_caption = None
            prov = prov_of(item)
            if prov is not None:
                pages_with_text.add(prov.page_no - 1)
            continue
        if label in ("text", "list_item", "paragraph", None):
            txt = (getattr(item, "text", "") or "").strip()
            if txt:
                acc.append((txt, item))
            continue
        # 公式、代码等首版不支持
        coverage.unsupported.append(f"未支持内容类型：{label}")

    flush_acc()

    # 扫描页 / 无文本层页判定
    skipped: list[dict] = []
    missing = [i for i in range(page_count) if i not in pages_with_text]
    if missing:
        # 合并连续页
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

    page_geom_out = []
    for i, g in enumerate(geoms):
        page_geom_out.append({
            "page_index": i, "width": g.width, "height": g.height,
            "rotation": g.rotation, "printed_page": g.printed_page,
        })

    return {
        "evidences": evidences,
        "coverage": coverage,
        "page_count": page_count,
        "page_geom": page_geom_out,
        "backend": "docling",
    }


def _parse_with_fitz(path: str) -> dict[str, Any]:
    """PyMuPDF 回退后端：异常统一为 PdfParseError，便于导入端给出明确提示。"""
    from .pdf_fitz import parse_pdf_fitz
    try:
        return parse_pdf_fitz(path)
    except Exception as e:
        if isinstance(e, PdfParseError):
            raise
        raise PdfParseError(f"PDF 解析失败：{e}") from e


def _emit_table(item, geoms: list[_PageGeom], heading_path: list[str],
                caption: Optional[str], evidences: list[ParsedEvidence]):
    """表格 → 单元格级证据。单元格框按表格框在网格上均分近似。"""
    prov = item.prov[0] if getattr(item, "prov", None) else None
    if prov is None or prov.page_no < 1 or prov.page_no > len(geoms):
        return
    g = geoms[prov.page_no - 1]
    data = getattr(item, "data", None)
    grid = getattr(data, "grid", None) if data is not None else None
    if not grid:
        text = (getattr(item, "text", "") or "").strip()
        if text:
            box = g.to_norm_box(prov.bbox.l, prov.bbox.b, prov.bbox.r, prov.bbox.t)
            loc: dict[str, Any] = {"kind": "pdf", "page_index": prov.page_no - 1,
                                   "boxes": [box.to_dict()],
                                   "raw": {"l": prov.bbox.l, "b": prov.bbox.b,
                                           "r": prov.bbox.r, "t": prov.bbox.t}}
            if g.printed_page:
                loc["printed_page"] = g.printed_page
            evidences.append(ParsedEvidence(
                chunk_no=len(evidences),
                text_orig=text, text_norm=normalize_text(text),
                heading_path=list(heading_path),
                locator=loc,
            ))
        return

    n_rows = len(grid)
    n_cols = max(len(r) for r in grid) if grid else 0
    bb = prov.bbox
    tbl_box = g.to_norm_box(bb.l, bb.b, bb.r, bb.t)
    col_headers = [str(grid[0][c].text or "").strip() if c < len(grid[0]) else ""
                   for c in range(n_cols)] if n_rows else []
    header_blob = " ".join(col_headers)
    unit_m = UNIT_RE.search(header_blob)

    chunk_base = len(evidences)
    for ri, row in enumerate(grid):
        row_head = str(row[0].text or "").strip() if row else ""
        for ci in range(n_cols):
            if ci >= len(row):
                continue
            cell = row[ci]
            txt = str(getattr(cell, "text", "") or "").strip()
            if not txt:
                continue
            # 均分近似框（行从上到下；PDF 框 y0<y1 为左上原点）
            fx0 = tbl_box.x0 + (tbl_box.x1 - tbl_box.x0) * ci / n_cols
            fx1 = tbl_box.x0 + (tbl_box.x1 - tbl_box.x0) * (ci + 1) / n_cols
            fy0 = tbl_box.y0 + (tbl_box.y1 - tbl_box.y0) * ri / n_rows
            fy1 = tbl_box.y0 + (tbl_box.y1 - tbl_box.y0) * (ri + 1) / n_rows
            tctx = TableContext(
                table_name=caption or (heading_path[-1] if heading_path else None),
                row_headers=[row_head] if ci != 0 and row_head else [],
                col_headers=[col_headers[ci]] if ci < len(col_headers) and col_headers[ci] else [],
                unit=unit_m.group(0) if unit_m else None,
                footnotes=[],
            )
            cell_loc: dict[str, Any] = {
                "kind": "pdf", "page_index": prov.page_no - 1,
                "boxes": [PdfBox(fx0, fy0, fx1, fy1).to_dict()],
                "raw": {"table_bbox": {"l": bb.l, "b": bb.b, "r": bb.r, "t": bb.t},
                        "grid": [ri, ci, n_rows, n_cols]},
            }
            if g.printed_page:
                cell_loc["printed_page"] = g.printed_page
            evidences.append(ParsedEvidence(
                chunk_no=chunk_base + ri * n_cols + ci,
                text_orig=txt,
                text_norm=normalize_text(txt),
                heading_path=list(heading_path),
                locator=cell_loc,
                table_ctx=tctx.to_dict(),
            ))
