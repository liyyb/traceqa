"""正式记录与更正生命周期（手册 §2.2、§4.3）。

签发即冻结：结论、说明、材料信息和模板版本一次性冻结；生成记录编号；
重复下载同一编号返回同一内容。更正生成新版本，旧版保持逐字节一致。
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime
from typing import Optional

from . import db as DB
from .config import Config

TEMPLATE_VERSION = "traceqa-template-1"


class RecordService:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    # ------------------------------------------------------------ 预览

    def preview(self, conn: sqlite3.Connection, run_id: str) -> dict:
        """GET /runs/{id}/export 预览：只读草稿视图，无正式编号。"""
        row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return {"error": "任务不存在", "status": 404}
        if row["status"] != DB.RUN_COMPLETED:
            return {"error": "任务未完成，不能生成核查记录",
                    "status": 409, "run_status": row["status"],
                    "actions": ["等待任务完成", "重试失败任务"] if
                    row["status"] == DB.RUN_FAILED else ["等待任务完成"]}
        snap = self._collect_snapshot(conn, run_id)
        from .export_html import render_record_html
        return {"draft": True, "snapshot": snap,
                "summary": self._summary(snap),
                "html": render_record_html({**snap, "serial_no": "草稿",
                                            "rev_no": "", "issued_at": "",
                                            "draft_watermark": True})}

    def _summary(self, snap: dict) -> dict:
        confirmed = [c for c in snap["claims"] if c["status"] == DB.CLAIM_ANSWERED]
        pending = [c for c in snap["claims"]
                   if c["status"] in (DB.CLAIM_CONFLICT, DB.CLAIM_NEEDS_CONDITION)]
        gaps = [c for c in snap["claims"]
                if c["status"] in (DB.CLAIM_COVERAGE_GAP, DB.CLAIM_NO_EVIDENCE)]
        return {"confirmed": len(confirmed), "pending": len(pending),
                "gaps": len(gaps),
                "confirmed_ids": [c["id"] for c in confirmed],
                "pending_ids": [c["id"] for c in pending],
                "gap_ids": [c["id"] for c in gaps]}

    # ------------------------------------------------------------ 快照

    def _collect_snapshot(self, conn: sqlite3.Connection, run_id: str) -> dict:
        run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        scope = json.loads(run["scope_json"])
        materials = []
        for rid in scope:
            r = conn.execute(
                """SELECT r.id AS rev_id, v.id AS version_id, v.file_name, v.sha256,
                          d.title, r.coverage_json, v.imported_at
                   FROM parse_revisions r JOIN versions v ON v.id=r.version_id
                   JOIN documents d ON d.id=v.document_id WHERE r.id=?""",
                (rid,)).fetchone()
            if not r:
                continue
            notes = conn.execute(
                "SELECT field, value, source, signature, created_at FROM metadata_notes "
                "WHERE version_id=? ORDER BY created_at", (r["version_id"],)).fetchall()
            fields = conn.execute(
                "SELECT field, value, source, note FROM document_fields "
                "WHERE version_id=?", (r["version_id"],)).fetchall()
            cov = json.loads(r["coverage_json"]) if r["coverage_json"] else {}
            materials.append({
                "revision_id": r["rev_id"], "version_id": r["version_id"],
                "file_name": r["file_name"], "title": r["title"],
                "sha256_short": r["sha256"][:12],
                "coverage": cov or None,
                "document_fields": {f["field"]: dict(f) for f in fields},
                "cover_status": cov.get("cover_status"),
                "metadata_notes": [dict(n) for n in notes],
            })
        claims = conn.execute(
            "SELECT * FROM claims WHERE run_id=? ORDER BY seq_no", (run_id,)).fetchall()
        claim_out = []
        for c in claims:
            ces = conn.execute(
                """SELECT ce.evidence_id, ce.relation, ce.group_no, ce.quote,
                          e.locator_json, e.heading_path_json, e.text_orig,
                          e.revision_id, r.version_id,
                          d.title AS doc_title, v.file_name, v.sha256
                   FROM claim_evidence ce
                   JOIN evidence e ON e.id = ce.evidence_id
                   JOIN parse_revisions r ON r.id = e.revision_id
                   JOIN versions v ON v.id = r.version_id
                   JOIN documents d ON d.id = v.document_id
                   WHERE ce.claim_id=? ORDER BY ce.group_no, ce.rowid""",
                (c["id"],)).fetchall()
            notes = conn.execute(
                "SELECT body, source, contact_time, signature, recorded_at "
                "FROM notes WHERE claim_id=? AND status='saved' ORDER BY recorded_at",
                (c["id"],)).fetchall()
            claim_out.append({
                "id": c["id"], "seq_no": c["seq_no"], "topic": c["topic"],
                "question_text": c["question_text"], "status": c["status"],
                "conclusion_text": c["conclusion_text"],
                "conditions": json.loads(c["conditions_json"] or "{}"),
                "followup": json.loads(c["followup_json"]) if c["followup_json"] else None,
                "gap": json.loads(c["gap_json"]) if c["gap_json"] else None,
                "evidences": [{
                    "evidence_id": ce["evidence_id"], "relation": ce["relation"],
                    "group_no": ce["group_no"], "quote": ce["quote"],
                    "revision_id": ce["revision_id"],
                    "version_id": ce["version_id"],
                    "locator": json.loads(ce["locator_json"]),
                    "heading_path": json.loads(ce["heading_path_json"] or "[]"),
                    "text_orig": ce["text_orig"], "doc_title": ce["doc_title"],
                    "file_name": ce["file_name"], "sha256_short": ce["sha256"][:12],
                } for ce in ces],
                "notes": [dict(n) for n in notes],
            })
        return {
            "run_id": run_id,
            "question": run["question"],
            "conditions": json.loads(run["conditions_json"]),
            "materials": materials,
            "claims": claim_out,
            "model_version": run["model_version"],
            "rules_version": run["rules_version"],
            "template_version": TEMPLATE_VERSION,
        }

    # ------------------------------------------------------------ 签发

    def issue(self, conn: sqlite3.Connection, run_id: str,
              request_id: str) -> tuple[int, dict]:
        conn.execute("BEGIN IMMEDIATE")
        # 幂等：同一请求 ID 返回已签发记录
        idem = conn.execute(
            "SELECT id FROM records WHERE json_extract(frozen_json,'$.issue_request_id')=?",
            (request_id,)).fetchone() if _has_json1(conn) else None
        if idem is not None:
            rec = self.get(conn, idem["id"])
            if rec["run_id"] != run_id or rec["rev_no"] != "R1":
                return 409, {"error": "相同请求 ID 已用于其他记录操作"}
            return 200, rec

        row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return 404, {"error": "任务不存在"}
        if row["status"] != DB.RUN_COMPLETED:
            return 409, {"error": "仅已完成的核查可生成正式记录",
                         "run_status": row["status"]}
        # 同一 run 只允许一张 R1：不同 request_id 重复点击也不能重复签发。
        existing = conn.execute(
            "SELECT id FROM records WHERE run_id=? AND rev_no='R1' LIMIT 1",
            (run_id,)).fetchone()
        if existing is not None:
            return 200, self.get(conn, existing["id"])

        snap = self._collect_snapshot(conn, run_id)
        serial = self._next_serial(conn)
        rev_no = "R1"
        record_id = DB.new_id("rec")
        snap["issue_request_id"] = request_id
        snap["serial_no"] = serial
        snap["rev_no"] = rev_no
        snap["issued_at"] = DB.now_iso()

        from .export_html import render_record_html
        html = render_record_html(snap)
        data = html.encode("utf-8")
        content_hash = hashlib.sha256(data).hexdigest()
        self.cfg.exports_dir.mkdir(parents=True, exist_ok=True)
        file_path = self.cfg.exports_dir / f"{serial}-{rev_no}.html"
        try:
            file_path.write_bytes(data)  # 文件落盘成功后才公布编号
        except OSError as e:
            return 507, {"error": f"磁盘写入失败，草稿已保留可重试：{e}"}

        conn.execute(
            "INSERT INTO records(id, run_id, serial_no, rev_no, parent_record_id, "
            "correction_reason, frozen_json, file_path, content_hash, created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (record_id, run_id, serial, rev_no, None, None,
             json.dumps(snap, ensure_ascii=False), str(file_path),
             content_hash, DB.now_iso()))
        conn.commit()
        return 201, self.get(conn, record_id)

    def _next_serial(self, conn: sqlite3.Connection) -> str:
        today = datetime.now().strftime("%Y%m%d")
        prefix = f"TQ-{today}-"
        rows = conn.execute(
            "SELECT serial_no FROM records WHERE serial_no LIKE ?",
            (prefix + "%",)).fetchall()
        nums = set()
        for r in rows:
            m = re.fullmatch(re.escape(prefix) + r"(\d+)", r["serial_no"])
            if m:
                nums.add(int(m.group(1)))
        n = max(nums) + 1 if nums else 1
        return f"{prefix}{n:03d}"

    # ------------------------------------------------------------ 更正

    def correct(self, conn: sqlite3.Connection, record_id: str, base_rev_no: str,
                reason: str, note_updates: list[dict],
                request_id: str) -> tuple[int, dict]:
        """note_updates: [{claim_id, body, source, contact_time, signature}]"""
        conn.execute("BEGIN IMMEDIATE")
        rec_row = conn.execute("SELECT * FROM records WHERE id=?",
                               (record_id,)).fetchone()
        if rec_row is None:
            return 404, {"error": "记录不存在"}
        if request_id and _has_json1(conn):
            issued = conn.execute(
                "SELECT id, parent_record_id FROM records "
                "WHERE json_extract(frozen_json,'$.issue_request_id')=?", (request_id,)
            ).fetchone()
            if issued:
                if issued["parent_record_id"] != record_id:
                    return 409, {"error": "相同请求 ID 已用于其他记录操作"}
                return 200, self.get(conn, issued["id"])
        serial = rec_row["serial_no"]
        # 基准过期检查：找到该系列最新版本
        latest_row = conn.execute(
            "SELECT * FROM records WHERE serial_no=? "
            "ORDER BY CAST(substr(rev_no, 2) AS INTEGER) DESC LIMIT 1",
            (serial,)).fetchone()
        if latest_row["rev_no"] != base_rev_no or latest_row["id"] != record_id:
            return 409, {"error": "基准版本已过期",
                         "current_rev": latest_row["rev_no"],
                         "current_id": latest_row["id"]}
        if not reason or not reason.strip():
            return 400, {"error": "更正原因为必填"}
        if not isinstance(note_updates, list) or not note_updates:
            return 400, {"error": "没有实际改动：请提供 note_updates"}

        old_snap = json.loads(latest_row["frozen_json"])
        new_snap = json.loads(json.dumps(old_snap))  # deep copy
        changes = []
        for upd in note_updates:
            claim = next((c for c in new_snap["claims"] if c["id"] == upd.get("claim_id")),
                         None)
            if claim is None:
                continue
            old_notes = claim.get("notes", [])
            old_note = old_notes[0] if old_notes else {}
            new_note = {
                "body": (upd.get("body") or old_note.get("body") or "").strip(),
                "source": (upd.get("source") or old_note.get("source") or "").strip(),
                "contact_time": upd.get("contact_time", old_note.get("contact_time")),
                "signature": upd.get("signature", old_note.get("signature")),
                "recorded_at": DB.now_iso(),
            }
            if not new_note["body"] or not new_note["source"]:
                return 400, {"error": "来源和正文必填"}
            claim["notes"] = [new_note]
            for field, label in (("body", "人工说明正文"), ("source", "信息来源"),
                                 ("contact_time", "沟通时间"), ("signature", "记录人")):
                before = old_note.get(field) or ""
                after = new_note.get(field) or ""
                if before != after:
                    changes.append({"claim_id": claim["id"], "topic": claim["topic"],
                                    "field": label, "before": before, "after": after})
        if not changes:
            conn.rollback()
            return 400, {"error": "没有实际改动"}

        m = re.fullmatch(r"R(\d+)", base_rev_no or "")
        if m is None:
            conn.rollback()
            return 400, {"error": "基准版本号无效"}
        new_rev = f"R{int(m.group(1)) + 1}"
        new_snap["rev_no"] = new_rev
        new_snap["issued_at"] = DB.now_iso()
        new_snap["correction"] = {
            "reason": reason.strip(),
            "prev_rev": base_rev_no,
            "prev_record_id": latest_row["id"],
            "corrected_at": DB.now_iso(),
            "changes": changes,
        }
        new_snap.pop("issue_request_id", None)
        new_snap["issue_request_id"] = request_id

        from .export_html import render_record_html
        html = render_record_html(new_snap)
        data = html.encode("utf-8")
        content_hash = hashlib.sha256(data).hexdigest()
        file_path = self.cfg.exports_dir / f"{serial}-{new_rev}.html"
        try:
            file_path.write_bytes(data)
        except OSError as e:
            return 507, {"error": f"磁盘写入失败：{e}"}

        new_id = DB.new_id("rec")
        try:
            conn.execute(
                "INSERT INTO records(id, run_id, serial_no, rev_no, parent_record_id, "
                "correction_reason, frozen_json, file_path, content_hash, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (new_id, rec_row["run_id"], serial, new_rev, latest_row["id"],
                 reason.strip(), json.dumps(new_snap, ensure_ascii=False),
                 str(file_path), content_hash, DB.now_iso()))
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()
            return 409, {"error": "版本已更新：另一窗口已提交更正，请基于最新版本重试",
                         "current_rev": new_rev}
        return 201, self.get(conn, new_id)

    # ------------------------------------------------------------ 读取

    def get(self, conn: sqlite3.Connection, record_id: str) -> Optional[dict]:
        row = conn.execute("SELECT * FROM records WHERE id=?",
                           (record_id,)).fetchone()
        if row is None:
            return None
        chain = conn.execute(
            "SELECT id, rev_no, created_at FROM records WHERE serial_no=? "
            "ORDER BY CAST(substr(rev_no, 2) AS INTEGER)", (row["serial_no"],)).fetchall()
        return {
            "id": row["id"], "run_id": row["run_id"],
            "serial_no": row["serial_no"], "rev_no": row["rev_no"],
            "parent_record_id": row["parent_record_id"],
            "correction_reason": row["correction_reason"],
            "created_at": row["created_at"],
            "content_hash": row["content_hash"],
            "chain": [dict(c) for c in chain],
            "snapshot": json.loads(row["frozen_json"]),
        }

    def download(self, conn: sqlite3.Connection, record_id: str) -> tuple[int, bytes, str]:
        row = conn.execute("SELECT * FROM records WHERE id=?",
                           (record_id,)).fetchone()
        if row is None:
            return 404, b"", ""
        from pathlib import Path
        p = self.cfg.exports_dir / Path(row["file_path"]).name
        if not p.exists():
            return 410, b"", "记录文件丢失，请从备份恢复；不会用新模板重造旧件"
        data = p.read_bytes()
        if hashlib.sha256(data).hexdigest() != row["content_hash"]:
            return 410, b"", "记录文件校验失败，请从备份恢复"
        return 200, data, p.name

    def list_records(self, conn: sqlite3.Connection, q: Optional[str] = None) -> list[dict]:
        rows = conn.execute(
            "SELECT * FROM records ORDER BY created_at DESC").fetchall()
        out = []
        for row in rows:
            snap = json.loads(row["frozen_json"])
            text = json.dumps(snap, ensure_ascii=False)
            if q and q not in text and q not in row["serial_no"]:
                continue
            out.append({
                "id": row["id"], "run_id": row["run_id"],
                "serial_no": row["serial_no"], "rev_no": row["rev_no"],
                "created_at": row["created_at"],
                "question": snap.get("question", ""),
                "correction_reason": row["correction_reason"],
            })
        return out


def _has_json1(conn: sqlite3.Connection) -> bool:
    try:
        conn.execute("SELECT json_extract('{}','$.a')").fetchone()
        return True
    except sqlite3.OperationalError:
        return False
