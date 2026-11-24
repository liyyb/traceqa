"""手册 §5.1 / §5.2 定位与数值契约单元测试（无需启动服务）。

运行：python -m unittest discover -s evaluation/tests -v
"""
from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "parsers"))
sys.path.insert(0, str(ROOT / "backend"))

from traceqa_parsers.locator import (  # noqa: E402
    DocxLocator, PdfLocator, PdfBox, transform_pdf_box, locator_from_dict,
)
from traceqa_parsers.chunk import split_clauses  # noqa: E402
from traceqa_parsers.docx_parse import parse_docx, CLAUSE_RE  # noqa: E402
from traceqa_server.imports_svc import doc_key_of  # noqa: E402
from traceqa_server import judge  # noqa: E402
from traceqa_server.export_html import render_record_html  # noqa: E402


# ---------------------------------------------------------------- 工具

def _rows(conn: sqlite3.Connection, items: list[dict]) -> list[sqlite3.Row]:
    conn.execute("DROP TABLE IF EXISTS ev")
    conn.execute("""CREATE TABLE ev(
        id TEXT, revision_id TEXT, chunk_no INTEGER, text_orig TEXT,
        text_norm TEXT, heading_path_json TEXT, locator_json TEXT,
        table_ctx_json TEXT, page_no INTEGER, meta_json TEXT)""")
    for i, it in enumerate(items):
        conn.execute(
            "INSERT INTO ev VALUES(?,?,?,?,?,?,?,?,?,?)",
            (it.get("id", f"ev-{i}"), it.get("revision_id", "rev-1"),
             it.get("chunk_no", i), it["text"], it.get("norm", it["text"]),
             json.dumps(it.get("heading", []), ensure_ascii=False),
             json.dumps(it.get("locator", {"kind": "pdf", "page_index": 0}),
                        ensure_ascii=False),
             json.dumps(it.get("table_ctx"), ensure_ascii=False)
             if it.get("table_ctx") else None,
             it.get("page_no"), None))
    conn.commit()
    return conn.execute("SELECT * FROM ev").fetchall()


def _utf16_units(s: str, cp: int) -> int:
    """Unicode 码点偏移 → UTF-16 码元偏移（与前端 cpToUtf16 同义）。"""
    return len(s[:cp].encode("utf-16-le")) // 2


# ---------------------------------------------------------------- Word 码点区间

class TestWordCodepointInterval(unittest.TestCase):

    def test_clause_offsets_and_duplicate_amounts(self):
        """原句含生僻字、换行、跨样式片段，前后两段有相同金额；
        点击后只命中指定块的那次出现。"""
        from docx import Document
        rare = "\U00020000"          # 𠀀：UTF-16 代理对，码点 1 个 / 码元 2 个
        seg1 = "第一条 住宿费每人每晚不超过 500 元，"
        seg2 = f"{rare}说明同上。"
        seg3 = "第二条 住宿费每人每晚不超过 500 元，另有补充。"
        full = seg1 + seg2 + seg3

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "sample.docx"
            doc = Document()
            doc.add_heading("差旅管理办法", level=1)
            para = doc.add_paragraph()
            para.add_run(seg1)
            para.add_run(seg2).bold = True     # 跨样式片段
            para.add_run(seg3)
            doc.save(str(p))
            result = parse_docx(str(p))

        clauses = [e for e in result["evidences"] if "500" in e.text_orig]
        self.assertEqual(len(clauses), 2, "同一段两条条款应各自成块")
        blocks = result["tree"]["blocks"]
        for ev in clauses:
            loc = ev.locator
            self.assertEqual(loc["kind"], "docx")
            blk = next(b for b in blocks if b["path"] == loc["block_path"])
            s, e = loc["text_start"], loc["text_end"]
            # 起点包含、终点排除，且区间以 Unicode 码点计数
            self.assertEqual(blk["text"][s:e], ev.text_orig)
            self.assertLess(s, e)
            # 转成 UTF-16 码元后仍切出同一原句（浏览器选区）
            u0, u1 = _utf16_units(blk["text"], s), _utf16_units(blk["text"], e)
            raw = blk["text"].encode("utf-16-le")
            self.assertEqual(raw[u0 * 2:u1 * 2].decode("utf-16-le"),
                             ev.text_orig)
        self.assertLess(clauses[0].locator["text_start"],
                        clauses[1].locator["text_start"])
        self.assertIn("第二条", full[clauses[1].locator["text_start"]:])

    def test_split_clauses_keeps_offsets(self):
        text = "第一条 甲 500 元。  第二条 乙 500 元。"
        out = split_clauses(text, CLAUSE_RE)
        self.assertEqual(len(out), 2)
        for s, seg in out:
            self.assertEqual(text[s:s + len(seg)], seg)

    def test_locator_roundtrip(self):
        d = {"kind": "docx", "part": "document", "block_path": [3, 1, 0],
             "text_start": 7, "text_end": 19, "table_row": 2,
             "table_col": 1, "col_span": 2}
        self.assertEqual(locator_from_dict(d).to_dict()["block_path"], [3, 1, 0])
        self.assertEqual(locator_from_dict(d).to_dict()["text_end"], 19)


