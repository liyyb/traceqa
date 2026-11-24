"""离线核查记录 HTML（手册 §2.1、§2.2、§3）：自包含、可打印、黑白可辨识。

依据块顺序：制度全称、制度编号、文件中注明的日期及其含义、导入时文件名、
定位、原句。每条依据分配文内引用序号 [n]，句末编号与脚注行都能跳到本
导出件的摘录；打印成纸张后仍能从编号查到位置。
"""
from __future__ import annotations

import html

STATUS_TXT = {
    "answered": "有据可答",
    "needs_condition": "需补条件",
    "no_evidence": "未找到依据",
    "conflict": "待确认冲突",
    "coverage_gap": "材料覆盖不足",
}

DATE_FIELDS = (("发布日期", "文件注明的发布日期"),
               ("生效日期", "文件注明的生效日期"))


def human_locator(loc: dict, heading_path: list[str]) -> str:
    if loc.get("kind") == "pdf":
        page = loc.get("page_index", 0) + 1
        printed = loc.get("printed_page")
        page_txt = f"物理第 {page} 页" + (f"（印刷页码 {printed}）" if printed else "")
        boxes = loc.get("boxes", [])
        box_txt = ""
        if boxes:
            b = boxes[0]
            box_txt = (f"，区域 左{b['x0']:.0%} 上{b['y0']:.0%} "
                       f"右{b['x1']:.0%} 下{b['y1']:.0%}")
        head = " › ".join(heading_path)
        return f"{page_txt}{box_txt}" + (f" · {head}" if head else "")
    parts = []
    if heading_path:
        parts.append(" › ".join(heading_path))
    if loc.get("table_row") is not None:
        parts.append(f"表格 行{loc['table_row'] + 1}／列{loc['table_col'] + 1}")
    else:
        parts.append(f"段落（块路径 {'/'.join(map(str, loc.get('block_path', [])))}）")
    return " · ".join(parts)


def _field_text(mat: dict, field: str) -> str:
    """原件字段与经办人补录分行取用；补录不印成原件字段。"""
    orig = (mat.get("document_fields") or {}).get(field)
    if orig:
        return str(orig.get("value", ""))
    for n in mat.get("metadata_notes", []):
        if n["field"] == field:
            return f"{n['value']}（经办人补录，来源：{n['source']}）"
    if field == "制度编号":
        if mat.get("cover_status") == "unreadable":
            return "封面无法读取，编号待核"
        return "原件未载明"
    return "原件未载明"


def _date_lines(mat: dict) -> list[str]:
    out = []
    for field, meaning in DATE_FIELDS:
        orig = (mat.get("document_fields") or {}).get(field)
        if orig:
            out.append(f"{meaning}：{orig.get('value', '')}")
            continue
        for n in mat.get("metadata_notes", []):
            if n["field"] == field:
                out.append(f"{meaning}：{n['value']}（经办人补录，来源：{n['source']}；"
                           f"不自动作为条款生效或替代旧规的依据）")
                break
    return out


