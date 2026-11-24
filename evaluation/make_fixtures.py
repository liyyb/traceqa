"""任务 A：制作全套虚构制度夹具（手册 §6 双格式版本回归脚本、§7.1 任务 A）。

生成内容（全部虚构）：
  A  差旅管理办法.docx      附表一：上海／普通员工 每人每晚 500 元（2026-01-01 施行）
  A2 差旅管理办法（修订）.docx 新版把限额改成 600 元（版本回归用）
  B  住宿补充通知.pdf         自 2026-07-01 起将 A 的上海普通员工标准调整为 800 元，原标准同时停止执行
  C  住宿费用通知.pdf         同期规定 800 元，但无替代关系（冲突件）
  D  差旅管理办法（附件引用）.docx 正文指向附件1《住宿标准表》，附件为扫描页（覆盖缺口）
  D-附件1-住宿标准表-扫描.pdf  无文本层的“扫描”附件
  D-附件1-住宿标准表-清晰补件.pdf 清晰补件（800 元）
  E  无编号制度.docx          封面未载明制度编号（补录用例）
  F  跨页表格通知.pdf         表格跨页 + 横向旋转页（定位回归）
预期摘录与人工可读位置写入 expectations.json。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FIX_DIR = HERE / "fixtures"
FIX_DIR.mkdir(parents=True, exist_ok=True)

sys.path.insert(0, str(HERE.parent / "parsers"))


# ================================================================ DOCX

def make_docx_files():
    from docx import Document
    from docx.shared import Pt

    # ---------------- A 差旅管理办法 ----------------
    doc = Document()
    doc.add_heading("差旅管理办法", level=1)
    doc.add_paragraph("办法编号：ZD-2025-018")
    doc.add_paragraph("发布日期：2025 年 12 月 1 日")
    doc.add_paragraph("本办法自 2026 年 1 月 1 日起施行。")
    doc.add_heading("第一章 总则", level=2)
    doc.add_paragraph("第一条 为规范员工出差管理，制定本办法。")
    doc.add_heading("第二章 住宿标准", level=2)
    doc.add_paragraph("第二条 员工出差住宿标准按附表一执行。")
    doc.add_paragraph("附表一：住宿标准表（单位：元／每人每晚）")
    table = doc.add_table(rows=3, cols=3)
    table.style = "Table Grid"
    hdr = table.rows[0].cells
    hdr[0].text = "城市＼类别"
    hdr[1].text = "普通员工"
    hdr[2].text = "部门负责人"
    r1 = table.rows[1].cells
    r1[0].text = "上海"
    r1[1].text = "500元"
    r1[2].text = "800元"
    r2 = table.rows[2].cells
    r2[0].text = "杭州"
    r2[1].text = "400元"
    r2[2].text = "600元"
    doc.add_paragraph("第三条 住宿费在标准内凭发票报销，超支自理。")
    doc.add_heading("第三章 伙食补助", level=2)
    doc.add_paragraph("第四条 出差伙食补助按每人每天 100 元包干使用。")
    doc.save(FIX_DIR / "差旅管理办法.docx")

    # ---------------- A2 修订版（600 元） ----------------
    doc = Document()
    doc.add_heading("差旅管理办法（修订）", level=1)
    doc.add_paragraph("办法编号：ZD-2025-018")
    doc.add_paragraph("发布日期：2026 年 9 月 1 日")
    doc.add_paragraph("本办法自 2026 年 10 月 1 日起施行。")
    doc.add_heading("第二章 住宿标准", level=2)
    doc.add_paragraph("第二条 员工出差住宿标准按附表一执行。")
    doc.add_paragraph("附表一：住宿标准表（单位：元／每人每晚）")
    table = doc.add_table(rows=3, cols=3)
    table.style = "Table Grid"
    hdr = table.rows[0].cells
    hdr[0].text = "城市＼类别"
    hdr[1].text = "普通员工"
    hdr[2].text = "部门负责人"
    r1 = table.rows[1].cells
    r1[0].text = "上海"
    r1[1].text = "600元"
    r1[2].text = "900元"
    r2 = table.rows[2].cells
    r2[0].text = "杭州"
    r2[1].text = "450元"
    r2[2].text = "650元"
    doc.add_paragraph("第三条 住宿费在标准内凭发票报销，超支自理。")
    doc.save(FIX_DIR / "差旅管理办法（修订）.docx")

    # ---------------- D 指向扫描附件 ----------------
    doc = Document()
    doc.add_heading("差旅管理办法（附件引用版）", level=1)
    doc.add_paragraph("办法编号：ZD-2024-006")
    doc.add_paragraph("本办法自 2024 年 6 月 1 日起施行。")
    doc.add_heading("第二章 住宿标准", level=2)
    doc.add_paragraph("第二条 员工出差住宿标准详见附件 1《住宿标准表》。")
    doc.add_paragraph("第三条 住宿费在标准内凭发票报销。")
    doc.save(FIX_DIR / "差旅管理办法（附件引用版）.docx")

    # ---------------- E 无编号制度 ----------------
    doc = Document()
    doc.add_heading("市内交通补贴办法", level=1)
    doc.add_paragraph("发布日期：2026 年 6 月 25 日")
    doc.add_paragraph("第一条 员工市内办公交通补贴按每人每天 30 元发放。")
    doc.add_paragraph("第二条 补贴随工资发放，无需单独报销。")
    doc.save(FIX_DIR / "市内交通补贴办法（无编号）.docx")


# ================================================================ PDF

def _register_cn_font():
    """注册系统中文字体（reportlab 5.x 的 CID 字体宽度表不兼容，改用 TTF）。"""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    import os
    for name, path in (("SimHei", r"C:\Windows\Fonts\simhei.ttf"),
                       ("SimFang", r"C:\Windows\Fonts\simfang.ttf")):
        if os.path.exists(path):
            pdfmetrics.registerFont(TTFont(name, path))
            return name
    raise RuntimeError("未找到可用中文字体")


CN_FONT = None


def make_pdf_files():
    global CN_FONT
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as cv
    CN_FONT = _register_cn_font()
    W, H = A4

    def text_block(c, x, y, lines, size=12, leading=22):
        c.setFont(CN_FONT, size)
        for line in lines:
            c.drawString(x, y, line)
            y -= leading
        return y

    # ---------------- B 住宿补充通知（第 2 页调整条款） ----------------
    c = cv.Canvas(str(FIX_DIR / "住宿补充通知.pdf"), pagesize=A4)
    c.setFont(CN_FONT, 18)
    c.drawString(72, H - 96, "住宿补充通知")
    c.setFont(CN_FONT, 12)
    c.drawString(72, H - 130, "文号：ZD-2026-011")
    c.drawString(72, H - 152, "发布日期：2026 年 6 月 20 日")
    c.drawString(72, H - 190, "现就《差旅管理办法》（ZD-2025-018）住宿标准补充通知如下。")
    c.showPage()
    # 第 2 页：调整条款 + 页脚印刷页码
    y = H - 96
    c.setFont(CN_FONT, 14)
    c.drawString(72, y, "第四条 标准调整")
    y -= 30
    y = text_block(c, 72, y, [
        "自 2026 年 7 月 1 日起，将《差旅管理办法》附表一中",
        "上海普通员工住宿标准调整为每人每晚 800 元。",
        "原标准同时停止执行。",
    ])
    y -= 16
    y = text_block(c, 72, y, [
        "第五条 本通知自 2026 年 7 月 1 日起执行，此前规定与本通知",
        "不一致的，以本通知为准。",
    ])
    c.setFont(CN_FONT, 10)
    c.drawCentredString(W / 2, 40, "2")
    c.showPage()
    c.save()

    # ---------------- C 冲突件（无替代关系） ----------------
    c = cv.Canvas(str(FIX_DIR / "住宿费用通知.pdf"), pagesize=A4)
    c.setFont(CN_FONT, 18)
    c.drawString(72, H - 96, "住宿费用通知")
    c.setFont(CN_FONT, 12)
    c.drawString(72, H - 130, "文号：CW-2026-004")
    c.drawString(72, H - 152, "发布日期：2026 年 6 月 28 日")
    y = H - 200
    y = text_block(c, 72, y, [
        "第一条 自 2026 年 7 月 1 日起，员工赴上海出差住宿限额",
        "为每人每晚 800 元。",
        "第二条 住宿限额凭发票在限额内据实报销。",
    ])
    c.showPage()
    c.save()

    # ---------------- D 附件：扫描页（无文本层） ----------------
    c = cv.Canvas(str(FIX_DIR / "附件1-住宿标准表-扫描.pdf"), pagesize=A4)
    # 只画线条模拟扫描件：不写入任何文本
    c.setLineWidth(1.2)
    c.rect(72, H - 300, 400, 180)
    for i in range(1, 3):
        c.line(72, H - 300 + i * 60, 472, H - 300 + i * 60)
    c.line(205, H - 300, 205, H - 120)
    c.line(338, H - 300, 338, H - 120)
    c.showPage()
    c.save()

    # ---------------- D 补件：清晰版（含文本层） ----------------
    c = cv.Canvas(str(FIX_DIR / "附件1-住宿标准表-清晰补件.pdf"), pagesize=A4)
    c.setFont(CN_FONT, 16)
    c.drawString(72, H - 96, "附件 1：住宿标准表（清晰补件）")
    c.setFont(CN_FONT, 12)
    y = H - 140
    y = text_block(c, 72, y, [
        "单位：元／每人每晚",
        "上海 · 普通员工：800 元",
        "上海 · 部门负责人：1000 元",
        "杭州 · 普通员工：500 元",
        "本表自 2026 年 7 月 1 日起执行。",
    ])
    c.showPage()
    c.save()

    # ---------------- F 跨页表格 + 旋转页 ----------------
    c = cv.Canvas(str(FIX_DIR / "跨页表格通知.pdf"), pagesize=A4)
    c.setFont(CN_FONT, 16)
    c.drawString(72, H - 96, "关于调整部分城市住宿标准的通知")
    c.setFont(CN_FONT, 12)
    c.drawString(72, H - 124, "文号：ZD-2026-015　发布日期：2026 年 8 月 1 日")
    y = H - 170
    c.drawString(72, y, "附表：调整明细（单位：元／每人每晚）")
    y -= 26
    rows = [("城市", "普通员工", "部门负责人")] + [
        (city, str(a), str(b)) for city, a, b in [
            ("北京", 550, 850), ("广州", 520, 820), ("深圳", 560, 860),
            ("成都", 420, 680), ("武汉", 430, 690), ("西安", 410, 670),
            ("南京", 500, 800), ("苏州", 490, 790), ("天津", 480, 780),
            ("重庆", 440, 700), ("青岛", 460, 720), ("厦门", 470, 730),
        ]]
    for i, (c1, c2, c3) in enumerate(rows):
        if y < 80:
            break
        c.drawString(80, y, c1)
        c.drawString(220, y, c2)
        c.drawString(360, y, c3)
        y -= 24
    c.showPage()
    # 第 2 页：续表
    c.setFont(CN_FONT, 12)
    y = H - 96
    c.drawString(72, y, "附表（续）")
    y -= 26
    for c1, c2, c3 in [("长沙", "420", "680"), ("郑州", "400", "660"),
                       ("济南", "450", "710"), ("合肥", "430", "690")]:
        c.drawString(80, y, c1)
        c.drawString(220, y, c2)
        c.drawString(360, y, c3)
        y -= 24
    y -= 20
    c.drawString(72, y, "本通知自 2026 年 8 月 15 日起执行。")
    c.showPage()
    # 第 3 页：横向旋转 90° 页面（定位回归）
    c.save()
    import fitz
    with fitz.open(str(FIX_DIR / "跨页表格通知.pdf")) as doc:
        page = doc.new_page(width=A4[0], height=A4[1])
        page.set_rotation(90)
        # 旋转页写入文字（沿可见方向）
        text = "特别说明：本通知附表与《差旅管理办法》附表一并行有效，类别以职级为准。"
        page.insert_text((60, A4[0] - 80), text, fontsize=13,
                         fontname="china-s", rotate=0)
        tmp_out = str(FIX_DIR / "跨页表格通知.tmp.pdf")
        doc.save(tmp_out, deflate=True)
    import os
    os.replace(tmp_out, str(FIX_DIR / "跨页表格通知.pdf"))


# ================================================================ 预期清单

EXPECTATIONS = {
    "files": {
        "A": "差旅管理办法.docx",
        "A2": "差旅管理办法（修订）.docx",
        "B": "住宿补充通知.pdf",
        "C": "住宿费用通知.pdf",
        "D": "差旅管理办法（附件引用版）.docx",
        "D_attach_scan": "附件1-住宿标准表-扫描.pdf",
        "D_attach_clear": "附件1-住宿标准表-清晰补件.pdf",
        "E": "市内交通补贴办法（无编号）.docx",
        "F": "跨页表格通知.pdf",
    },
    "cases": [
        {
            "id": "date-boundary-500",
            "question": "2026年6月30日普通员工在上海住宿，每人每晚的上限是多少？",
            "scope": ["A", "B"],
            "expect": {"topic": "住宿", "status": "answered", "amount": "500",
                       "evidence_doc": "A",
                       "human_location": "差旅管理办法 · 第二章 住宿标准 · 附表一 上海行／普通员工列"},
        },
        {
            "id": "date-boundary-800",
            "question": "2026年7月1日普通员工在上海住宿，每人每晚的上限是多少？",
            "scope": ["A", "B"],
            "expect": {"topic": "住宿", "status": "answered", "amount": "800",
                       "evidence_doc": "B",
                       "human_location": "住宿补充通知 · 物理第 2 页 · 第四条"},
        },
        {
            "id": "no-date-with-breakfast",
            "question": "普通员工去上海住宿，每晚能报多少？早餐能单报吗？",
            "scope": ["A", "B"],
            "expect_multi": [
                {"topic": "住宿", "status": "needs_condition",
                 "followup_contains": "7 月 1 日"},
                {"topic": "早餐", "status": "no_evidence"},
            ],
        },
        {
            "id": "conflict-A-C",
            "question": "2026年7月10日普通员工在上海住宿，每人每晚能报多少？",
            "scope": ["A", "C"],
            "expect": {"topic": "住宿", "status": "conflict",
                       "amounts": ["500", "800"],
                       "note": "C 无替代关系，双方原文并列"},
        },
        {
            "id": "coverage-gap-D",
            "question": "普通员工在上海住宿，每晚能报多少？",
            "scope": ["D"],
            "expect": {"topic": "住宿", "status": "coverage_gap",
                       "gap_attachment": "住宿标准表"},
        },
        {
            "id": "breakfast-only",
            "question": "出差期间的早餐能单独报销吗？",
            "scope": ["A"],
            "expect": {"topic": "早餐", "status": "no_evidence"},
        },
    ],
}


def main():
    make_docx_files()
    make_pdf_files()
    (FIX_DIR / "expectations.json").write_text(
        json.dumps(EXPECTATIONS, ensure_ascii=False, indent=2), encoding="utf-8")
    print("夹具已生成：")
    for p in sorted(FIX_DIR.iterdir()):
        print(" -", p.name, p.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
