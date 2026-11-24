"""FastAPI 接口层（手册 §4 接口约定）。仅监听回环地址 + 本机令牌。"""
from __future__ import annotations

import asyncio
import json
import mimetypes
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from . import db as DB
from . import backup as backup_service
from .store import word_tree_path
from .config import Config, PROJECT_ROOT
from .imports_svc import ImportService, MAX_FILE_SIZE
from .judge import scope_example
from .records_svc import RecordService
from .runs_svc import RunService
from .supplements_svc import SupplementService

import_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="import")
run_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="run")


def create_app(cfg: Config, token: str) -> FastAPI:
    cfg.ensure_dirs()
    DB.init_db(cfg.db_path)
    _token = token

    app = FastAPI(title="TraceQA", version="0.1.0")
    imports = ImportService(cfg)
    runs = RunService(cfg, cfg.db_path)
    records = RecordService(cfg)
    supps = SupplementService()

    def conn() -> sqlite3.Connection:
        return DB.connect(cfg.db_path)

    def auth(x_traceqa_token: Optional[str] = Header(default=None,
                                                     alias="X-TraceQA-Token"),
             token_q: Optional[str] = Query(default=None, alias="token")):
        # 浏览器直链（打开原件、下载记录）无法携带自定义头，允许查询参数令牌
        if x_traceqa_token != token and token_q != token:
            raise HTTPException(status_code=401, detail="本机访问令牌无效")
        return True

    # 手册 §8：服务仅监听回环地址，校验本机访问令牌和请求来源
    @app.middleware("http")
    async def check_origin(request: Request, call_next):
        origin = request.headers.get("origin")
        if origin:
            from urllib.parse import urlparse
            try:
                host = urlparse(origin).hostname or ""
            except ValueError:
                host = ""
            if host not in ("127.0.0.1", "localhost", "::1"):
                return JSONResponse(status_code=403,
                                    content={"error": "请求来源不是本机"})
        return await call_next(request)

    # 启动恢复：中断任务标记可重试
    with DB.connect(cfg.db_path) as c:
        DB.recover_interrupted(c)
        imports.store.cleanup_orphan_tmps()

    # ------------------------------------------------------------ 基础

    @app.get("/api/health")
    def health():
        return {"ok": True, "version": app.version}

    @app.get("/api/bootstrap")
    def bootstrap(_=Depends(auth)):
        import os
        with conn() as c:
            doc_count = c.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"]
            model_dir_ok = cfg.artifacts_dir.exists()
        backend = os.environ.get("TRACEQA_PDF_BACKEND", "auto")
        try:
            import docling  # noqa: F401
            pdf_backend = "docling" if backend != "fitz" else "fitz（PyMuPDF）"
        except ImportError:
            pdf_backend = "fitz（PyMuPDF，CPU 离线）"
        return {"data_dir": str(cfg.data_dir), "doc_count": doc_count,
                "model_backend": cfg.model_backend,
                "pdf_backend": pdf_backend,
                "artifacts_dir_ok": model_dir_ok,
                "port": cfg.port}

    # ------------------------------------------------------------ 导入

    @app.post("/api/imports", status_code=202)
    async def create_import(files: list[UploadFile] = File(...),
                            folder_paths: str = Form(""), _=Depends(auth)):
        if not files:
            raise HTTPException(400, "未收到文件")
        folders = [""] * len(files)
        if folder_paths:
            try:
                folders = json.loads(folder_paths)
                if not isinstance(folders, list) or len(folders) != len(files):
                    raise ValueError("文件夹清单与上传文件数量不符")
                for value in folders:
                    if (not isinstance(value, str) or len(value) > 2048 or
                            (value and any(not part.strip() or part in {".", ".."} or
                                           any(ch in '\\:' or ord(ch) < 32 for ch in part)
                                           for part in value.split("/")))):
                        raise ValueError("文件夹路径无效")
            except ValueError as exc:
                raise HTTPException(400, "上传的文件夹路径清单无效") from exc
        payload: list[tuple[str, Path]] = []
        oversized: set[Path] = set()
        try:
            for f in files:
                path = cfg.tmp_dir / f"{DB.new_id('upload')}.part"
                name = (f.filename or "").replace("\\", "/").rsplit("/", 1)[-1]
                payload.append((name or "unnamed", path))
                size = 0
                with path.open("xb") as output:
                    while chunk := await f.read(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_FILE_SIZE:
                            oversized.add(path)
                            break
                        output.write(chunk)
                await f.close()
            with conn() as c:
                import_id = imports.create_import(c, payload, folders)
                file_rows = c.execute(
                    "SELECT id FROM import_files WHERE import_id=? ORDER BY rowid",
                    (import_id,)).fetchall()
                for (_, path), row in zip(payload, file_rows):
                    if path in oversized:
                        c.execute("UPDATE import_files SET status='failed', error=? WHERE id=?",
                                  ("文件过大（超过 200 MB）", row["id"]))
                c.commit()
        except BaseException:
            for _, path in payload:
                path.unlink(missing_ok=True)
            raise
        for (name, path), row in zip(payload, file_rows):
            if path in oversized:
                path.unlink(missing_ok=True)
            else:
                import_executor.submit(_import_worker, row["id"], name, path)
        return {"import_id": import_id}

    def _import_worker(file_id: str, name: str, data: Path):
        c = DB.connect(cfg.db_path)
        try:
            imports.process_file(c, file_id, name, data)
        finally:
            c.close()
            data.unlink(missing_ok=True)

    @app.get("/api/imports/{import_id}")
    def get_import(import_id: str, _=Depends(auth)):
        with conn() as c:
            out = imports.get_import(c, import_id)
        if out is None:
            raise HTTPException(404, "导入任务不存在")
        pending = [f for f in out["files"]
                   if f["status"] in ("pending", "storing", "parsing")]
        out["status"] = "running" if pending else "done"
        return out

    # ------------------------------------------------------------ 材料

    @app.get("/api/documents")
    def list_documents(_=Depends(auth)):
        with conn() as c:
            docs = c.execute("""
                SELECT d.id, d.title, d.created_at, d.folder_path FROM documents d
                ORDER BY d.created_at DESC""").fetchall()
            out = []
            for d in docs:
                # 一个版本可能有多条解析修订（重建索引），只展示每条版本
                # 最新的已发布修订，避免同一版本在列表里重复出现。
                vers = c.execute(
                    """SELECT v.*, r.id AS rev_id, r.status AS rev_status,
                              r.coverage_json
                       FROM versions v
                       LEFT JOIN parse_revisions r ON r.id = (
                           SELECT r2.id FROM parse_revisions r2
                           WHERE r2.version_id = v.id
                           ORDER BY (r2.status='ready') DESC,
                                    r2.published_at DESC, r2.created_at DESC, r2.rowid DESC
                           LIMIT 1)
                       WHERE v.document_id=? ORDER BY v.imported_at, v.rowid""",
                    (d["id"],)).fetchall()
                last_ver = vers[-1]["id"] if vers else None
                fields = c.execute(
                    "SELECT field, value, source FROM document_fields WHERE version_id=?",
                    (last_ver,)).fetchall() if last_ver else []
                mdnotes = c.execute(
                    "SELECT field, value, source, signature, created_at "
                    "FROM metadata_notes WHERE version_id=? ORDER BY created_at",
                    (last_ver,)).fetchall() if last_ver else []
                out.append({
                    "id": d["id"], "title": d["title"],
                    "created_at": d["created_at"],
                    "folder_path": d["folder_path"],
                    "document_fields": {f["field"]: dict(f) for f in fields},
                    "metadata_notes": [dict(n) for n in mdnotes],
                    "versions": [{
                        "version_id": v["id"], "file_name": v["file_name"],
                        "format": v["format"], "sha256_short": v["sha256"][:12],
                        "imported_at": v["imported_at"],
                        "revision_id": v["rev_id"],
                        "revision_status": v["rev_status"],
                        "is_latest": v["id"] == last_ver,
                        "coverage": json.loads(v["coverage_json"])
                        if v["coverage_json"] else None,
                    } for v in vers],
                })
        return {"documents": out}

    @app.get("/api/versions/{version_id}/page-geom")
    def page_geom(version_id: str, revision_id: Optional[str] = None,
                  _=Depends(auth)):
        """PDF 页面几何：旋转角、裁切框、印刷页码（手册 §5.1）。"""
        with conn() as c:
            if revision_id:
                row = c.execute(
                    "SELECT id FROM parse_revisions WHERE id=? AND version_id=?",
                    (revision_id, version_id)).fetchone()
                rid = revision_id if row else None
            else:
                row = c.execute(
                    "SELECT id FROM parse_revisions WHERE version_id=? AND status='ready' "
                    "ORDER BY published_at DESC, rowid DESC LIMIT 1", (version_id,)).fetchone()
                rid = row["id"] if row else None
            if rid is None:
                return {"pages": []}
            rows = c.execute(
                "SELECT * FROM page_geometry WHERE revision_id=? ORDER BY page_index",
                (rid,)).fetchall()
        return {"revision_id": rid,
                "pages": [dict(r) for r in rows]}

    @app.get("/api/versions/{version_id}/tree")
    def version_tree(version_id: str, revision_id: Optional[str] = None, _=Depends(auth)):
        with conn() as c:
            revision = c.execute(
                "SELECT id, parser_version FROM parse_revisions WHERE version_id=? "
                "AND status='ready' AND (? IS NULL OR id=?) "
                "ORDER BY published_at DESC, rowid DESC LIMIT 1",
                (version_id, revision_id, revision_id),
            ).fetchone()
        if revision is None:
            raise HTTPException(404, "该版本无对应的解析修订")
        p = word_tree_path(cfg.data_dir, version_id, revision["id"], revision["parser_version"])
        if not p.is_file():
            raise HTTPException(404, "该版本无结构树（PDF 请按页阅读原件）")
        return {**json.loads(p.read_text(encoding="utf-8")), "revision_id": revision["id"]}

    @app.get("/api/versions/{version_id}/original")
    def original_file(version_id: str, _=Depends(auth)):
        with conn() as c:
            v = c.execute("SELECT * FROM versions WHERE id=?",
                          (version_id,)).fetchone()
        if v is None:
            raise HTTPException(404, "版本不存在")
        p = imports.store.path_for(v["sha256"])
        if not p.exists():
            raise HTTPException(410, "原件缺失，请从备份恢复")
        media = ("application/pdf" if v["format"] == "pdf" else
                 "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        return FileResponse(p, media_type=media, filename=v["file_name"])

    @app.post("/api/versions/{version_id}/metadata-notes")
    def add_metadata_note(version_id: str, body: dict, _=Depends(auth)):
        field = (body.get("field") or "").strip()
        value = (body.get("value") or "").strip()
        source = (body.get("source") or "").strip()
        if not field or not value or not source:
            raise HTTPException(400, "字段、补录值、来源均为必填")
        with conn() as c:
            v = c.execute("SELECT id FROM versions WHERE id=?",
                          (version_id,)).fetchone()
            if v is None:
                raise HTTPException(404, "版本不存在")
            note_id = DB.new_id("mdn")
            c.execute(
                "INSERT INTO metadata_notes(id, version_id, field, value, source, "
                "signature, created_at) VALUES(?,?,?,?,?,?,?)",
                (note_id, version_id, field, value, source,
                 body.get("signature"), DB.now_iso()))
            c.commit()
        return {"id": note_id}

    # ------------------------------------------------------------ 核查

    @app.post("/api/runs", status_code=202)
    def submit_run(body: dict, _=Depends(auth)):
        request_id = (body.get("request_id") or "").strip()
        if not request_id:
            raise HTTPException(400, "缺少 request_id")
        with conn() as c:
            status, out = runs.submit(
                c, request_id, body.get("question", ""),
                body.get("revision_ids", []), body.get("conditions") or {})
            run_id = out.get("run_id")
        if status == 202 and run_id and not out.get("reused"):
            run_executor.submit(_run_worker, run_id)
        return JSONResponse(status_code=status, content=out)

    def _run_worker(run_id: str):
        runs.execute(run_id)

    @app.get("/api/runs/active")
    def active_run(_=Depends(auth)):
        """刷新后恢复进行中的任务（手册 §2 等待步骤）。"""
        with conn() as c:
            row = c.execute(
                "SELECT id, status, question, seq, created_at FROM runs "
                "WHERE status IN (?,?,?,?) OR status=? "
                "ORDER BY created_at DESC LIMIT 1",
                (DB.RUN_QUEUED, DB.RUN_RETRIEVING, DB.RUN_CHECKING,
                 DB.RUN_COMMITTING, DB.RUN_INTERRUPTED)).fetchone()
        if row is None:
            return {"run": None}
        return {"run": dict(row)}

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, _=Depends(auth)):
        with conn() as c:
            snap = runs.snapshot(c, run_id)
        if snap is None:
            raise HTTPException(404, "任务不存在")
        return snap

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str, request: Request,
                         last_seq: int = Query(default=0),
                         token: Optional[str] = Query(default=None)):
        # EventSource 无法携带自定义头，允许查询参数令牌
        if token != _token:
            raise HTTPException(401, "本机访问令牌无效")
        async def gen():
            seq = last_seq
            while True:
                if await request.is_disconnected():
                    return
                with conn() as c:
                    row = c.execute("SELECT status FROM runs WHERE id=?",
                                    (run_id,)).fetchone()
                    if row is None:
                        yield {"event": "error", "data": json.dumps({"error": "任务不存在"})}
                        return
                    events = runs.events_since(c, run_id, seq)
                for ev in events:
                    seq = ev["seq"]
                    yield {"event": "stage", "data": json.dumps(ev, ensure_ascii=False)}
                if row["status"] in DB.RUN_TERMINAL or row["status"] == DB.RUN_INTERRUPTED:
                    yield {"event": "done",
                           "data": json.dumps({"status": row["status"], "seq": seq})}
                    return
                await asyncio.sleep(0.3)
        return EventSourceResponse(gen())

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: str, _=Depends(auth)):
        with conn() as c:
            return runs.request_cancel(c, run_id)

    @app.get("/api/runs/{run_id}/export")
    def export_preview(run_id: str, _=Depends(auth)):
        with conn() as c:
            out = records.preview(c, run_id)
        if "error" in out:
            return JSONResponse(status_code=out.get("status", 409), content=out)
        return out

    # ------------------------------------------------------------ 证据与原文

    @app.get("/api/evidence/{evidence_id}")
    def get_evidence(evidence_id: str, _=Depends(auth)):
        with conn() as c:
            row = c.execute(
                """SELECT e.*, v.id AS version_id, v.file_name, v.format,
                          d.title AS doc_title
                   FROM evidence e
                   JOIN parse_revisions r ON r.id = e.revision_id
                   JOIN versions v ON v.id = r.version_id
                   JOIN documents d ON d.id = v.document_id
                   WHERE e.id=?""", (evidence_id,)).fetchone()
        if row is None:
            raise HTTPException(410, "该版本已清理，保留的摘录见历史记录")
        return {
            "id": row["id"], "text_orig": row["text_orig"],
            "heading_path": json.loads(row["heading_path_json"] or "[]"),
            "locator": json.loads(row["locator_json"]),
            "table_ctx": json.loads(row["table_ctx_json"]) if row["table_ctx_json"] else None,
            "version_id": row["version_id"], "file_name": row["file_name"],
            "format": row["format"], "doc_title": row["doc_title"],
        }

    # ------------------------------------------------------------ 人工说明

    @app.put("/api/claims/{claim_id}/note-draft")
    def save_note(claim_id: str, body: dict, _=Depends(auth)):
        expected = body.get("expected_draft_version")
        body_text = (body.get("body") or "").strip()
        source = (body.get("source") or "").strip()
        if not body_text or not source:
            raise HTTPException(400, "来源和正文必填")
        with conn() as c:
            c.execute("BEGIN IMMEDIATE")
            claim = c.execute("SELECT id, run_id FROM claims WHERE id=?",
                              (claim_id,)).fetchone()
            if claim is None:
                raise HTTPException(404, "子问题不存在")
            existing = c.execute(
                "SELECT * FROM notes WHERE claim_id=? ORDER BY recorded_at DESC LIMIT 1",
                (claim_id,)).fetchone()
            cur_ver = existing["draft_version"] if existing else 0
            if expected is not None and int(expected) != cur_ver:
                return JSONResponse(status_code=409, content={
                    "error": "另一窗口已保存该说明", "current_draft_version": cur_ver})
            if existing:
                c.execute("DELETE FROM notes WHERE id=?", (existing["id"],))
            note_id = DB.new_id("note")
            c.execute(
                "INSERT INTO notes(id, claim_id, body, source, contact_time, signature, "
                "recorded_at, draft_version, status) VALUES(?,?,?,?,?,?,?,?,'saved')",
                (note_id, claim_id, body_text, source, body.get("contact_time"),
                 body.get("signature"), DB.now_iso(), cur_ver + 1))
            c.commit()
        return {"id": note_id, "draft_version": cur_ver + 1,
                "recorded_at": DB.now_iso()}

    # ------------------------------------------------------------ 正式记录

    @app.post("/api/records")
    def issue_record(body: dict, _=Depends(auth)):
        run_id = body.get("run_id")
        request_id = body.get("request_id")
        if not run_id or not request_id:
            raise HTTPException(400, "缺少 run_id 或 request_id")
        with conn() as c:
            status, out = records.issue(c, run_id, request_id)
        return JSONResponse(status_code=status, content=out)

    @app.get("/api/records")
    def list_records(q: Optional[str] = None, _=Depends(auth)):
        with conn() as c:
            return {"records": records.list_records(c, q)}

    @app.get("/api/records/{record_id}")
    def get_record(record_id: str, _=Depends(auth)):
        with conn() as c:
            out = records.get(c, record_id)
        if out is None:
            raise HTTPException(404, "记录不存在")
        return out

    @app.post("/api/records/{record_id}/corrections")
    def correct_record(record_id: str, body: dict, _=Depends(auth)):
        with conn() as c:
            status, out = records.correct(
                c, record_id, body.get("base_rev_no", ""),
                body.get("reason", ""),
                body.get("note_updates") or body.get("noteUpdates") or [],
                body.get("request_id", ""))
        return JSONResponse(status_code=status, content=out)

    @app.get("/api/records/{record_id}/download")
    def download_record(record_id: str, _=Depends(auth)):
        with conn() as c:
            status, data, name = records.download(c, record_id)
        if status != 200:
            raise HTTPException(status, name or "下载失败")
        return Response(content=data, media_type="text/html; charset=utf-8",
                        headers={"Content-Disposition":
                                 f"attachment; filename={name}"})

    # ------------------------------------------------------------ 范围/补件/重查

    @app.get("/api/scope/last")
    def last_scope(_=Depends(auth)):
        """最近一次成功提交的材料版本组合 + 全部可选修订 + 可选新版（§2.4）。

        只浏览其他文档不覆盖默认值；新版本由用户显式选用。
        """
        with conn() as c:
            all_revs = c.execute(
                """SELECT r.id AS rev_id, r.status, v.id AS version_id,
                          v.file_name, v.format, v.imported_at, d.title,
                          d.id AS doc_id, d.folder_path, r.coverage_json, v.rowid AS version_order
                   FROM parse_revisions r JOIN versions v ON v.id=r.version_id
                   JOIN documents d ON d.id=v.document_id
                   WHERE r.status='ready' ORDER BY d.title, v.imported_at, v.rowid, r.rowid"""
            ).fetchall()
            by_rev = {r["rev_id"]: r for r in all_revs}
            latest_by_doc: dict[str, sqlite3.Row] = {}
            for r in all_revs:
                prev = latest_by_doc.get(r["doc_id"])
                if prev is None or r["imported_at"] >= prev["imported_at"]:
                    latest_by_doc[r["doc_id"]] = r
            catalog = [{
                "revision_id": r["rev_id"], "version_id": r["version_id"],
                "document_id": r["doc_id"], "file_name": r["file_name"],
                "title": r["title"], "format": r["format"],
                "folder_path": r["folder_path"],
                "imported_at": r["imported_at"],
                "is_latest": latest_by_doc[r["doc_id"]]["rev_id"] == r["rev_id"],
                "coverage": json.loads(r["coverage_json"]) if r["coverage_json"] else None,
            } for r in all_revs]

            row = c.execute("SELECT * FROM scope_last WHERE id=1").fetchone()
            if row is None:
                return {"revisions": [], "detail": [], "catalog": catalog,
                        "updates": []}
            revs = json.loads(row["revisions_json"])
            detail = []
            updates = []
            for rid in revs:
                r = by_rev.get(rid)
                detail.append({"revision_id": rid,
                               "available": bool(r and r["status"] == "ready"),
                               "file_name": r["file_name"] if r else None,
                               "folder_path": r["folder_path"] if r else "",
                               "title": r["title"] if r else None})
                if r:
                    newer = [x for x in all_revs
                             if x["doc_id"] == r["doc_id"]
                             and (x["imported_at"], x["version_order"]) >
                                 (r["imported_at"], r["version_order"])]
                    for n in newer:
                        updates.append({
                            "revision_id": rid, "file_name": r["file_name"],
                            "newer_revision_id": n["rev_id"],
                            "newer_file_name": n["file_name"],
                            "newer_imported_at": n["imported_at"],
                            "coverage_change": _coverage_delta(
                                r["coverage_json"], n["coverage_json"]),
                        })
        return {"revisions": revs, "detail": detail, "catalog": catalog,
                "updates": updates, "run_id": row["run_id"],
                "created_at": row["created_at"]}

    @app.post("/api/scope/example")
    def get_scope_example(body: dict, _=Depends(auth)):
        ids = body.get("revision_ids")
        if (not isinstance(ids, list) or len(ids) > 1000
                or any(not isinstance(rid, str) or len(rid) > 128 for rid in ids)):
            raise HTTPException(400, "核查范围无效")
        with conn() as c:
            return {"question": scope_example(c, list(dict.fromkeys(ids)))}

    @app.get("/api/supplements")
    def list_supplements(_=Depends(auth)):
        with conn() as c:
            rows = c.execute(
                """SELECT s.*, vo.file_name AS original_file,
                          vs.file_name AS supplement_file
                   FROM supplements s
                   JOIN parse_revisions ro ON ro.id=s.original_revision_id
                   JOIN versions vo ON vo.id=ro.version_id
                   JOIN parse_revisions rs ON rs.id=s.supplement_revision_id
                   JOIN versions vs ON vs.id=rs.version_id
                   ORDER BY s.created_at DESC""").fetchall()
            out = []
            for r in rows:
                affected = supps.affected_runs(c, r["id"], limit=1)
                out.append({
                    "id": r["id"],
                    "original_revision_id": r["original_revision_id"],
                    "supplement_revision_id": r["supplement_revision_id"],
                    "original_file": r["original_file"],
                    "supplement_file": r["supplement_file"],
                    "gap": json.loads(r["gap_json"]),
                    "status": r["status"], "created_at": r["created_at"],
                    "affected_total": affected.get("total", 0),
                })
        return {"supplements": out}

    @app.post("/api/supplements")
    def link_supplement(body: dict, _=Depends(auth)):
        with conn() as c:
            status, out = supps.link(
                c, body.get("original_revision_id", ""),
                body.get("gap") or {}, body.get("supplement_revision_id", ""))
        return JSONResponse(status_code=status, content=out)

    @app.get("/api/supplements/{supplement_id}/runs")
    def supplement_runs(supplement_id: str, limit: int = 50, offset: int = 0,
                        _=Depends(auth)):
        with conn() as c:
            return supps.affected_runs(c, supplement_id, limit, offset)

    @app.post("/api/runs/{run_id}/reruns")
    def rerun(run_id: str, body: dict, _=Depends(auth)):
        request_id = body.get("request_id")
        if not request_id:
            raise HTTPException(400, "缺少 request_id")
        with conn() as c:
            status, out = supps.rerun(c, run_id,
                                      body.get("supplement_revision_ids", []),
                                      request_id, runs)
            new_run_id = out.get("run_id")
        if status == 202 and new_run_id:
            run_executor.submit(_run_worker, new_run_id)
        return JSONResponse(status_code=status, content=out)

    @app.get("/api/runs/{run_id}/comparison")
    def comparison(run_id: str, _=Depends(auth)):
        with conn() as c:
            out = supps.comparison(c, run_id)
        if out is None:
            raise HTTPException(404, "该运行没有关联的重查")
        return out

    # ------------------------------------------------------------ 历史列表

    @app.get("/api/history")
    def history(q: Optional[str] = None, _=Depends(auth)):
        with conn() as c:
            rows = c.execute(
                """SELECT r.*, COALESCE((
                    SELECT json_group_array(file_name) FROM (
                        SELECT DISTINCT v.file_name
                        FROM json_each(r.scope_json) s JOIN parse_revisions p ON p.id=s.value
                        JOIN versions v ON v.id=p.version_id
                        ORDER BY v.file_name
                    )
                ), '[]') AS file_names
                FROM runs r
                WHERE ? = '' OR instr(r.question, ?) > 0 OR EXISTS (
                    SELECT 1 FROM json_each(r.scope_json) s
                    JOIN parse_revisions p ON p.id=s.value JOIN versions v ON v.id=p.version_id
                    WHERE instr(v.file_name, ?) > 0
                )
                ORDER BY r.created_at DESC, r.id DESC LIMIT 200""",
                (q or "", q or "", q or "")).fetchall()
            out = []
            for r in rows:
                file_names = json.loads(r["file_names"])
                out.append({
                    "run_id": r["id"], "question": r["question"],
                    "status": r["status"], "created_at": r["created_at"],
                    "finished_at": r["finished_at"],
                    "parent_run_id": r["parent_run_id"],
                    "files": file_names,
                })
        return {"runs": out}

    # ------------------------------------------------------------ 设置/自检/备份

    @app.get("/api/settings/status")
    def settings_status(_=Depends(auth)):
        import shutil, os
        total, used, free = shutil.disk_usage(cfg.data_dir)
        db_size = cfg.db_path.stat().st_size if cfg.db_path.exists() else 0
        backend = os.environ.get("TRACEQA_PDF_BACKEND", "auto")
        try:
            import docling  # noqa: F401
            pdf_backend = "docling" if backend != "fitz" else "fitz"
        except ImportError:
            pdf_backend = "fitz（CPU 离线回退）"
        with conn() as c:
            interrupted = c.execute(
                "SELECT COUNT(*) n FROM runs WHERE status=?",
                (DB.RUN_INTERRUPTED,)).fetchone()["n"]
            revs = c.execute(
                "SELECT COUNT(*) n FROM parse_revisions WHERE status='ready' "
                "AND parser_version NOT LIKE ?",
                (f"{__import__('traceqa_server.config', fromlist=['PARSER_VERSION']).PARSER_VERSION}%",)
            ).fetchone()["n"]
        from .config import DATA_DIR_FILE
        return {"data_dir": str(cfg.data_dir),
                "data_dir_config": str(DATA_DIR_FILE),
                "disk_free_gb": round(free / 2 ** 30, 1),
                "db_size_mb": round(db_size / 2 ** 20, 2),
                "parser_version": __import__(
                    "traceqa_server.config", fromlist=["PARSER_VERSION"]).PARSER_VERSION,
                "model_backend": cfg.model_backend,
                "pdf_backend": pdf_backend,
                "interrupted_runs": interrupted,
                "incompatible_revisions": revs}

    @app.post("/api/settings/data-dir")
    def set_data_dir(body: dict, _=Depends(auth)):
        from .config import DATA_DIR_FILE
        raw = (body.get("path") or "").strip()
        if not raw:
            raise HTTPException(400, "请填写数据目录")
        p = Path(raw).expanduser().resolve()
        p.mkdir(parents=True, exist_ok=True)
        if not _writable(p):
            raise HTTPException(400, "目录不可写")
        DATA_DIR_FILE.write_text(str(p), encoding="utf-8")
        return {"data_dir": str(p), "note": "重启服务后生效；原目录数据不会自动迁移"}

    @app.get("/api/settings/interrupted")
    def interrupted_runs(_=Depends(auth)):
        with conn() as c:
            rows = c.execute(
                "SELECT id, question, status, created_at, error FROM runs "
                "WHERE status=? ORDER BY created_at DESC",
                (DB.RUN_INTERRUPTED,)).fetchall()
        return {"runs": [dict(r) for r in rows]}

    @app.post("/api/settings/retry-interrupted")
    def retry_interrupted(body: dict, _=Depends(auth)):
        """重试沿用输入快照，保留上次错误记录（手册 §4.2）。"""
        run_id = body.get("run_id")
        with conn() as c:
            if run_id:
                rows = c.execute(
                    "SELECT id FROM runs WHERE id=? AND status=?",
                    (run_id, DB.RUN_INTERRUPTED)).fetchall()
            else:
                rows = c.execute(
                    "SELECT id FROM runs WHERE status=?",
                    (DB.RUN_INTERRUPTED,)).fetchall()
            ids = [r["id"] for r in rows]
            for rid in ids:
                c.execute(
                    "UPDATE runs SET status=?, cancel_requested=0, finished_at=NULL "
                    "WHERE id=?", (DB.RUN_QUEUED, rid))
                runs.requeue_event(c, rid)
            c.commit()
        for rid in ids:
            run_executor.submit(_run_worker, rid)
        return {"retried": ids}

    @app.post("/api/settings/rebuild-index")
    def rebuild_index(body: dict, _=Depends(auth)):
        """从原件重新解析并发布新修订；旧修订与历史引用保持不动。"""
        with conn() as c:
            vids = [body["version_id"]] if body.get("version_id") else [
                r["id"] for r in c.execute("SELECT id FROM versions").fetchall()]
            out, errors = [], []
            for vid in vids:
                try:
                    out.append(imports.rebuild_index(c, vid))
                except Exception as e:
                    errors.append({"version_id": vid, "error": str(e)})
        return {"rebuilt": out, "errors": errors}

    @app.post("/api/settings/selfcheck")
    def selfcheck(_=Depends(auth)):
        checks = []
        checks.append({"name": "数据库可读", "ok": cfg.db_path.exists()})
        with conn() as c:
            n_doc = c.execute("SELECT COUNT(*) c FROM documents").fetchone()["c"]
            n_ev = c.execute("SELECT COUNT(*) c FROM evidence").fetchone()["c"]
            bad = c.execute(
                "SELECT COUNT(*) c FROM versions v WHERE NOT EXISTS "
                "(SELECT 1 FROM parse_revisions r WHERE r.version_id=v.id)").fetchone()["c"]
            orphan_fts = c.execute(
                "SELECT COUNT(*) c FROM evidence_fts f LEFT JOIN evidence e "
                "ON e.rowid=f.rowid WHERE e.id IS NULL").fetchone()["c"]
            orphan_vec = c.execute(
                "SELECT COUNT(*) c FROM evidence_vec v LEFT JOIN evidence e "
                "ON e.rowid=v.evidence_rowid WHERE e.id IS NULL").fetchone()["c"]
            missing_originals = [dict(r) for r in c.execute(
                "SELECT id, sha256 FROM versions").fetchall()
                if not imports.store.path_for(r["sha256"]).is_file()][:5]
        checks.append({"name": f"文档 {n_doc} 份／证据 {n_ev} 条", "ok": True})
        checks.append({"name": "无主版本（缺修订）", "ok": bad == 0,
                       "detail": f"{bad} 个版本缺少修订"})
        checks.append({"name": "全文索引与证据一致", "ok": orphan_fts == 0,
                       "detail": f"孤立 FTS 行 {orphan_fts}"})
        checks.append({"name": "向量索引与证据一致", "ok": orphan_vec == 0,
                       "detail": f"孤立向量行 {orphan_vec}"})
        checks.append({"name": "原件文件齐全", "ok": not missing_originals,
                       "detail": f"{len(missing_originals)} 个原件缺失" if
                       missing_originals else "全部可读"})
        checks.append({"name": "原件目录可写", "ok": _writable(cfg.originals_dir)})
        return {"checks": checks}

    @app.post("/api/settings/backup")
    def backup(_=Depends(auth)):
        try:
            return {"backup_dir": str(backup_service.create_backup(cfg))}
        except (OSError, sqlite3.Error) as exc:
            raise HTTPException(507, f"无法创建备份：{exc}") from exc

    @app.get("/api/settings/backups")
    def list_backups(_=Depends(auth)):
        root = cfg.data_dir / "backups"
        out = []
        if root.exists():
            for d in sorted(root.iterdir(), reverse=True):
                if not d.is_dir() or not (d / "manifest.json").is_file():
                    continue
                manifest = {}
                mf = d / "manifest.json"
                if mf.exists():
                    try:
                        manifest = json.loads(mf.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        pass
                out.append({"name": d.name, "dir": str(d),
                            "created_at": manifest.get("created_at"),
                            "size_mb": round(
                                sum(f.stat().st_size for f in d.rglob("*")
                                    if f.is_file()) / 2 ** 20, 2)})
        return {"backups": sorted(out, key=lambda item: item["created_at"] or "", reverse=True)}

    @app.post("/api/settings/restore")
    def restore_backup(body: dict, _=Depends(auth)):
        """Validate now and restore during exclusive startup."""
        src = Path(body.get("backup_dir") or "")
        if not src.is_absolute():
            src = cfg.data_dir / "backups" / str(body.get("backup_dir") or "")
        try:
            backup_service.schedule_restore(cfg, src)
        except (OSError, ValueError, sqlite3.Error) as exc:
            raise HTTPException(409, f"备份未通过校验：{exc}") from exc
        return {"note": "恢复已安排。退出并重新启动 TraceQA 后应用，启动时会先保留回滚备份。"}

    # ------------------------------------------------------------ 前端静态资源

    dist = PROJECT_ROOT / "frontend" / "dist"
    if dist.exists():
        # The Windows MIME registry can map .mjs to text/plain, which disables module workers.
        mimetypes.add_type("text/javascript", ".mjs")
        app.mount("/assets", StaticFiles(directory=dist / "assets"), name="assets")

        @app.get("/{path:path}")
        def spa(path: str):
            if path.startswith("api/"):
                raise HTTPException(404)
            root = dist.resolve()
            # 路径穿越防护：请求的文件必须落在 dist 目录内，禁止读取
            # ../data/session.json 等敏感文件（该路由不带令牌鉴权）。
            try:
                f = (root / path).resolve()
                if not f.is_relative_to(root):
                    raise HTTPException(404)
            except (OSError, ValueError):
                raise HTTPException(404)
            if path and f.exists() and f.is_file():
                return FileResponse(f)
            return FileResponse(dist / "index.html")

    return app


def _writable(p: Path) -> bool:
    try:
        p.mkdir(parents=True, exist_ok=True)
        t = p / ".wtest"
        t.write_text("ok")
        t.unlink()
        return True
    except OSError:
        return False


def _coverage_delta(old_json: Optional[str], new_json: Optional[str]) -> str:
    """新版本覆盖变化摘要（§2.4「逐份列出版本和覆盖变化」）。"""
    try:
        old = json.loads(old_json) if old_json else {}
        new = json.loads(new_json) if new_json else {}
    except (ValueError, TypeError):
        return "覆盖报告不可读"
    parts = []
    for key, label in (("evidence_count", "可用证据"), ("covered_pages", "覆盖页")):
        a, b = old.get(key), new.get(key)
        if a is not None and b is not None and a != b:
            parts.append(f"{label} {a} → {b}")
    a = len(old.get("skipped") or [])
    b = len(new.get("skipped") or [])
    if a != b:
        parts.append(f"跳过页段 {a} → {b}")
    return "；".join(parts) if parts else "覆盖范围无明显变化"