# ---------------------------------------------------------------- PDF 坐标

class TestPdfRotation(unittest.TestCase):

    def test_four_corners_each_rotation(self):
        boxes = {
            "tl": {"x0": 0.0, "y0": 0.0, "x1": 0.1, "y1": 0.1},
            "tr": {"x0": 0.9, "y0": 0.0, "x1": 1.0, "y1": 0.1},
            "br": {"x0": 0.9, "y0": 0.9, "x1": 1.0, "y1": 1.0},
            "bl": {"x0": 0.0, "y0": 0.9, "x1": 0.1, "y1": 1.0},
        }
        # 90° 顺时针：左上→右上→右下→左下→左上
        expected = {0: ["tl", "tr", "br", "bl"],
                    90: ["tr", "br", "bl", "tl"],
                    180: ["br", "bl", "tl", "tr"],
                    270: ["bl", "tl", "tr", "br"]}
        for rot, order in expected.items():
            for name, want in zip(boxes, order):
                t = transform_pdf_box(boxes[name], rot)
                cx, cy = t["x"] + t["w"] / 2, t["y"] + t["h"] / 2
                ref = boxes[want]
                rx, ry = (ref["x0"] + ref["x1"]) / 2, (ref["y0"] + ref["y1"]) / 2
                self.assertAlmostEqual(cx, rx, places=6,
                                       msg=f"rotation {rot} {name} → {want}")
                self.assertAlmostEqual(cy, ry, places=6)
                self.assertAlmostEqual(t["w"], 0.1, places=6)
                self.assertAlmostEqual(t["h"], 0.1, places=6)

    def test_full_page_box_invariant(self):
        full = {"x0": 0.0, "y0": 0.0, "x1": 1.0, "y1": 1.0}
        for rot in (0, 90, 180, 270):
            t = transform_pdf_box(full, rot)
            self.assertAlmostEqual(t["x"], 0.0)
            self.assertAlmostEqual(t["y"], 0.0)
            self.assertAlmostEqual(t["w"], 1.0)
            self.assertAlmostEqual(t["h"], 1.0)

    def test_four_rotations_return_to_origin(self):
        b = {"x0": 0.12, "y0": 0.3, "x1": 0.4, "y1": 0.44}
        t = b
        total = 0
        for _ in range(4):
            t = {"x0": t["x"], "y0": t["y"],
                 "x1": t["x"] + t["w"], "y1": t["y"] + t["h"]} if "w" in t else t
            t = transform_pdf_box(t, 90)
            total += 90
        self.assertEqual(total, 360)
        self.assertAlmostEqual(t["x"], b["x0"], places=6)
        self.assertAlmostEqual(t["y"], b["y0"], places=6)

    def test_pdf_locator_keeps_printed_page_and_raw(self):
        d = {"kind": "pdf", "page_index": 1,
             "boxes": [{"x0": 0.1, "y0": 0.2, "x1": 0.3, "y1": 0.25}],
             "printed_page": "2", "raw": {"l": 10, "b": 20, "r": 30, "t": 40}}
        loc = locator_from_dict(d)
        self.assertEqual(loc.printed_page, "2")
        self.assertEqual(loc.raw["l"], 10)
        self.assertEqual(loc.to_dict()["printed_page"], "2")


# ---------------------------------------------------------------- 数值契约

