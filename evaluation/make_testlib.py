"""生成 30 份虚构制度测试库（手册 §6：约 30 份、约 500 页，PDF/Word 混排）。

全部内容为虚构，不引用任何真实单位或真实制度。
输出：evaluation/testlib/ 下的 30 个原件 + manifest.json + test_questions.json

覆盖场景：
  日期边界与显式替代 / 无替代关系的真实冲突 / 扫描附件与清晰补件 /
  封面无编号 / 地区分类缺映射 / 跨页表格 / 旋转页 / 同名新版本 /
  中英文混排 / 长附录（用于冷启动与大批量导入计时）
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "testlib"
OUT.mkdir(parents=True, exist_ok=True)
sys.path.insert(0, str(HERE.parent / "parsers"))

DOCX_PAGES = 18
PDF_PAGES = 17

APPENDIX_TITLES = [
    "附录 A：条款索引", "附录 B：填报模板", "附录 C：办理流程",
    "附录 D：职责分工", "附录 E：常见问题", "附录 F：表单样式",
    "附录 G：填写说明", "附录 H：归档要求", "附录 I：复核要点",
]

EN_TERMS = ("Policy No.", "Effective Date", "Issue Date", "Department",
            "Approval", "Record No.", "Review", "Attachment", "Version", "Note")


def appendix_lines(spec: dict, page: int, index: int) -> list[str]:
    """生成一页可打印的附录内容，避免无意义乱码，且每页行数固定。"""
    title = APPENDIX_TITLES[index % len(APPENDIX_TITLES)]
    head = f"{title}（{spec['short']} · 第 {index + 1} 节）"
    lines = [head]
    family = spec["family"]
    for i in range(12):
        n = page * 20 + i + 1
        lines.append(
            f"{n:03d}. {family}事项登记第 {n} 项：经办部门填写实际情况，"
            f"复核岗位核对依据后签署意见。")
    lines.append(f"{EN_TERMS[index % len(EN_TERMS)]}: {spec['code'] or 'N/A'} / {spec['effective']}")
    lines.append(f"本页为 {spec['short']} 的组成部分，与主文一并保存。")
    return lines


# ============================================================ 规格清单
SPECS: list[dict] = [
    dict(id="T01", name="01-差旅与费用管理办法", fmt="docx", pages=DOCX_PAGES,
         family="差旅", short="差旅办法", code="ZD-2026-001",
         issue="2025 年 12 月 1 日", effective="2026 年 1 月 1 日",
         body=["第一章 总则", "第一条 为规范员工出差管理，制定本办法。",
               "第二章 住宿标准", "第二条 员工出差住宿标准按附表一执行。",
               "附表一：住宿标准表（单位：元／每人每晚）",
               "第三条 住宿费在标准内凭发票报销，超支自理。",
               "第四条 出差伙食补助按每人每天 100 元包干使用。"],
         table=dict(header=["城市", "普通员工", "部门负责人"],
                    rows=[["上海", "500元", "800元"], ["杭州", "400元", "600元"],
                          ["北京", "550元", "850元"]]),
         flags=["base-500"], core="上海普通员工住宿标准为每人每晚 500 元。"),

    dict(id="T02", name="01-差旅与费用管理办法（修订版）", fmt="docx", pages=DOCX_PAGES,
         family="差旅", short="差旅办法新版", code="ZD-2026-001",
         issue="2026 年 9 月 1 日", effective="2026 年 10 月 1 日",
         body=["第二章 住宿标准", "第二条 员工出差住宿标准按附表一执行。",
               "附表一：住宿标准表（单位：元／每人每晚）",
               "第三条 住宿费在标准内凭发票报销，超支自理。"],
         table=dict(header=["城市", "普通员工", "部门负责人"],
                    rows=[["上海", "600元", "900元"], ["杭州", "450元", "650元"],
                          ["北京", "650元", "950元"]]),
         flags=["new-version"], core="修订版自 2026 年 10 月 1 日起施行。"),

    dict(id="T03", name="02-住宿标准调整通知", fmt="pdf", pages=PDF_PAGES,
         family="差旅", short="住宿调整通知", code="ZD-2026-011",
         issue="2026 年 6 月 20 日", effective="2026 年 7 月 1 日",
         body=["现就《差旅与费用管理办法》（ZD-2026-001）住宿标准补充通知如下。"],
         special={2: ["第四条 标准调整",
                      "自 2026 年 7 月 1 日起，将上海普通员工住宿标准调整为每人每晚 800 元。",
                      "原标准同时停止执行。",
                      "第五条 本通知自 2026 年 7 月 1 日起执行，此前规定与本通知不一致的，以本通知为准。"]},
         table=None, flags=["replacement"], core="自 2026 年 7 月 1 日起上海普通员工住宿标准为 800 元。"),

    dict(id="T04", name="03-住宿费用规定", fmt="pdf", pages=PDF_PAGES,
         family="差旅", short="住宿费用规定", code="CW-2026-004",
         issue="2026 年 6 月 28 日", effective="2026 年 7 月 1 日",
         body=["第一条 自 2026 年 7 月 1 日起，员工赴上海出差住宿限额为每人每晚 800 元。",
               "第二条 住宿限额凭发票在限额内据实报销。"],
         table=None, flags=["conflict-no-replace"], core="同期另一个 800 元标准，无替代关系。"),

    dict(id="T05", name="04-差旅管理办法（附件引用版）", fmt="docx", pages=DOCX_PAGES,
         family="差旅", short="附件引用版", code="ZD-2024-006",
         issue="2024 年 5 月 20 日", effective="2024 年 6 月 1 日",
         body=["第二章 住宿标准",
               "第二条 员工出差住宿标准详见附件 1《住宿标准表》。",
               "第三条 住宿费在标准内凭发票报销。"],
         table=None, flags=["coverage-gap"], core="标准在附件 1，正文未写金额。"),

    dict(id="T06", name="04-附件1-住宿标准表-扫描", fmt="pdf", pages=1,
         family="差旅", short="扫描附件", code="", issue="", effective="",
         body=[], table=None, scanned=True, flags=["scanned"],
         core="无文本层的扫描附件，需人工核对。"),

    dict(id="T07", name="04-附件1-住宿标准表-清晰补件", fmt="pdf", pages=1,
         family="差旅", short="清晰补件", code="", issue="2026 年 6 月 1 日", effective="2026 年 7 月 1 日",
         body=["附件 1：住宿标准表（清晰补件）", "单位：元／每人每晚",
               "上海 · 普通员工：800 元", "上海 · 部门负责人：1000 元",
               "杭州 · 普通员工：500 元", "本表自 2026 年 7 月 1 日起执行。"],
         table=None, flags=["clear-supplement"], core="清晰补件写明上海普通员工 800 元。"),

    dict(id="T08", name="05-市内交通补贴办法", fmt="docx", pages=DOCX_PAGES,
         family="交通", short="交通补贴办法", code="",
         issue="2026 年 6 月 25 日", effective="2026 年 7 月 1 日",
         body=["第一条 员工市内办公交通补贴按每人每天 30 元发放。",
               "第二条 补贴随工资发放，无需单独报销。"],
         table=None, flags=["no-number"], core="交通补贴每人每天 30 元。"),

    dict(id="T09", name="06-地区分类与住宿标准通知", fmt="pdf", pages=PDF_PAGES + 1,
         family="差旅", short="地区分类通知", code="ZD-2026-019",
         issue="2026 年 5 月 10 日", effective="2026 年 6 月 1 日",
         body=["附表：住宿标准表（单位：元／每人每晚）", "北京属于一类城市。"],
         table=dict(header=["城市类别", "普通员工", "部门负责人"],
                    rows=[["一类城市", "700元", "1000元"], ["二类城市", "500元", "750元"]]),
         rotate_last=True, flags=["region-mapping", "rotated-page"],
         core="按一类／二类城市分档，上海缺少明确归类。"),

    dict(id="T10", name="07-跨页表格标准通知", fmt="pdf", pages=PDF_PAGES,
         family="差旅", short="跨页住宿标准表通知", code="ZD-2026-015",
         issue="2026 年 8 月 1 日", effective="2026 年 8 月 15 日",
         body=["第一条 住宿标准调整明细见附表。",
               "附表：住宿标准明细（单位：元／每人每晚）",
               "本通知自 2026 年 8 月 15 日起执行。"],
         table=dict(header=["城市", "普通员工", "部门负责人"],
                    rows=[["北京", "550元", "850元"], ["广州", "520元", "820元"],
                          ["深圳", "560元", "860元"], ["成都", "420元", "680元"],
                          ["武汉", "430元", "690元"], ["西安", "410元", "670元"],
                          ["南京", "500元", "800元"], ["苏州", "490元", "790元"]]),
         cross_page=True, flags=["cross-page-table"], core="北京普通员工 550 元。"),

    dict(id="T11", name="08-餐补与报销细则", fmt="docx", pages=DOCX_PAGES,
         family="餐补", short="餐补细则", code="CW-2026-021",
         issue="2026 年 3 月 1 日", effective="2026 年 4 月 1 日",
         body=["第一条 出差餐补按每人每天 80 元标准发放。",
               "第二条 餐补随差旅报销单一并办理。"],
         table=None, flags=[], core="餐补每人每天 80 元。"),
    dict(id="T12", name="09-出差审批流程规定", fmt="docx", pages=DOCX_PAGES,
         family="出差审批", short="审批规定", code="ZD-2026-023",
         issue="2026 年 2 月 1 日", effective="2026 年 3 月 1 日",
         body=["第一条 员工出差须提前提交出差审批。",
               "第二条 出差审批由部门负责人批准后方可执行。"],
         table=None, flags=[], core="出差审批流程，无金额标准。"),
    dict(id="T13", name="10-薪酬管理办法", fmt="docx", pages=DOCX_PAGES,
         family="薪酬", short="薪酬办法", code="RL-2026-005",
         issue="2026 年 1 月 5 日", effective="2026 年 2 月 1 日",
         body=["第一条 基本工资按岗位等级确定。",
               "第二条 薪酬调整须经审批后于次月生效。"],
         table=None, flags=[], core="薪酬标准按岗位等级执行。"),

    # ---- 中后段：PDF 家族 ----
    dict(id="T14", name="11-采购管理办法", fmt="pdf", pages=PDF_PAGES,
         family="采购", short="采购办法", code="CG-2026-002",
         issue="2026 年 1 月 8 日", effective="2026 年 2 月 1 日",
         body=["第一条 采购申请须经部门负责人审批。",
               "第二条 采购合同须经法务审核后签署。"],
         table=None, flags=[], core="采购审批要求。"),
    dict(id="T15", name="12-合同管理制度", fmt="pdf", pages=PDF_PAGES,
         family="合同", short="合同制度", code="FG-2026-003",
         issue="2026 年 1 月 9 日", effective="2026 年 2 月 1 日",
         body=["第一条 合同签署前须经法务审核。",
               "第二条 合同文本由经办部门归档保存。"],
         table=None, flags=[], core="合同审核要求。"),
    dict(id="T16", name="13-印章管理规定", fmt="pdf", pages=PDF_PAGES,
         family="行政", short="印章规定", code="XZ-2026-007",
         issue="2026 年 1 月 10 日", effective="2026 年 2 月 1 日",
         body=["第一条 印章使用须履行审批手续。",
               "第二条 印章使用记录须按月归档。"],
         table=None, flags=[], core="印章审批要求。"),
    dict(id="T17", name="14-档案管理办法", fmt="pdf", pages=PDF_PAGES,
         family="档案", short="档案办法", code="XZ-2026-009",
         issue="2026 年 1 月 11 日", effective="2026 年 2 月 1 日",
         body=["第一条 归档材料须完整、真实。",
               "第二条 档案借阅须经归口部门批准。"],
         table=None, flags=[], core="归档要求。"),
    dict(id="T18", name="15-会议管理制度", fmt="pdf", pages=PDF_PAGES,
         family="行政", short="会议制度", code="XZ-2026-011",
         issue="2026 年 1 月 12 日", effective="2026 年 2 月 1 日",
         body=["第一条 会议纪要须在会后两个工作日内发出。",
               "第二条 会议决定事项须跟踪落实。"],
         table=None, flags=[], core="会议纪要时限。"),
    dict(id="T19", name="16-公务用车管理办法", fmt="pdf", pages=PDF_PAGES,
         family="行政", short="用车办法", code="XZ-2026-013",
         issue="2026 年 1 月 13 日", effective="2026 年 2 月 1 日",
         body=["第一条 公务用车须提前预约。",
               "第二条 用车结束后须登记里程。"],
         table=None, flags=[], core="用车预约要求。"),
    dict(id="T20", name="17-办公用品管理规定", fmt="pdf", pages=PDF_PAGES,
         family="行政", short="用品规定", code="XZ-2026-015",
         issue="2026 年 1 月 14 日", effective="2026 年 2 月 1 日",
         body=["第一条 办公用品按季度申领。",
               "第二条 领用情况由行政部门登记。"],
         table=None, flags=[], core="用品申领要求。"),
    dict(id="T21", name="18-培训管理办法", fmt="pdf", pages=PDF_PAGES,
         family="培训", short="培训办法", code="RL-2026-017",
         issue="2026 年 1 月 15 日", effective="2026 年 2 月 1 日",
         body=["第一条 培训结束后须提交学习记录。",
               "第二条 外部培训须事先审批。"],
         table=None, flags=[], core="培训记录要求。"),
    dict(id="T22", name="19-保密工作规定", fmt="pdf", pages=PDF_PAGES,
         family="行政", short="保密规定", code="XZ-2026-019",
         issue="2026 年 1 月 16 日", effective="2026 年 2 月 1 日",
         body=["第一条 涉密文件不得外传。",
               "第二条 涉密载体须按清单保管。"],
         table=None, flags=[], core="保密管理要求。"),

    # ---- 中后段：DOCX 家族 ----
    dict(id="T23", name="20-招聘与入职管理办法", fmt="docx", pages=DOCX_PAGES,
         family="人事", short="招聘办法", code="RL-2026-021",
         issue="2026 年 1 月 17 日", effective="2026 年 2 月 1 日",
         body=["第一条 入职材料须在报到当日提交。",
               "第二条 试用期内须完成岗位培训。"],
         table=None, flags=[], core="入职材料要求。"),
    dict(id="T24", name="21-考勤与休假管理制度", fmt="docx", pages=DOCX_PAGES,
         family="人事", short="考勤制度", code="RL-2026-023",
         issue="2026 年 1 月 18 日", effective="2026 年 2 月 1 日",
         body=["第一条 员工考勤按实际出勤记录。",
               "第二条 年休假按国家规定执行。"],
         table=None, flags=[], core="考勤与休假要求。"),
    dict(id="T25", name="22-绩效考核办法", fmt="docx", pages=DOCX_PAGES,
         family="人事", short="绩效办法", code="RL-2026-025",
         issue="2026 年 1 月 19 日", effective="2026 年 2 月 1 日",
         body=["第一条 绩效考核每年开展两次。",
               "第二条 考核结果须反馈至本人。"],
         table=None, flags=[], core="绩效考核周期。"),
    dict(id="T26", name="23-安全生产管理制度", fmt="docx", pages=DOCX_PAGES,
         family="安全", short="安全制度", code="AQ-2026-027",
         issue="2026 年 1 月 20 日", effective="2026 年 2 月 1 日",
         body=["第一条 安全隐患须及时上报。",
               "第二条 安全培训每半年开展一次。"],
         table=None, flags=[], core="安全隐患上报要求。"),
    dict(id="T27", name="24-信息安全管理办法", fmt="docx", pages=DOCX_PAGES,
         family="信息安全", short="信息安全", code="XX-2026-029",
         issue="2026 年 1 月 21 日", effective="2026 年 2 月 1 日",
         body=["第一条 账号权限须定期复核。",
               "第二条 离职人员账号须及时停用。"],
         table=None, flags=[], core="账号权限要求。"),
    dict(id="T28", name="25-知识产权管理规定", fmt="docx", pages=DOCX_PAGES,
         family="法务", short="知产规定", code="FG-2026-031",
         issue="2026 年 1 月 22 日", effective="2026 年 2 月 1 日",
         body=["第一条 职务成果归公司所有。",
               "第二条 成果登记须在完成后办理。"],
         table=None, flags=[], core="职务成果归属。"),
    dict(id="T29", name="26-员工宿舍管理办法", fmt="docx", pages=DOCX_PAGES,
         family="行政", short="宿舍办法", code="XZ-2026-033",
         issue="2026 年 1 月 23 日", effective="2026 年 2 月 1 日",
         body=["第一条 宿舍费用按实际发生结算。",
               "第二条 宿舍入住须办理登记。"],
         table=None, flags=[], core="宿舍费用结算。"),
    dict(id="T30", name="27-食堂管理办法", fmt="docx", pages=DOCX_PAGES,
         family="行政", short="食堂办法", code="XZ-2026-035",
         issue="2026 年 1 月 24 日", effective="2026 年 2 月 1 日",
         body=["第一条 食堂餐费按实际消费结算。",
               "第二条 食堂账目须按月公示。"],
         table=None, flags=[], core="食堂餐费结算。"),
]


# ============================================================ PDF

def _cn_font():
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    for name, path in (("SimHei", r"C:\Windows\Fonts\simhei.ttf"),
                       ("SimFang", r"C:\Windows\Fonts\simfang.ttf")):
        if os.path.exists(path):
            pdfmetrics.registerFont(TTFont(name, path))
            return name
    raise RuntimeError("未找到可用中文字体")


def _wrap(line: str, size: int, width: float) -> list[str]:
    max_chars = max(12, int(width / (size * 1.02)))
    if len(line) <= max_chars:
        return [line]
    out, cur = [], ""
    for ch in line:
        if len(cur) >= max_chars:
            out.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def _draw_table(c, header, rows, y, font, label="附表"):
    """画带框线的表格：PyMuPDF 的 find_tables 依赖框线识别行列与表头。"""
    c.setFont(font, 10)
    c.drawString(80, y, label)
    y -= 22
    all_rows = [header] + rows
    top = y + 6
    x_edges = [72, 230, 380, 520]
    c.setLineWidth(0.8)
    for x in x_edges:
        c.line(x, top, x, top - 24 * len(all_rows))
    for i in range(len(all_rows) + 1):
        yy = top - 24 * i
        c.line(72, yy, 520, yy)
    for i, row in enumerate(all_rows):
        c.setFont(font, 11)
        baseline = top - 18 - 24 * i
        c.drawString(80, baseline, row[0])
        c.drawString(240, baseline, row[1])
        c.drawString(390, baseline, row[2])
    return top - 24 * len(all_rows) - 12


def make_pdf(spec: dict, font: str):
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas as cv
    W, H = A4
    path = OUT / f"{spec['name']}.pdf"

    def draw_lines(c, lines, x, y, size=11, leading=22):
        c.setFont(font, size)
        for raw in lines:
            for seg in _wrap(raw, size, W - x - 72):
                c.drawString(x, y, seg)
                y -= leading
        return y

    if spec.get("scanned"):
        c = cv.Canvas(str(path), pagesize=A4)
        c.setLineWidth(1.2)
        c.rect(72, H - 320, 440, 200)
        for i in range(1, 4):
            c.line(72, H - 320 + i * 50, 512, H - 320 + i * 50)
        for x in (215, 360):
            c.line(x, H - 320, x, H - 120)
        c.showPage()
        c.save()
        return path

    c = cv.Canvas(str(path), pagesize=A4)
    for p in range(1, spec["pages"] + 1):
        y = H - 84
        if p == 1:
            c.setFont(font, 17)
            c.drawString(72, y, spec["short"])
            y -= 36
            c.setFont(font, 11)
            if spec["code"]:
                c.drawString(72, y, f"Policy No. / 制度编号：{spec['code']}")
                y -= 22
            if spec["issue"]:
                c.drawString(72, y, f"Issue Date / 发布日期：{spec['issue']}")
                y -= 22
            c.drawString(72, y, f"Effective Date / 生效日期：{spec['effective']}")
            y -= 34
            lines = list(spec["body"])
            if spec.get("core"):
                lines.append(spec["core"])
            y = draw_lines(c, lines, 72, y, size=11, leading=23)
            if spec.get("table"):
                y -= 10
                tbl = spec["table"]
                rows = tbl["rows"][:8] if spec.get("cross_page") else tbl["rows"]
                y = _draw_table(c, tbl["header"], rows, y, font,
                                "附表（跨页）" if spec.get("cross_page") else "附表")
        elif p == 2 and spec.get("cross_page"):
            c.setFont(font, 12)
            c.drawString(72, y, "附表（续）：住宿标准明细（单位：元／每人每晚）")
            y -= 30
            tbl = spec["table"]
            y = _draw_table(c, tbl["header"], tbl["rows"][8:], y, font, "附表（续）")
            y = draw_lines(c, ["本表与主表一并执行。"], 72, y - 16)
        elif p in spec.get("special", {}):
            c.setFont(font, 14)
            c.drawString(72, y, "第二章　标准调整")
            y -= 36
            y = draw_lines(c, spec["special"][p], 72, y, size=12, leading=26)
            y = draw_lines(c, ["Note: this notice replaces the earlier standard."],
                           72, y - 16, size=10, leading=20)
        else:
            idx = p - 2
            lines = appendix_lines(spec, p, idx)
            y = draw_lines(c, lines, 72, y, size=10, leading=21)
        c.setFont(font, 9)
        c.drawCentredString(W / 2, 40, str(p))
        c.showPage()
    c.save()

    if spec.get("rotate_last"):
        import fitz
        with fitz.open(str(path)) as doc:
            page = doc[-1]
            page.set_rotation(90)
            page.insert_text((60, A4[0] - 90),
                             "Note: rotated page — 附表与主体条款一并生效。",
                             fontsize=11, fontname="china-s", rotate=0)
            tmp = str(path) + ".tmp"
            doc.save(tmp, deflate=True)
        os.replace(tmp, str(path))
    return path


# ============================================================ DOCX

def make_docx(spec: dict):
    from docx import Document
    from docx.oxml.ns import qn
    from docx.shared import Pt

    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "SimSun"
    normal.font.size = Pt(10.5)
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), "SimSun")

    doc.add_heading(spec["short"], level=1)
    if spec["code"]:
        doc.add_paragraph(f"制度编号 / Policy No.：{spec['code']}")
    doc.add_paragraph(f"发布日期 / Issue Date：{spec['issue']}")
    doc.add_paragraph(f"本办法自 {spec['effective']} 起施行。")
    doc.add_paragraph(f"Effective Date: {spec['effective']}")
    for line in spec["body"]:
        doc.add_paragraph(line)
    if spec.get("core"):
        doc.add_paragraph(spec["core"])
    if spec.get("table"):
        tbl = doc.add_table(rows=1 + len(spec["table"]["rows"]), cols=3)
        tbl.style = "Table Grid"
        for j, v in enumerate(spec["table"]["header"]):
            tbl.rows[0].cells[j].text = v
        for i, row in enumerate(spec["table"]["rows"], 1):
            for j, v in enumerate(row):
                tbl.rows[i].cells[j].text = v
    for p in range(2, spec["pages"] + 1):
        doc.add_page_break()
        doc.add_heading(APPENDIX_TITLES[(p - 2) % len(APPENDIX_TITLES)], level=2)
        for line in appendix_lines(spec, p, p - 2):
            doc.add_paragraph(line)
    path = OUT / f"{spec['name']}.docx"
    doc.save(str(path))
    return path


# ============================================================ 问题集

QUESTIONS = [
    dict(id="Q01", question="2026年6月30日普通员工在上海住宿，每人每晚的上限是多少？",
         scope=["T01", "T03"], expect=dict(status="answered", amount="500", source="T01")),
    dict(id="Q02", question="2026年7月1日普通员工在上海住宿，每人每晚的上限是多少？",
         scope=["T01", "T03"], expect=dict(status="answered", amount="800", source="T03")),
    dict(id="Q03", question="普通员工去上海出差住宿，每晚能报多少？早餐能单报吗？",
         scope=["T01", "T03"], expect_multi=[
             dict(status="needs_condition"), dict(status="no_evidence")]),
    dict(id="Q04", question="2026年7月10日普通员工在上海住宿，每人每晚能报多少？",
         scope=["T01", "T04"], expect=dict(status="conflict")),
    dict(id="Q05", question="普通员工在上海住宿，每晚能报多少？",
         scope=["T05"], expect=dict(status="coverage_gap", attachment="住宿标准表")),
    dict(id="Q06", question="普通员工在上海住宿，每晚能报多少？",
         scope=["T02"], expect=dict(status="answered", amount="600", source="T02")),
    # 按一类/二类城市列档但没有上海归属条文：安全结果是不给确定金额。
    dict(id="Q07", question="上海普通员工住宿标准是多少？",
         scope=["T09"], expect=dict(status="no_evidence")),
    dict(id="Q08", question="北京普通员工住宿标准是多少？",
         scope=["T10"], expect=dict(status="answered", amount="550")),
    dict(id="Q09", question="出差餐补每人每天多少？",
         scope=["T11"], expect=dict(status="answered", amount="80")),
    dict(id="Q10", question="市内交通补贴每人每天多少？",
         scope=["T08"], expect=dict(status="answered", amount="30")),
    dict(id="Q11", question="出差期间的早餐能单独报销吗？",
         scope=["T01"], expect=dict(status="no_evidence")),
    dict(id="Q12", question="2026年7月20日普通员工在上海住宿、餐补和早餐分别能报多少？",
         scope=["T01", "T03", "T11"], expect_multi=[
             dict(status="answered", amount="800"),
             dict(status="answered", amount="80"),
             dict(status="no_evidence")]),
]


def main():
    manifest = {
        "name": "TraceQA 30 份混杂制度测试库",
        "note": "全部为虚构制度，仅用于功能与性能验收。",
        "target_pages": 500,
        "files": [],
    }
    total = 0
    for spec in SPECS:
        if spec["fmt"] == "pdf":
            p = make_pdf(spec, _cn_font())
        else:
            p = make_docx(spec)
        manifest["files"].append({
            "id": spec["id"], "file": p.name,
            "format": spec["fmt"].upper(),
            "pages": spec["pages"],
            "page_basis": "PDF 物理页" if spec["fmt"] == "pdf" else "Word 参考页（固定分页）",
            "family": spec["family"], "code": spec["code"],
            "effective": spec["effective"], "flags": spec["flags"],
            "core_fact": spec["core"],
        })
        total += spec["pages"]
        print(f"  {spec['id']:>3}  {p.name:<40} {spec['fmt'].upper():<4} {spec['pages']:>3} 页")
    manifest["total_pages"] = total
    (OUT / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (OUT / "test_questions.json").write_text(
        json.dumps(QUESTIONS, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n共 {len(SPECS)} 份，合计 {total} 页（目标约 500 页）")
    print("清单：", OUT / "manifest.json")
    print("问题集：", OUT / "test_questions.json")


if __name__ == "__main__":
    main()
