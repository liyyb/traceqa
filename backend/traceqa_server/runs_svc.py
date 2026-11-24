"""核查运行：状态机、事件序号、取消与完成竞争、原子提交（手册 §4、§4.1）。"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
from time import perf_counter
from typing import Optional

from . import db as DB, semantic
from .config import Config, MODEL_VERSION, RULES_VERSION
from .judge import (extract_conditions, judge_run, split_subquestions,
                    verify_claim)
from .ollama import select_passages
from .retrieval import (HashingEmbedder, VectorSearch, fts_match_query,
                        reciprocal_rank_fusion, search_fts)

EMBEDDER = HashingEmbedder()

# 每题补取/重写上限（手册 §5.2：防止 CPU 上反复调用模型）
MAX_REFETCH = 2

# §5.2：候选只说「原则上按标准执行」「详见附表」「除……外」时，
# 沿显式线索再补取一次相关条款，记录补取原因与命中位置。
REFETCH_RE = re.compile(r"(详见|见附表|标准见|除[^，。；]{0,24}外|原则上按|按附表|按附件)")
REFETCH_KEY_RE = re.compile(
    r"(附表\s*[一二三四五六七八九十0-9]*|附件\s*[0-9一二三四五六七八九十]*|"
    r"标准表|一览表|住宿标准)")


class RunService:
    def __init__(self, cfg: Config, db_path):
        self.cfg = cfg
        self.db_path = db_path
        self._cancel_flags: dict[str, threading.Event] = {}
        self._vector_search = VectorSearch()

    # ------------------------------------------------------------ 提交

    @staticmethod
    def input_digest(question: str, revision_ids: list[str], conditions: dict) -> str:
        payload = json.dumps({"q": question, "r": sorted(revision_ids),
                              "c": conditions or {}}, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def submit(self, conn: sqlite3.Connection, request_id: str, question: str,
               revision_ids: list[str], conditions: dict,
               parent_run_id: Optional[str] = None,
               rerun_meta: Optional[dict] = None) -> tuple[int, dict]:
        """返回 (http_status, body)。相同 request_id 幂等；内容不同返回 409。"""
        question = (question or "").strip()
        if not question:
            return 400, {"error": "问题不能为空"}
        if not revision_ids:
            return 400, {"error": "请至少选择一份材料"}
        # 校验修订可用
        marks = ",".join("?" * len(revision_ids))
        rows = conn.execute(
            f"SELECT id FROM parse_revisions WHERE id IN ({marks}) AND status='ready'",
            tuple(revision_ids)).fetchall()
        ready = {r["id"] for r in rows}
        missing = [r for r in revision_ids if r not in ready]
        if missing:
            return 400, {"error": "以下材料修订不可用（可能已移除或解析失败）",
                         "missing": missing}

        # Serialize the idempotency check with insertion, including across API threads.
        conn.execute("BEGIN IMMEDIATE")
        digest = self.input_digest(question, revision_ids, conditions)
        existing = conn.execute(
            "SELECT id, status FROM runs WHERE request_id=?", (request_id,)).fetchone()
        if existing is not None:
            old_digest_row = conn.execute(
                "SELECT question, scope_json, conditions_json FROM runs WHERE id=?",
                (existing["id"],)).fetchone()
            old_digest = self.input_digest(
                old_digest_row["question"],
                json.loads(old_digest_row["scope_json"]),
                json.loads(old_digest_row["conditions_json"]))
            if old_digest == digest:
                return 202, {"run_id": existing["id"], "status": existing["status"],
                             "reused": True}
            return 409, {"error": "相同请求 ID 已用于不同内容，请更换 request_id"}

        run_id = DB.new_id("run")
        conn.execute(
            "INSERT INTO runs(id, request_id, question, conditions_json, scope_json, "
            "model_version, rules_version, status, created_at, parent_run_id, "
            "rerun_meta_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, request_id, question,
             json.dumps(conditions or {}, ensure_ascii=False),
             json.dumps(revision_ids, ensure_ascii=False),
             MODEL_VERSION, RULES_VERSION, DB.RUN_QUEUED, DB.now_iso(),
             parent_run_id,
             json.dumps(rerun_meta, ensure_ascii=False) if rerun_meta else None))
        # 保存最近提交的范围（同一事务语义：与接受请求同时写入）
        conn.execute(
            "INSERT OR REPLACE INTO scope_last(id, revisions_json, run_id, created_at) "
            "VALUES(1,?,?,?)",
            (json.dumps(revision_ids, ensure_ascii=False), run_id, DB.now_iso()))
        self._emit(conn, run_id, DB.RUN_QUEUED, None, None)
        return 202, {"run_id": run_id, "status": DB.RUN_QUEUED}

    # ------------------------------------------------------------ 事件

    def _emit(self, conn: sqlite3.Connection, run_id: str, stage: str,
              done: Optional[int], total: Optional[int],
              payload: Optional[dict] = None) -> int:
        cur = conn.execute("SELECT seq FROM runs WHERE id=?", (run_id,))
        row = cur.fetchone()
        if row is None:
            return 0
        seq = row["seq"] + 1
        conn.execute("UPDATE runs SET seq=? WHERE id=?", (seq, run_id))
        conn.execute(
            "INSERT INTO run_events(run_id, seq, stage, done, total, payload_json, "
            "created_at) VALUES(?,?,?,?,?,?,?)",
            (run_id, seq, stage, done, total,
             json.dumps(payload, ensure_ascii=False) if payload else None,
             DB.now_iso()))
        conn.commit()
        return seq

    def requeue_event(self, conn: sqlite3.Connection, run_id: str) -> int:
        """中断任务重试：沿用输入快照，追加一条可重试事件（保留原错误记录）。"""
        return self._emit(conn, run_id, DB.RUN_QUEUED, None, None,
                          {"retry": True})

    def events_since(self, conn: sqlite3.Connection, run_id: str,
                     last_seq: int) -> list[dict]:
        rows = conn.execute(
            "SELECT seq, stage, done, total, payload_json FROM run_events "
            "WHERE run_id=? AND seq>? ORDER BY seq", (run_id, last_seq)).fetchall()
        out = []
        for r in rows:
            out.append({"seq": r["seq"], "stage": r["stage"], "done": r["done"],
                        "total": r["total"],
                        "payload": json.loads(r["payload_json"]) if r["payload_json"] else None})
        return out

    # ------------------------------------------------------------ 执行

    def request_cancel(self, conn: sqlite3.Connection, run_id: str) -> dict:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT status FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return {"error": "任务不存在"}
        if row["status"] not in DB.RUN_ACTIVE:
            return {"status": row["status"], "note": "任务已停止"}
        if row["status"] == DB.RUN_QUEUED:
            self._finish_cancelled(conn, run_id)
            return {"status": DB.RUN_CANCELLED}
        conn.execute("UPDATE runs SET cancel_requested=1 WHERE id=?", (run_id,))
        conn.commit()
        ev = self._cancel_flags.get(run_id)
        if ev:
            ev.set()
        return {"status": "cancelling"}

    def execute(self, run_id: str) -> None:
        """工作线程入口：检索 → 核对 → 原子提交。"""
        conn = DB.connect(self.db_path)
        cancel_ev = threading.Event()
        self._cancel_flags[run_id] = cancel_ev
        started = perf_counter()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                return
            if row["status"] != DB.RUN_QUEUED:
                return
            scope = json.loads(row["scope_json"])
            question = row["question"]
            conditions = json.loads(row["conditions_json"])

            conn.execute("UPDATE runs SET status=?, started_at=? WHERE id=?",
                         (DB.RUN_RETRIEVING, DB.now_iso(), run_id))
            conn.commit()
            subqs = split_subquestions(question)
            self._emit(conn, run_id, DB.RUN_RETRIEVING, 0, len(subqs),
                       {"scope_size": len(scope)})
            candidates: dict[str, list] = {}
            semantic_key = None
            try:
                semantic_key = semantic.model_key(self.cfg)
            except semantic.UNAVAILABLE:
                pass
            for i, sq in enumerate(subqs):
                if cancel_ev.is_set() or self._cancel_in_db(conn, run_id):
                    self._finish_cancelled(conn, run_id)
                    return
                cands, refetch_reasons = self._retrieve(conn, sq.text, scope, semantic_key)
                candidates[str(i)] = cands
                self._emit(conn, run_id, DB.RUN_RETRIEVING, i + 1, len(subqs),
                           {"refetch": refetch_reasons} if refetch_reasons else None)

            # 核对阶段
            conn.execute("UPDATE runs SET status=? WHERE id=?",
                         (DB.RUN_CHECKING, run_id))
            conn.commit()
            checking_started = perf_counter()
            self._emit(conn, run_id, DB.RUN_CHECKING, None, None, {
                "retrieval_ms": round((checking_started - started) * 1000),
            })
            claims, conds = judge_run(conn, question, conditions, scope, candidates)

            verified_claims = []
            scope_set = set(scope)
            model_attempted = False
            model_version = MODEL_VERSION
            for ci, claim in enumerate(claims):
                if cancel_ev.is_set() or self._cancel_in_db(conn, run_id):
                    self._finish_cancelled(conn, run_id)
                    return
                rows = candidates.get(str(claim["seq_no"]), [])
                if (self.cfg.ollama_model and not model_attempted and rows
                        and claim["status"] == DB.CLAIM_NO_EVIDENCE):
                    model_attempted = True
                    selected = select_passages(self.cfg.ollama_model, claim["question_text"], rows)
                    if cancel_ev.is_set() or self._cancel_in_db(conn, run_id):
                        self._finish_cancelled(conn, run_id)
                        return
                    if selected is not None:
                        model_version = f"{MODEL_VERSION}+ollama:{self.cfg.ollama_model}"
                        claim = {**claim, "groups": [{
                            "relation": "related",
                            "items": [{"evidence_id": r["id"], "quote": r["text_orig"]}
                                      for r in selected],
                        }]}
                ok, reason = verify_claim(claim, conn, scope_set)
                if not ok:
                    # 校验失败：降级为依据不足并保留原因（故障可定位到阶段）
                    claim = {**claim, "status": DB.CLAIM_NO_EVIDENCE,
                             "conclusion_text": f"自动校验未通过（{reason}），已转为待人工核对。",
                             "groups": [], "amount": None, "comparator": None,
                             "denominator": None}
                verified_claims.append(claim)
                self._emit(conn, run_id, DB.RUN_CHECKING, ci + 1, len(claims))

            # 提交阶段：事务原子提交全部结论、证据关系及完成状态
            conn.execute("UPDATE runs SET status=? WHERE id=?",
                         (DB.RUN_COMMITTING, run_id))
            conn.commit()
            committing_started = perf_counter()
            self._emit(conn, run_id, DB.RUN_COMMITTING, None, None, {
                "checking_ms": round((committing_started - checking_started) * 1000),
                "model_attempted": model_attempted,
            })

            conn.execute("BEGIN IMMEDIATE")
            try:
                if self._cancel_in_db(conn, run_id):
                    self._finish_cancelled(conn, run_id)
                    return
                cur = conn.execute(
                    "UPDATE runs SET status=?, finished_at=?, model_version=?, rules_version=? "
                    "WHERE id=? AND status IN (?,?,?,?)",
                    (DB.RUN_COMPLETED, DB.now_iso(), model_version, RULES_VERSION, run_id,
                     DB.RUN_QUEUED, DB.RUN_RETRIEVING, DB.RUN_CHECKING,
                     DB.RUN_COMMITTING))
                if cur.rowcount == 0:
                    conn.rollback()
                    return
                for claim in verified_claims:
                    claim_id = DB.new_id("clm")
                    conn.execute(
                        "INSERT INTO claims(id, run_id, seq_no, topic, question_text, "
                        "status, conclusion_text, conditions_json, followup_json, gap_json, "
                        "amount_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (claim_id, run_id, claim["seq_no"], claim["topic"],
                         claim["question_text"], claim["status"],
                         claim.get("conclusion_text"),
                         json.dumps(claim.get("conditions") or {}, ensure_ascii=False),
                         json.dumps(claim.get("followup"), ensure_ascii=False)
                         if claim.get("followup") else None,
                         json.dumps(claim.get("gap"), ensure_ascii=False)
                         if claim.get("gap") else None,
                         json.dumps({k: claim.get(k) for k in
                                     ("amount", "comparator", "denominator")
                                     if claim.get(k) is not None}, ensure_ascii=False)
                         if claim.get("amount") is not None else None))
                    for gi, g in enumerate(claim.get("groups", [])):
                        for item in g.get("items", []):
                            conn.execute(
                                "INSERT INTO claim_evidence(id, claim_id, evidence_id, "
                                "relation, group_no, quote) VALUES(?,?,?,?,?,?)",
                                (DB.new_id("ce"), claim_id, item["evidence_id"],
                                 g["relation"], gi, item.get("quote")))
                self._emit(conn, run_id, DB.RUN_COMPLETED, None, None,
                           {"claims": len(verified_claims),
                            "commit_ms": round((perf_counter() - committing_started) * 1000),
                            "worker_ms": round((perf_counter() - started) * 1000)})
            except Exception:
                conn.rollback()
                raise
        except Exception as e:
            try:
                conn.execute(
                    "UPDATE runs SET status=?, error=?, finished_at=? WHERE id=? "
                    "AND status IN (?,?,?,?)",
                    (DB.RUN_FAILED, str(e), DB.now_iso(), run_id,
                     DB.RUN_QUEUED, DB.RUN_RETRIEVING, DB.RUN_CHECKING,
                     DB.RUN_COMMITTING))
                if conn.execute("SELECT changes()").fetchone()[0]:
                    self._emit(conn, run_id, DB.RUN_FAILED, None, None,
                               {"error": str(e)})
            except sqlite3.Error:
                pass
        finally:
            self._cancel_flags.pop(run_id, None)
            conn.close()

    def _cancel_in_db(self, conn: sqlite3.Connection, run_id: str) -> bool:
        row = conn.execute("SELECT cancel_requested FROM runs WHERE id=?",
                           (run_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    def _finish_cancelled(self, conn: sqlite3.Connection, run_id: str):
        conn.execute(
            "UPDATE runs SET status=?, finished_at=? WHERE id=? AND status IN (?,?,?,?)",
            (DB.RUN_CANCELLED, DB.now_iso(), run_id,
             DB.RUN_QUEUED, DB.RUN_RETRIEVING, DB.RUN_CHECKING, DB.RUN_COMMITTING))
        self._emit(conn, run_id, DB.RUN_CANCELLED, None, None)

    # ------------------------------------------------------------ 检索

    def _retrieve(self, conn: sqlite3.Connection, question: str,
                  scope: list[str], semantic_key: str | None = None) -> tuple[list, list[str]]:
        """检索 + 相邻块补取 + 按显式线索二次补取。返回 (候选行, 补取原因)。"""
        # 口语问句（“入住/离店/这两晚”）正文里没有“住宿”二字，检索锁不到
        # 金额条款。用规则层识别出的主题词扩展查询，仍保留原问句词项。
        subqs = split_subquestions(question)
        extra_terms = list(dict.fromkeys(
            k for sq in subqs for k in sq.keywords
            if isinstance(k, str) and len(k) >= 2))
        match = fts_match_query(question, extra_terms)
        fts_hits = search_fts(conn, scope, match, limit=40)
        qvec = EMBEDDER.embed(question + " " + " ".join(extra_terms))
        vec_hits = self._vector_search.search(conn, scope, qvec, limit=40)
        fused = reciprocal_rank_fusion([fts_hits, vec_hits])[:30]
        if semantic_key:
            try:
                semantic_hits = semantic.search(conn, question, scope, semantic_key)
            except semantic.UNAVAILABLE:
                semantic_hits = []
            if semantic_hits:
                # Keep rule-mode candidates so semantic ranking cannot hide conflicting versions.
                fused = list(dict.fromkeys([*fused, *(eid for eid, _ in semantic_hits[:12])]))
        if not fused:
            return [], []
        marks = ",".join("?" * len(fused))
        rows = conn.execute(
            f"SELECT * FROM evidence WHERE id IN ({marks})", tuple(fused)).fetchall()
        by_id = {r["id"]: r for r in rows}
        ordered = [by_id[i] for i in fused if i in by_id]
        if not ordered:
            return [], []
        # 补取相邻条款：同修订内命中块的前后块（手册 §5 第 5 条）
        extra: list = []
        seen = set(fused)
        neighbors = [(r["revision_id"], r["chunk_no"] + offset)
                     for r in ordered for offset in (-1, 1)]
        wanted = ",".join(["(?,?,?)"] * len(neighbors))
        params = [value for i, (rid, chunk) in enumerate(neighbors)
                  for value in (rid, chunk, i)]
        for row in conn.execute(
                f"""WITH wanted(revision_id, chunk_no, position) AS (VALUES {wanted})
                    SELECT e.* FROM wanted w JOIN evidence e
                    ON e.revision_id=w.revision_id AND e.chunk_no=w.chunk_no
                    ORDER BY w.position""", params):
            if row["id"] not in seen:
                seen.add(row["id"])
                extra.append(row)

        # 表格是一个整体证据：金额常在孤立单元格（正文只有“600元”），
        # 城市/职级写在表头。只召回命中行会把条件与金额拆散，导致该金额丢失。
        qconds = extract_conditions(question, {})
        q_city = qconds.get("city")
        q_class = qconds.get("employee_class")

        def _table_row_matches(row) -> bool:
            """表格单元格只在用户给的城市场景/职级能对上表头时才补取；
            否则整张表的各行金额都会被当成候选，制造出并不存在的冲突。"""
            try:
                ctx = json.loads(row["table_ctx_json"] or "{}") or {}
            except (ValueError, TypeError):
                return False
            heads = " ".join(list(ctx.get("row_headers") or []) +
                             list(ctx.get("col_headers") or []))
            if q_city and q_city not in heads:
                return False
            if q_class and q_class not in heads:
                return False
            return bool(q_city or q_class)

        table_ctxs: dict[str, set[str]] = {}
        for r in ordered + extra:
            if not r["table_ctx_json"]:
                continue
            try:
                tname = (json.loads(r["table_ctx_json"]) or {}).get("table_name") or ""
            except (ValueError, TypeError):
                continue
            if tname:
                table_ctxs.setdefault(r["revision_id"], set()).add(tname)
        if table_ctxs:
            for rev_id, names in table_ctxs.items():
                table_rows = conn.execute(
                    "SELECT * FROM evidence WHERE revision_id=? AND "
                    "table_ctx_json IS NOT NULL AND table_ctx_json<>''",
                    (rev_id,)).fetchall()
                for n in table_rows:
                    try:
                        nname = (json.loads(n["table_ctx_json"]) or {}).get("table_name") or ""
                    except (ValueError, TypeError):
                        continue
                    if nname in names and _table_row_matches(n) and n["id"] not in seen:
                        seen.add(n["id"])
                        extra.append(n)

        # 兜底补取：用户显式勾进范围的每份材料，至少纳入若干条含金额的
        # 主题条款。长文档里真正的标准行常被 BM25 排名挤出，导致“勾了却
        # 读不到”（相对年份/跨天区间漏掉 500 元即由此产生）。
        money_keywords = [k for k in extra_terms if len(k) >= 2]
        for rev_id in scope:
            if not money_keywords:
                break
            money_rows = conn.execute(
                "SELECT * FROM evidence WHERE revision_id=? AND "
                "text_norm LIKE '%元%' LIMIT 120", (rev_id,)).fetchall()
            added = 0
            for r in money_rows:
                hay = (r["text_orig"] or "") + " " + (r["table_ctx_json"] or "")
                if not any(k in hay for k in money_keywords):
                    continue
                if r["table_ctx_json"] and not _table_row_matches(r):
                    continue
                if r["id"] not in seen:
                    seen.add(r["id"])
                    extra.append(r)
                    added += 1
                if added >= 6:
                    break

        # §5.2：显式线索二次补取，限制次数并记录原因与命中位置
        reasons: list[str] = []
        for r in list(ordered)[:MAX_REFETCH]:
            text = r["text_orig"] or ""
            if not REFETCH_RE.search(text):
                continue
            keys = REFETCH_KEY_RE.findall(text)
            for kw in keys[:2]:
                needle = re.sub(r"\s+", "", kw)
                if not needle:
                    continue
                found = conn.execute(
                    "SELECT * FROM evidence WHERE revision_id=? AND "
                    "replace(text_norm,' ','') LIKE ? LIMIT 4",
                    (r["revision_id"], f"%{needle}%")).fetchall()
                added = 0
                for n in found:
                    if n["id"] not in seen:
                        seen.add(n["id"])
                        extra.append(n)
                        added += 1
                if added:
                    reasons.append(f"「{text[:24]}」显式指向 {kw}，补取 {added} 条")
        return ordered + extra, reasons

    # ------------------------------------------------------------ 快照

    def snapshot(self, conn: sqlite3.Connection, run_id: str) -> Optional[dict]:
        row = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if row is None:
            return None
        claims = conn.execute(
            "SELECT * FROM claims WHERE run_id=? ORDER BY seq_no",
            (run_id,)).fetchall()
        claim_out = []
        for c in claims:
            ces = conn.execute(
                """SELECT ce.*, e.locator_json, e.heading_path_json, e.page_no,
                          e.text_orig, e.revision_id, r.version_id, v.file_name, d.title AS doc_title,
                          v.format
                   FROM claim_evidence ce
                   JOIN evidence e ON e.id = ce.evidence_id
                   JOIN parse_revisions r ON r.id = e.revision_id
                   JOIN versions v ON v.id = r.version_id
                   JOIN documents d ON d.id = v.document_id
                   WHERE ce.claim_id=? ORDER BY ce.group_no, ce.rowid""",
                (c["id"],)).fetchall()
            notes = conn.execute(
                "SELECT * FROM notes WHERE claim_id=? AND status='saved' "
                "ORDER BY recorded_at DESC", (c["id"],)).fetchall()
            amount_data = json.loads(c["amount_json"]) if c["amount_json"] else {}
            claim_out.append({
                "id": c["id"], "seq_no": c["seq_no"], "topic": c["topic"],
                "question_text": c["question_text"], "status": c["status"],
                "conclusion_text": c["conclusion_text"],
                "conditions": json.loads(c["conditions_json"] or "{}"),
                "followup": json.loads(c["followup_json"]) if c["followup_json"] else None,
                "gap": json.loads(c["gap_json"]) if c["gap_json"] else None,
                **amount_data,
                "evidences": [{
                    "ce_id": ce["ce_id"] if "ce_id" in ce.keys() else ce["id"],
                    "id": ce["evidence_id"], "relation": ce["relation"],
                    "group_no": ce["group_no"], "quote": ce["quote"],
                    "locator": json.loads(ce["locator_json"]),
                    "heading_path": json.loads(ce["heading_path_json"] or "[]"),
                    "page_no": ce["page_no"], "text_orig": ce["text_orig"],
                    "version_id": ce["version_id"], "revision_id": ce["revision_id"],
                    "file_name": ce["file_name"],
                    "doc_title": ce["doc_title"], "format": ce["format"],
                } for ce in ces],
                "notes": [dict(n) for n in notes],
            })
        scope = json.loads(row["scope_json"])
        scope_detail = []
        for rid in scope:
            r = conn.execute(
                """SELECT r.id AS rev_id, v.id AS version_id, v.file_name, d.title,
                          r.coverage_json, v.format
                   FROM parse_revisions r JOIN versions v ON v.id=r.version_id
                   JOIN documents d ON d.id=v.document_id WHERE r.id=?""",
                (rid,)).fetchone()
            if r:
                scope_detail.append({
                    "revision_id": r["rev_id"], "version_id": r["version_id"],
                    "file_name": r["file_name"], "title": r["title"],
                    "format": r["format"],
                    "coverage": json.loads(r["coverage_json"]) if r["coverage_json"] else None,
                })
        return {
            "run_id": row["id"], "status": row["status"],
            "question": row["question"],
            "conditions": json.loads(row["conditions_json"]),
            "scope": scope, "scope_detail": scope_detail,
            "model_version": row["model_version"],
            "rules_version": row["rules_version"],
            "seq": row["seq"], "error": row["error"],
            "parent_run_id": row["parent_run_id"],
            "created_at": row["created_at"], "finished_at": row["finished_at"],
            "claims": claim_out,
            "stage_label": DB.STAGE_LABELS.get(row["status"], row["status"]),
        }