class TestStructuredAmounts(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        self.conn.close()

    def _facts(self, text, table_ctx=None):
        rows = _rows(self.conn, [{"text": text,
                                  "table_ctx": table_ctx}])
        return judge._extract_facts_from_evidence(rows[0])

    def test_comparator_and_denominator(self):
        cases = [
            ("每人每晚不超过 800 元", "le", "每人每晚"),
            ("住宿限额为每人每晚 800 元", "eq", "每人每晚"),
            ("每间每晚 800 元", "eq", "每间每晚"),
            ("每人每天 800 元", "eq", "每人每天"),
            ("不低于 800 元", "ge", None),
        ]
        for text, comp, denom in cases:
            facts = self._facts(text)
            self.assertEqual(len(facts), 1, text)
            f = facts[0]
            self.assertEqual(f.amount, 800)
            self.assertEqual(f.currency, "元")
            self.assertEqual(f.comparator, comp, text)
            if denom:
                self.assertEqual(f.denominator, denom, text)

    def test_different_units_are_not_same_rule(self):
        keys = {self._facts(t)[0].key_amount() for t in
                ("每人每晚不超过 800 元", "每间每晚不超过 800 元",
                 "每人每天不超过 800 元", "恰为 800 元", "不低于 800 元")}
        self.assertEqual(len(keys), 5, "单位/比较关系不同不得判为同一条上限")

    def test_city_and_subject_from_table_headers(self):
        ctx = {"table_name": "住宿标准表", "row_headers": ["上海"],
               "col_headers": ["普通员工"], "unit": "元／每人每晚"}
        f = self._facts("500 元", ctx)[0]
        self.assertEqual(f.city, "上海")
        self.assertEqual(f.subject, "普通员工")
        self.assertEqual(f.denominator, "每人每晚")


# ---------------------------------------------------------------- §5.2 地区分类

class TestRegionClassification(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row

    def tearDown(self):
        self.conn.close()

    def test_missing_region_mapping_removes_certainty(self):
        items = [
            {"text": "附表：一类城市普通员工住宿标准每人每晚 800 元",
             "table_ctx": {"row_headers": ["一类城市"],
                           "col_headers": ["普通员工"]}},
        ]
        rows = _rows(self.conn, items)
        conds = {"city": "上海", "employee_class": "普通员工",
                 "date_year_known": True, "travel_date": "2026-07-10"}
        self.assertTrue(judge._region_mapping_required(conds, rows))
        claim = judge._decide(None, judge.SubQuestion("住宿", "住宿"), conds,
                              (2026, 7, 10),
                              judge._extract_facts_from_evidence(rows[0]),
                              [], rows, {})
        self.assertEqual(claim["status"], "needs_condition")

    def test_region_mapping_present_keeps_certainty(self):
        items = [
            {"text": "附表：一类城市普通员工住宿标准每人每晚 800 元"},
            {"text": "上海属于一类城市，按一类城市标准执行。"},
        ]
        rows = _rows(self.conn, items)
        conds = {"city": "上海", "employee_class": "普通员工",
                 "date_year_known": True, "travel_date": "2026-07-10"}
        self.assertFalse(judge._region_mapping_required(conds, rows))
        claim = judge._decide(None, judge.SubQuestion("住宿", "住宿"), conds,
                              (2026, 7, 10),
                              judge._extract_facts_from_evidence(rows[0]),
                              [], rows, {})
        self.assertEqual(claim["status"], "answered")


# ---------------------------------------------------------------- 版本与导出

class TestVersioningAndExport(unittest.TestCase):

    def test_same_document_new_version(self):
        self.assertEqual(doc_key_of("差旅管理办法.docx"),
                         doc_key_of("差旅管理办法（修订）.docx"))
        self.assertEqual(doc_key_of("附件1-住宿标准表-扫描.pdf"),
                         doc_key_of("附件1-住宿标准表-清晰补件.pdf"))
        self.assertNotEqual(doc_key_of("住宿补充通知.pdf"),
                            doc_key_of("住宿费用通知.pdf"))

    def test_export_has_anchors_and_full_locator(self):
        snap = {
            "serial_no": "TQ-20261121-001", "rev_no": "R1",
            "issued_at": "2026-11-21T10:00:00+08:00",
            "question": "上海住宿上限？", "conditions": {"city": "上海"},
            "template_version": "t", "rules_version": "r",
            "materials": [{
                "revision_id": "rev-1", "version_id": "ver-1",
                "file_name": "差旅管理办法.docx", "title": "差旅管理办法",
                "sha256_short": "abcdef", "coverage": None,
                "cover_status": "absent",
                "document_fields": {
                    "制度编号": {"value": "ZD-2025-018", "source": "原件"},
                    "发布日期": {"value": "2025 年 12 月 1 日", "source": "原件"}},
                "metadata_notes": [
                    {"field": "生效日期", "value": "2026-01-01",
                     "source": "部门收文登记簿"}],
            }],
            "claims": [{
                "id": "clm-1", "topic": "住宿", "status": "answered",
                "conclusion_text": "每人每晚 500 元", "conditions": {},
                "followup": None, "gap": None, "notes": [],
                "evidences": [{
                    "evidence_id": "ev-1", "revision_id": "rev-1",
                    "group_no": 0, "relation": "support", "quote": "500元",
                    "locator": {"kind": "docx", "block_path": [4],
                                "text_start": 0, "text_end": 4,
                                "table_row": 1, "table_col": 1},
                    "heading_path": ["第二章 住宿标准"],
                    "text_orig": "500元", "doc_title": "差旅管理办法",
                    "file_name": "差旅管理办法.docx",
                    "sha256_short": "abcdef"}],
            }],
        }
        h = render_record_html(snap)
        for needle in ("id='ref-1'", "id='quote-1'", "制度全称：差旅管理办法",
                       "制度编号：ZD-2025-018", "文件注明的发布日期",
                       "导入时文件名：差旅管理办法.docx", "经办人补录",
                       "定位：", "原句：500元", 'class="runhead"'):
            self.assertIn(needle, h, needle)
        # 补录不能印成原件字段
        self.assertNotIn("文件注明的发布日期：2026-01-01", h)

    def test_absent_number_wording(self):
        from traceqa_server.export_html import _field_text
        self.assertEqual(_field_text({"cover_status": "absent"}, "制度编号"),
                         "原件未载明")
        self.assertEqual(_field_text({"cover_status": "unreadable"}, "制度编号"),
                         "封面无法读取，编号待核")


if __name__ == "__main__":
    unittest.main(verbosity=2)