def render_record_html(snap: dict) -> str:
    esc = html.escape
    serial = snap.get("serial_no", "草稿")
    rev = snap.get("rev_no", "")
    issued = snap.get("issued_at", "")
    correction = snap.get("correction")
    draft = bool(snap.get("draft_watermark"))

    # 引用编号：按整份答复首次出现顺序分配；重复引用同一位置沿用编号
    cite_no: dict[str, int] = {}
    order: list[dict] = []
    for c in snap["claims"]:
        for ev in c["evidences"]:
            key = ev["evidence_id"]
            if key not in cite_no:
                cite_no[key] = len(cite_no) + 1
                order.append(ev)

    def num(ev) -> int:
        return cite_no[ev["evidence_id"]]

    def sup(ev, back: bool = False) -> str:
        n = num(ev)
        href = f"#quote-{n}" if back else f"#ref-{n}"
        return f"<sup class='cite'><a href='{href}' aria-label='查看依据 {n}'>[{n}]</a></sup>"

    parts: list[str] = []
    parts.append(f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TraceQA 核查记录 {esc(serial)} {esc(rev)}</title>
<style>
 body{{font-family:"Microsoft YaHei","微软雅黑",sans-serif;background:#F7F3E9;
      color:#292923;margin:0;padding:32px;line-height:1.8;font-size:16px}}
 .sheet{{max-width:860px;margin:0 auto}}
 header{{border-bottom:2px solid #292923;padding-bottom:12px;margin-bottom:20px}}
 h1{{font-size:22px;margin:4px 0}}
 .meta{{color:#5C5950;font-size:14px}}
 .claim{{margin:28px 0;padding:16px 20px;border:1px solid #d8d2c2;border-radius:6px;
        background:#fffdf7;break-inside:avoid-page}}
 .claim h2{{font-size:18px;margin:0 0 8px}}
 .status{{font-size:14px;padding:2px 10px;border-radius:4px;background:#eee7d6;
         color:#5C5950;margin-left:8px}}
 .conflict{{color:#814619;font-weight:700}}
 .amount{{font-weight:700}}
 .quote{{margin:6px 0;padding-left:12px;border-left:3px solid #315C54}}
 .loc{{color:#5C5950;font-size:14px}}
 .note{{margin-top:10px;padding:10px 12px;background:#f1ece0;border-radius:6px}}
 .note .src{{color:#5C5950;font-size:14px}}
 .foot{{margin-top:10px;font-size:14px;color:#5C5950}}
 .foot a{{color:#315C54}}
 table{{border-collapse:collapse;width:100%;margin:10px 0}}
 td,th{{border:1px solid #b9b2a0;padding:6px 10px;text-align:left;vertical-align:top}}
 th{{background:#eee7d6}}
 .corr{{border:2px solid #814619;padding:12px 16px;border-radius:6px;margin:16px 0;
       break-inside:avoid-page}}
 footer{{margin-top:32px;color:#5C5950;font-size:13px;border-top:1px solid #b9b2a0;
        padding-top:10px}}
 sup a, .cite, a{{color:#315C54;text-decoration:underline}}
 ol.refs li{{margin:10px 0;break-inside:avoid-page}}
 .runhead{{display:none}}
 .draft{{display:inline-block;background:#814619;color:#fff;padding:2px 10px;
         border-radius:4px;font-size:14px;margin-left:8px}}
 @media print{{
   body{{background:#fff}}
   .claim{{border-color:#000}}
   a{{color:#000;text-decoration:none}}
   .runhead{{display:block;position:fixed;top:0;left:0;right:0;
            font-size:12px;color:#5C5950;border-bottom:1px solid #999;
            background:#fff;padding:4px 0}}
   .sheet{{padding-top:18px}}
 }}
</style></head><body><div class="sheet">
<div class="runhead">TraceQA 核查记录 · {esc(serial)} / {esc(rev)} · 出具时间 {esc(issued)}</div>""")

    parts.append(f"<header><div class='meta'>TraceQA 核查记录 · 本机出具 · "
                 f"编号 <b>{esc(serial)} / {esc(rev)}</b> · 出具时间 {esc(issued)}"
                 + ("<span class='draft'>草稿 · 未签发</span>" if draft else "")
                 + "</div>")
    parts.append(f"<h1>核查记录 {esc(serial)} {esc(rev)}</h1>")
    parts.append(f"<div class='meta'>问题：{esc(snap.get('question',''))}</div>")
    conds = snap.get("conditions") or {}
    if conds:
        parts.append("<div class='meta'>条件：" +
                     esc("、".join(f"{k}={v}" for k, v in conds.items())) + "</div>")
    parts.append("</header>")

    if correction:
        parts.append("<div class='corr'><b>更正版说明</b><br>")
        parts.append(f"前一版本完整编号：{esc(serial)} / {esc(correction.get('prev_rev',''))} · "
                     f"更正时间：{esc(correction.get('corrected_at',''))}<br>")
        parts.append(f"更正原因：{esc(correction.get('reason',''))}")
        parts.append("<table><tr><th>事项</th><th>改动字段</th>"
                     "<th>改动前</th><th>改动后</th></tr>")
        for ch in correction.get("changes", []):
            parts.append(f"<tr><td>{esc(ch.get('topic',''))}</td>"
                         f"<td>{esc(ch.get('field',''))}</td>"
                         f"<td>{esc(ch.get('before',''))}</td>"
                         f"<td>{esc(ch.get('after',''))}</td></tr>")
        parts.append("</table></div>")

    for c in snap["claims"]:
        st = c["status"]
        cls = "conflict" if st == "conflict" else ""
        parts.append(f"<div class='claim' id='claim-{esc(c['id'])}'>")
        parts.append(f"<h2 class='{cls}'>{esc(c['topic'])}　"
                     f"<span class='status'>{STATUS_TXT.get(st, st)}</span></h2>")
        if c.get("conclusion_text"):
            # 句末编号：该事项首次出现的全部依据，点击跳到本导出件的依据清单
            cites = "".join(sup(ev) for ev in _unique_evidences(c["evidences"]))
            parts.append(f"<p>{esc(c['conclusion_text'])}{cites}</p>")
        if c.get("conditions"):
            parts.append("<div class='meta'>适用条件：" +
                         esc("、".join(f"{v}" for v in c["conditions"].values())) + "</div>")
        for gi, g in enumerate(_group_evidences(c)):
            if len(g) > 1 or c["status"] == "conflict":
                parts.append(f"<div class='meta'>依据组 {gi + 1}</div>")
            for ev in g:
                n = num(ev)
                parts.append(
                    f"<div class='quote' id='quote-{n}'>"
                    f"{esc(ev.get('quote') or ev.get('text_orig') or '')} "
                    f"{sup(ev)}</div>")
        # 事项内脚注：句末编号与脚注行打开同一处依据（黑白打印靠编号定位）
        uniq = _unique_evidences(c["evidences"])
        if uniq:
            parts.append("<div class='foot'>")
            for ev in uniq:
                n = num(ev)
                parts.append(
                    f"<div>[{n}] <a href='#ref-{n}'>{esc(ev.get('doc_title',''))} · "
                    f"{esc(human_locator(ev['locator'], ev.get('heading_path') or []))}</a></div>")
            parts.append("</div>")
        if c.get("gap"):
            gap = c["gap"]
            parts.append(f"<div class='loc'>缺口位置：{esc(gap.get('location',''))}；"
                         f"已读条款：{esc(gap.get('clause',''))}；"
                         f"缺失：{esc(gap.get('attachment',''))}</div>")
        for n in c.get("notes", []):
            parts.append(
                f"<div class='note'><b>人工说明（经办人记录，非制度条款，"
                f"不构成制度签批）</b><br>"
                f"{esc(n.get('body',''))}<div class='src'>来源：{esc(n.get('source',''))}"
                + (f"；沟通时间：{esc(n.get('contact_time',''))}" if n.get('contact_time') else "")
                + f"；记录人：{esc(n.get('signature','') or '未署名')}"
                + f"；记录时间：{esc(n.get('recorded_at',''))}</div></div>")
        parts.append("</div>")

    # 依据清单：制度全称、编号、日期及含义、导入文件名、定位、原句
    parts.append("<h2>依据清单</h2><ol class='refs'>")
    for ev in order:
        n = num(ev)
        loc_txt = human_locator(ev["locator"], ev.get("heading_path") or [])
        mat = next((m for m in snap["materials"]
                    if m["revision_id"] == _rev_of(ev)), None)
        lines: list[str] = []
        if mat:
            lines.append(f"制度全称：{mat['title']}")
            lines.append(f"制度编号：{_field_text(mat, '制度编号')}")
            lines.extend(_date_lines(mat))
            lines.append(f"导入时文件名：{mat['file_name']}")
            lines.append(f"版本摘要（系统内部）：{mat['sha256_short']}")
        else:
            lines.append("制度全称／编号：该依据所属版本不在本次材料清单中")
        lines.append(f"定位：{loc_txt}")
        parts.append(
            f"<li id='ref-{n}' value='{n}'>"
            f"<b><a href='#quote-{n}'>[{n}]</a> {esc(ev.get('doc_title',''))}</b><br>"
            f"<span class='loc'>{'<br>'.join(esc(s) for s in lines)}</span><br>"
            f"原句：{esc(ev.get('quote') or ev.get('text_orig') or '')}</li>")
    parts.append("</ol>")

    # 材料清单：原件字段与经办人补录分行
    parts.append("<h2>材料范围</h2><table><tr><th>文件</th><th>制度</th>"
                 "<th>版本摘要</th><th>原件字段</th><th>经办人补录</th></tr>")
    for m in snap["materials"]:
        orig_txt = "；".join(f"{f}={v['value']}" for f, v in
                            (m.get("document_fields") or {}).items()) or "—"
        notes_txt = "；".join(f"{n['field']}={n['value']}（来源：{n['source']}）"
                              for n in m.get("metadata_notes", [])) or "—"
        parts.append(f"<tr><td>{esc(m['file_name'])}</td><td>{esc(m['title'])}</td>"
                     f"<td>{esc(m['sha256_short'])}</td><td>{esc(orig_txt)}</td>"
                     f"<td>{esc(notes_txt)}</td></tr>")
    parts.append("</table>")

    parts.append(f"<footer>本记录由 TraceQA 在本机生成。复核人请凭上述制度全称、"
                 f"制度编号、日期、导入时文件名、版本摘要与定位信息核对所持原件。"
                 f"人工说明为经办人自报记录，不构成制度签批。模板版本 "
                 f"{esc(snap.get('template_version',''))} · "
                 f"规则版本 {esc(snap.get('rules_version',''))}</footer>")
    parts.append("</div></body></html>")
    return "".join(parts)


def _unique_evidences(evs: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for ev in evs:
        k = ev["evidence_id"]
        if k not in seen:
            seen.add(k)
            out.append(ev)
    return out


def _group_evidences(claim: dict) -> list[list[dict]]:
    groups: dict[int, list[dict]] = {}
    for ev in claim["evidences"]:
        groups.setdefault(ev["group_no"], []).append(ev)
    return [groups[k] for k in sorted(groups)]


def _rev_of(ev: dict) -> str:
    return ev.get("revision_id", "") or ""


def _doc_no(mat: dict) -> str:
    return _field_text(mat, "制度编号")
