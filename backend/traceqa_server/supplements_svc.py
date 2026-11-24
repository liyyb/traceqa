"""补件关联、受影响记录、一键重查与前后对照（手册 §2.4、§4.4）。"""
from __future__ import annotations

import json
import sqlite3
from typing import Optional

from . import db as DB


class SupplementService:

    def link(self, conn: sqlite3.Connection, original_revision_id: str,
             gap: dict, supplement_revision_id: str) -> tuple[int, dict]:
        row = conn.execute(
            "SELECT status FROM parse_revisions WHERE id=?",
            (supplement_revision_id,)).fetchone()
        if row is None or row["status"] != "ready":
            return 400, {"error": "补件修订不可用：请先完成补件导入"}
        orig = conn.execute(
            "SELECT id FROM parse_revisions WHERE id=?",
            (original_revision_id,)).fetchone()
        if orig is None:
            return 404, {"error": "原修订不存在"}
        existed = conn.execute(
            "SELECT id FROM supplements WHERE original_revision_id=? AND "
            "supplement_revision_id=?",
            (original_revision_id, supplement_revision_id)).fetchone()
        if existed:
            return 200, {"id": existed["id"], "reused": True}
        sup_id = DB.new_id("sup")
        conn.execute(
            "INSERT INTO supplements(id, original_revision_id, gap_json, "
            "supplement_revision_id, status, created_at) VALUES(?,?,?,?,?,?)",
            (sup_id, original_revision_id, json.dumps(gap, ensure_ascii=False),
             supplement_revision_id, "linked", DB.now_iso()))
        conn.commit()
        # 补件解析成功后的反馈：可用证据数与仍未覆盖位置
        ev_count = conn.execute(
            "SELECT COUNT(*) c FROM evidence WHERE revision_id=?",
            (supplement_revision_id,)).fetchone()["c"]
        cov = conn.execute(
            "SELECT coverage_json FROM parse_revisions WHERE id=?",
            (supplement_revision_id,)).fetchone()["coverage_json"]
        return 201, {"id": sup_id, "supplement_evidence_count": ev_count,
                     "supplement_coverage": json.loads(cov) if cov else None}

    def affected_runs(self, conn: sqlite3.Connection, supplement_id: str,
                      limit: int = 50, offset: int = 0) -> dict:
        sup = conn.execute("SELECT * FROM supplements WHERE id=?",
                           (supplement_id,)).fetchone()
        if sup is None:
            return {"error": "补件关联不存在"}
        orig_rev = sup["original_revision_id"]
        supp_rev = sup["supplement_revision_id"]
        rows = conn.execute(
            "SELECT * FROM runs WHERE status IN (?,?) ORDER BY created_at DESC",
            (DB.RUN_COMPLETED, DB.RUN_FAILED)).fetchall()
        out = []
        for r in rows:
            scope = json.loads(r["scope_json"])
            in_scope = orig_rev in scope
            cited = conn.execute(
                """SELECT COUNT(*) c FROM claim_evidence ce
                   JOIN claims cl ON cl.id = ce.claim_id
                   WHERE cl.run_id=? AND ce.evidence_id IN
                     (SELECT id FROM evidence WHERE revision_id IN (?,?))""",
                (r["id"], orig_rev, supp_rev)).fetchone()["c"]
            if not in_scope and cited == 0:
                continue
            tags = []
            if cited > 0:
                tags.append("曾引用此材料")
            if in_scope:
                tags.append("材料在当时的核查范围内")
            out.append({
                "run_id": r["id"], "question": r["question"],
                "created_at": r["created_at"], "status": r["status"],
                "tags": tags, "cited": cited > 0,
            })
        total = len(out)
        return {"total": total, "runs": out[offset:offset + limit]}

    def rerun(self, conn: sqlite3.Connection, parent_run_id: str,
              supplement_revision_ids: list[str], request_id: str,
              run_service) -> tuple[int, dict]:
        """按旧问题重查：复制问题、条件、未变文件的旧修订，加入选定补件。"""
        parent = conn.execute("SELECT * FROM runs WHERE id=?",
                              (parent_run_id,)).fetchone()
        if parent is None:
            return 404, {"error": "原核查不存在"}
        scope = json.loads(parent["scope_json"])
        missing = []
        new_scope = []
        replaced: dict[str, str] = {}
        for rid in scope:
            ok = conn.execute(
                "SELECT id FROM parse_revisions WHERE id=? AND status='ready'",
                (rid,)).fetchone()
            if ok is None:
                missing.append(rid)
                continue
            new_scope.append(rid)
        if missing:
            return 409, {"error": "原范围中的必需原件缺失", "missing": missing}
        for sup_rev in supplement_revision_ids:
            row = conn.execute(
                "SELECT * FROM supplements WHERE supplement_revision_id=? ",
                (sup_rev,)).fetchone()
            if row and row["original_revision_id"] in new_scope:
                # 补件替换原修订在范围中的位置（保留原修订以便对照）
                replaced[row["original_revision_id"]] = sup_rev
            new_scope.append(sup_rev)
        # 去重保序
        seen = set()
        dedup = []
        for rid in new_scope:
            if rid not in seen:
                seen.add(rid)
                dedup.append(rid)
        meta = {"parent_run_id": parent_run_id,
                "added_revisions": supplement_revision_ids,
                "replaced": replaced}
        status, body = run_service.submit(
            conn, request_id, parent["question"],
            dedup, json.loads(parent["conditions_json"]),
            parent_run_id=parent_run_id, rerun_meta=meta)
        return status, body

    def comparison(self, conn: sqlite3.Connection, run_id: str) -> Optional[dict]:
        run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None or not run["parent_run_id"]:
            return None
        if run["status"] != DB.RUN_COMPLETED:
            return {"error": "重查完成后才提供对照", "status": run["status"]}
        parent = self._mini_snapshot(conn, run["parent_run_id"])
        child = self._mini_snapshot(conn, run_id)
        items = []
        p_by_item = {(c["seq_no"], c["question_text"]): c for c in parent["claims"]}
        matched = set()
        for c in child["claims"]:
            item = (c["seq_no"], c["question_text"])
            p = p_by_item.get(item)
            entry = {"topic": c["topic"], "question_text": c["question_text"],
                     "old": _claim_view(p) if p else {"label": "旧项缺失"},
                     "new": _claim_view(c),
                     "changed": (not p) or p["status"] != c["status"]
                     or p.get("conclusion_text") != c.get("conclusion_text")}
            if p:
                matched.add(item)
                old_ev = {e["evidence_id"] for e in p["evidences"]}
                new_ev = {e["evidence_id"] for e in c["evidences"]}
                entry["evidence_diff"] = {
                    "kept": len(old_ev & new_ev),
                    "added": len(new_ev - old_ev),
                    "removed": len(old_ev - new_ev),
                }
                if not entry["changed"] and old_ev != new_ev:
                    entry["basis_changed"] = True  # 依据已变但结论未变
            else:
                entry["evidence_diff"] = None
            items.append(entry)
        for item, p in p_by_item.items():
            if item not in matched:
                items.append({"topic": p["topic"], "question_text": p["question_text"], "old": _claim_view(p),
                              "new": {"label": "新增项（本次未拆出）"},
                              "changed": True, "evidence_diff": None})
        rerun_meta = json.loads(run["rerun_meta_json"]) if run["rerun_meta_json"] else {}
        return {
            "parent_run_id": run["parent_run_id"],
            "run_id": run_id,
            "conditions_same": parent["conditions"] == child["conditions"],
            "parent_conditions": parent["conditions"],
            "child_conditions": child["conditions"],
            "model_diff": parent["model_version"] != child["model_version"]
            or parent["rules_version"] != child["rules_version"],
            "model_version": {"old": parent["model_version"],
                              "new": child["model_version"],
                              "rules_old": parent["rules_version"],
                              "rules_new": child["rules_version"]},
            "rerun_meta": rerun_meta,
            "items": items,
        }

    def _mini_snapshot(self, conn: sqlite3.Connection, run_id: str) -> dict:
        run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        claims = conn.execute(
            "SELECT * FROM claims WHERE run_id=? ORDER BY seq_no",
            (run_id,)).fetchall()
        out_claims = []
        for c in claims:
            evs = conn.execute(
                "SELECT evidence_id, relation, quote FROM claim_evidence WHERE claim_id=?",
                (c["id"],)).fetchall()
            out_claims.append({
                "topic": c["topic"], "status": c["status"], "seq_no": c["seq_no"],
                "question_text": c["question_text"],
                "conclusion_text": c["conclusion_text"],
                "conditions": json.loads(c["conditions_json"] or "{}"),
                "evidences": [dict(e) for e in evs],
            })
        return {"run_id": run_id, "question": run["question"],
                "conditions": json.loads(run["conditions_json"]),
                "model_version": run["model_version"],
                "rules_version": run["rules_version"],
                "claims": out_claims}


def _claim_view(c: Optional[dict]) -> dict:
    if c is None:
        return {"label": "无"}
    return {"status": c["status"], "conclusion_text": c.get("conclusion_text"),
            "evidence_count": len(c.get("evidences", []))}
