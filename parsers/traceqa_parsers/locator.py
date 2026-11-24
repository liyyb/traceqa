"""定位契约（手册 §5.1）。解析器写入、阅读器渲染、导出引用共用本模块。

Word 定位键：文件修订 + 文档部件 + 块路径 + 段落内文字区间（Unicode 码点，
起点包含、终点排除）。表格额外记录行、列及合并关系。
PDF 定位键：物理页序号 + 归一化证据框。证据框统一到未旋转的可见裁切区域，
左上为原点、右下为 (1,1)。
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


# ---------------------------------------------------------------- Word

@dataclass
class DocxLocator:
    kind: str = "docx"
    part: str = "document"          # 文档部件，首版固定 document
    block_path: list[int] = field(default_factory=list)  # 结构树块路径
    text_start: int = 0             # 码点起点（包含）
    text_end: int = 0               # 码点终点（排除）
    # 表格上下文（命中单元格时填写）
    table_row: Optional[int] = None
    table_col: Optional[int] = None
    row_span: int = 1
    col_span: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "DocxLocator":
        return DocxLocator(
            part=d.get("part", "document"),
            block_path=list(d.get("block_path", [])),
            text_start=int(d.get("text_start", 0)),
            text_end=int(d.get("text_end", 0)),
            table_row=d.get("table_row"),
            table_col=d.get("table_col"),
            row_span=int(d.get("row_span", 1)),
            col_span=int(d.get("col_span", 1)),
        )


# ---------------------------------------------------------------- PDF

@dataclass
class PdfBox:
    """归一化证据框：未旋转可见裁切区域，左上原点，右下 (1,1)。"""
    x0: float
    y0: float
    x1: float
    y1: float

    def to_dict(self) -> dict[str, float]:
        return {"x0": self.x0, "y0": self.y0, "x1": self.x1, "y1": self.y1}


@dataclass
class PdfLocator:
    kind: str = "pdf"
    page_index: int = 0             # 物理页序号，0 起
    boxes: list[PdfBox] = field(default_factory=list)
    printed_page: Optional[str] = None   # 印刷页码（与物理页差异时并列显示）
    raw: Optional[dict] = None      # 原解析器坐标，仅诊断用

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "kind": "pdf",
            "page_index": self.page_index,
            "boxes": [b.to_dict() for b in self.boxes],
        }
        if self.printed_page is not None:
            d["printed_page"] = self.printed_page
        return d

    @staticmethod
    def from_dict(d: dict[str, Any]) -> "PdfLocator":
        return PdfLocator(
            page_index=int(d.get("page_index", 0)),
            boxes=[PdfBox(**b) for b in d.get("boxes", [])],
            printed_page=d.get("printed_page"),
            raw=d.get("raw"),
        )


def locator_from_dict(d: dict[str, Any]) -> Any:
    kind = d.get("kind")
    if kind == "docx":
        return DocxLocator.from_dict(d)
    if kind == "pdf":
        return PdfLocator.from_dict(d)
    raise ValueError(f"未知定位类型: {kind}")


def transform_pdf_box(box: dict[str, float], rotation: int) -> dict[str, float]:
    """未旋转归一化框 → 显示页归一化框（PDF 页面旋转变换，§5.1）。

    解析器、前端阅读器与自动测试共用同一变换定义：
      90°: (x,y)→(1-y,x)  180°: (1-x,1-y)  270°: (y,1-x)
    返回该框旋转后的外接矩形，浏览器按视口宽高一次性映射到像素。
    """
    pts = [(box["x0"], box["y0"]), (box["x1"], box["y0"]),
           (box["x1"], box["y1"]), (box["x0"], box["y1"])]
    r = ((rotation % 360) + 360) % 360
    if r == 90:
        pts = [(1 - y, x) for x, y in pts]
    elif r == 180:
        pts = [(1 - x, 1 - y) for x, y in pts]
    elif r == 270:
        pts = [(y, 1 - x) for x, y in pts]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return {"x": min(xs), "y": min(ys),
            "w": max(xs) - min(xs), "h": max(ys) - min(ys)}


# ---------------------------------------------------------------- 证据

@dataclass
class TableContext:
    """表格单元携带的表名、行列标题、单位、脚注（手册 §5 第 4 条）。"""
    table_name: Optional[str] = None
    row_headers: list[str] = field(default_factory=list)
    col_headers: list[str] = field(default_factory=list)
    unit: Optional[str] = None
    footnotes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ParsedEvidence:
    """解析阶段产出的证据单元（检索单元）。"""
    chunk_no: int                       # 修订内序号
    text_orig: str                      # 原文（引用永远取原文）
    text_norm: str                      # 规范化文本（检索用）
    heading_path: list[str]             # 标题路径
    locator: dict[str, Any]             # DocxLocator/PdfLocator 的 dict
    table_ctx: Optional[dict] = None    # TableContext.to_dict()
    page_no: Optional[int] = None       # 物理页（PDF）
    covered: bool = True                # 覆盖状态（扫描页等置 False 并记入覆盖报告）
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class CoverageReport:
    """覆盖报告：跳过的页、失败原因、未支持内容（修订痕迹、文本框等）。"""
    total_pages: Optional[int] = None       # PDF 物理页数；Word 为参考页数
    covered_pages: Optional[int] = None
    skipped: list[dict] = field(default_factory=list)   # [{"pages":[4,5],"reason":"扫描页"}]
    unsupported: list[str] = field(default_factory=list)
    evidence_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
