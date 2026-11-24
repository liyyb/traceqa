"""导入管线：逐文件去重、解析、覆盖检查、建索引，原子发布修订（手册 §5 第 1 条）。"""
from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
import sys
import zipfile
from pathlib import Path
from typing import Optional

from . import db as DB
from .config import Config, PARSER_VERSION, INDEX_VERSION
from .retrieval import HashingEmbedder, fts_tokens_text, vec_to_blob
from .store import OriginalStore

MAX_FILE_SIZE = 200 * 1024 * 1024
MAX_DOCX_UNCOMPRESSED = 500 * 1024 * 1024

# 同一份制度的改版文件名归一化：去掉「（修订）」「-扫描」「-清晰补件」等版本标记，
# 使新版本挂在同一个 Document 下（手册 §2.4「2 份有新版」、§6 版本回归）。
_VERSION_MARKERS = re.compile(
    r"(?:[（(](?:修订|修订版|新版|附件引用版|无编号|副本|v\d+|\d+)[)）]"
    r"|[-—_\s]*(?:扫描件?|清晰补件|修订版?|附件引用版|副本)$)",
    re.I)

# 原件元数据提取（手册 §2.3）：只认文档里明确印出的字段与日期。
_FIELD_PATTERNS = {
    "制度编号": re.compile(r"(?:制度编号|文号|办法编号|文件编号|编号)\s*[:：]\s*([^\s，。；、]+)"),
    "发布日期": re.compile(r"(?:发布日期|印发日期|发文日期)\s*[:：]\s*"
                           r"(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日?)"),
    "生效日期": re.compile(r"自\s*(\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日?)\s*起"
                           r"(?:施行|执行|实施|生效)"),
}


def doc_key_of(file_name: str) -> str:
    stem = re.sub(r"\.(docx|pdf)$", "", file_name, flags=re.I)
    prev = None
    while prev != stem:
        prev = stem
        stem = _VERSION_MARKERS.sub("", stem).strip(" -—_")
    return stem or re.sub(r"\.(docx|pdf)$", "", file_name, flags=re.I)

_PARSERS_PATH = Path(__file__).resolve().parent.parent.parent / "parsers"
if str(_PARSERS_PATH) not in sys.path:
    sys.path.insert(0, str(_PARSERS_PATH))

from traceqa_parsers.docx_parse import parse_docx, DocxParseError  # noqa: E402
from traceqa_parsers.pdf_parse import parse_pdf, PdfParseError  # noqa: E402

EMBEDDER = HashingEmbedder()


def detect_format(data: bytes | Path, file_name: str) -> Optional[str]:
    if isinstance(data, Path):
        with data.open("rb") as stream:
            magic = stream.read(4)
    else:
        magic = data[:4]
    if magic == b"%PDF":
        return "pdf"
    if magic == b"PK\x03\x04":
        if file_name.lower().endswith(".docx"):
            return "docx"
        try:
            with zipfile.ZipFile(data if isinstance(data, Path) else io.BytesIO(data)) as zf:
                if "word/document.xml" in zf.namelist():
                    return "docx"
        except Exception:
            pass
    if file_name.lower().endswith(".doc"):
        return "doc_legacy"
    return None


class ImportService:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = OriginalStore(cfg)

    # ------------------------------------------------------------ 任务入口

    def create_import(self, conn: sqlite3.Connection,
                      files: list[tuple[str, bytes | Path]],
                      folder_paths: list[str] | None = None) -> str:
        import_id = DB.new_id("imp")
        conn.execute("INSERT INTO imports(id, status, created_at) VALUES(?,?,?)",
                     (import_id, "running", DB.now_iso()))
        for i, (name, _data) in enumerate(files):
            conn.execute(
                "INSERT INTO import_files(id, import_id, file_name, folder_path, status) VALUES(?,?,?,?,?)",
                (DB.new_id("imf"), import_id, name, folder_paths[i] if folder_paths else "", "pending"))
        conn.commit()
        return import_id

    def get_import(self, conn: sqlite3.Connection, import_id: str) -> Optional[dict]:
        row = conn.execute("SELECT * FROM imports WHERE id=?", (import_id,)).fetchone()
        if row is None:
            return None
        files = conn.execute(
            "SELECT * FROM import_files WHERE import_id=? ORDER BY rowid",
            (import_id,)).fetchall()
        return {
            "id": import_id, "status": row["status"],
            "files": [self._file_view(conn, f) for f in files],
        }

    def _file_view(self, conn: sqlite3.Connection, f: sqlite3.Row) -> dict:
        out = {
            "id": f["id"], "file_name": f["file_name"], "status": f["status"],
            "folder_path": f["folder_path"],
            "version_id": f["version_id"], "revision_id": f["revision_id"],
            "error": f["error"],
        }
        if f["coverage_json"]:
            out["coverage"] = json.loads(f["coverage_json"])
        if f["version_id"]:
            v = conn.execute("SELECT * FROM versions WHERE id=?",
                             (f["version_id"],)).fetchone()
            if v:
                out["doc_title"] = conn.execute(
                    "SELECT title FROM documents WHERE id=?",
                    (v["document_id"],)).fetchone()["title"]
        return out

    # ------------------------------------------------------------ 单文件管线

    def process_file(self, conn: sqlite3.Connection, import_file_id: str,
                     file_name: str, data: bytes | Path) -> None:
        def update(status: str, **kw):
            sets = ["status=?"]
            vals: list = [status]
            for k, v in kw.items():
                sets.append(f"{k}=?")
                vals.append(v)
            vals.append(import_file_id)
            conn.execute(f"UPDATE import_files SET {', '.join(sets)} WHERE id=?", vals)
            conn.commit()

        try:
            fmt = detect_format(data, file_name)
            if fmt == "doc_legacy":
                update("failed", error="旧式 .doc 格式：请另存为 .docx 后重新导入")
                return
            if fmt is None:
                update("failed", error="无法识别的文件类型（支持 .docx 与带文本层的 PDF）")
                return
            size = data.stat().st_size if isinstance(data, Path) else len(data)
            if size > MAX_FILE_SIZE:
                update("failed", error="文件过大（超过 200 MB）")
                return
            if fmt == "docx":
                try:
                    with zipfile.ZipFile(data if isinstance(data, Path) else io.BytesIO(data)) as zf:
                        total = sum(i.file_size for i in zf.infolist())
                    if total > MAX_DOCX_UNCOMPRESSED:
                        update("failed", error="DOCX 解包后过大，拒绝导入")
                        return
                except zipfile.BadZipFile:
                    update("failed", error="DOCX 包损坏")
                    return

            update("storing")
            if isinstance(data, Path):
                with data.open("rb") as stream:
                    sha = hashlib.file_digest(stream, "sha256").hexdigest()
                tmp = data
            else:
                sha, tmp = self.store.stage(data)

            # 逐文件去重：相同内容复用已有版本与修订
            folder = conn.execute("SELECT folder_path FROM import_files WHERE id=?",
                                  (import_file_id,)).fetchone()["folder_path"]
            dup = conn.execute(
                """SELECT v.* FROM versions v JOIN documents d ON d.id=v.document_id
                   WHERE v.sha256=? AND d.folder_path=? AND (?='' OR d.doc_key=?)""",
                (sha, folder, folder, doc_key_of(file_name))).fetchone()
            final_path = self.store.publish(sha, tmp)
            if dup is not None:
                rev = conn.execute(
                    "SELECT * FROM parse_revisions WHERE version_id=? AND status='ready' "
                    "ORDER BY published_at DESC, rowid DESC LIMIT 1", (dup["id"],)).fetchone()
                if rev is not None:
                    update("done", version_id=dup["id"], revision_id=rev["id"],
                           coverage_json=rev["coverage_json"])
                    return
                ver_id = dup["id"]
                is_new_version = conn.execute(
                    "SELECT 1 FROM versions WHERE document_id=? AND status='published' LIMIT 1",
                    (dup["document_id"],),
                ).fetchone() is not None
            else:
                ver_id, is_new_version = self._create_version(
                    conn, file_name, fmt, sha, final_path, size, folder)

            update("parsing", version_id=ver_id)
            result = self._parse(fmt, str(final_path))

            # 全部准备好再发布修订（一个事务）
            rev_id = DB.new_id("rev")
            evidences = result["evidences"]
            coverage = result["coverage"]
            backend = result.get("backend", "unknown")
            parser_version = f"{PARSER_VERSION}+{backend}"
            document_fields, cover_status = self._extract_document_fields(
                evidences, fmt, coverage.to_dict())
            coverage_dict = coverage.to_dict()
            coverage_dict["document_fields"] = document_fields
            coverage_dict["cover_status"] = cover_status
            coverage_dict["is_new_version"] = is_new_version
            coverage_json = json.dumps(coverage_dict, ensure_ascii=False)
            self._save_tree(rev_id, result)
            self._publish_revision(conn, ver_id, rev_id, parser_version,
                                   coverage_json, evidences, result)
            update("done", revision_id=rev_id, coverage_json=coverage_json)

        except (DocxParseError, PdfParseError) as e:
            update("failed", error=str(e))
        except Exception as e:  # 明确提示损坏/不可读
            update("failed", error=f"导入失败：{e}")

    def _create_version(self, conn: sqlite3.Connection, file_name: str, fmt: str,
                        sha: str, final_path: Path, size: int, folder: str) -> tuple[str, bool]:
        key = doc_key_of(file_name)
        document = conn.execute("SELECT id FROM documents WHERE folder_path=? AND doc_key=?",
                                (folder, key)).fetchone()
        doc_id = document["id"] if document else DB.new_id("doc")
        if document is None:
            title = re.sub(r"\.(docx|pdf)$", "", file_name, flags=re.I)
            conn.execute("INSERT INTO documents(id, title, doc_key, created_at, folder_path) VALUES(?,?,?,?,?)",
                         (doc_id, title, key, DB.now_iso(), folder))
        ver_id = DB.new_id("ver")
        conn.execute(
            "INSERT INTO versions(id, document_id, sha256, file_name, format, "
            "size_bytes, stored_path, imported_at, status) VALUES(?,?,?,?,?,?,?,?,?)",
            (ver_id, doc_id, sha, file_name, fmt, size, str(final_path), DB.now_iso(), "stored"))
        conn.commit()
        return ver_id, document is not None

    # ------------------------------------------------------------ 发布事务

    def _publish_revision(self, conn: sqlite3.Connection, ver_id: str, rev_id: str,
                          parser_version: str, coverage_json: str,
                          evidences: list, result: dict) -> None:
        """解析结果、索引、页面几何、原件元数据在一个事务里发布。"""
        fields, cover_status = json.loads(coverage_json).get("document_fields"), \
            json.loads(coverage_json).get("cover_status")
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO parse_revisions(id, version_id, parser_version, "
                "index_version, status, coverage_json, created_at, published_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (rev_id, ver_id, parser_version, INDEX_VERSION, "ready",
                 coverage_json, DB.now_iso(), DB.now_iso()))
            for ev in evidences:
                cur = conn.execute(
                    "INSERT INTO evidence(id, revision_id, chunk_no, text_orig, "
                    "text_norm, heading_path_json, locator_json, table_ctx_json, "
                    "page_no, meta_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (DB.new_id("ev"), rev_id, ev.chunk_no, ev.text_orig,
                     ev.text_norm, json.dumps(ev.heading_path, ensure_ascii=False),
                     json.dumps(ev.locator, ensure_ascii=False),
                     json.dumps(ev.table_ctx, ensure_ascii=False) if ev.table_ctx else None,
                     ev.page_no if ev.page_no is not None else
                     (ev.locator.get("page_index") if ev.locator.get("kind") == "pdf" else None),
                     json.dumps(ev.meta, ensure_ascii=False) if ev.meta else None))
                rowid = cur.lastrowid
                conn.execute(
                    "INSERT INTO evidence_fts(rowid, tokens) VALUES(?,?)",
                    (rowid, fts_tokens_text(ev.text_norm + " " +
                                            " ".join(ev.heading_path) + " " +
                                            self._table_ctx_text(ev.table_ctx))))
                conn.execute(
                    "INSERT INTO evidence_vec(evidence_rowid, vec) VALUES(?,?)",
                    (rowid, vec_to_blob(EMBEDDER.embed(ev.text_norm))))
            for pg in result.get("page_geom", []):
                conn.execute(
                    "INSERT OR REPLACE INTO page_geometry(revision_id, page_index, "
                    "width, height, rotation, printed_page, crop_json) VALUES(?,?,?,?,?,?,?)",
                    (rev_id, pg["page_index"], pg["width"], pg["height"],
                     pg.get("rotation", 0), pg.get("printed_page"),
                     json.dumps(pg.get("crop")) if pg.get("crop") else None))
            for field, info in (fields or {}).items():
                conn.execute(
                    "INSERT OR REPLACE INTO document_fields(version_id, field, value, "
                    "source, note) VALUES(?,?,?,?,?)",
                    (ver_id, field, info.get("value"), "原件", cover_status))
            conn.execute("UPDATE versions SET status='published' WHERE id=?", (ver_id,))
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def _extract_document_fields(self, evidences: list, fmt: str,
                                 coverage: dict) -> tuple[dict, str]:
        """从原件正文提取制度编号与日期；区分「未载明」与「封面无法读取」。

        只有确实读过相应部位且没有字段时才标注「原件未载明」；
        首页无文本层（扫描封面）则标注「封面无法读取，编号待核」。
        """
        head_text = "\n".join(e.text_orig for e in evidences[:12])
        fields: dict[str, dict] = {}
        for field, pat in _FIELD_PATTERNS.items():
            m = pat.search(head_text)
            if m:
                fields[field] = {"value": m.group(1).strip(), "source": "原件"}
        skipped_first = any(
            1 in s.get("pages", []) for s in (coverage.get("skipped") or []))
        cover_status = "unreadable" if (fmt == "pdf" and skipped_first) else \
            ("absent" if "制度编号" not in fields else "ok")
        return fields, cover_status

    def rebuild_index(self, conn: sqlite3.Connection, version_id: str) -> dict:
        """索引损坏/解析器升级：从原件重新解析并发布新修订（手册 §4.2）。

        旧修订及其证据保持不动，历史引用仍指向当时的修订。
        """
        v = conn.execute("SELECT * FROM versions WHERE id=?", (version_id,)).fetchone()
        if v is None:
            raise ValueError("版本不存在")
        original = self.store.path_for(v["sha256"])
        if not original.is_file():
            raise ValueError("原件缺失，请先从备份恢复")
        result = self._parse(v["format"], str(original))
        coverage = result["coverage"]
        fields, cover_status = self._extract_document_fields(
            result["evidences"], v["format"], coverage.to_dict())
        cov = coverage.to_dict()
        cov["document_fields"] = fields
        cov["cover_status"] = cover_status
        rev_id = DB.new_id("rev")
        self._save_tree(rev_id, result)
        self._publish_revision(
            conn, version_id, rev_id,
            f"{PARSER_VERSION}+{result.get('backend', 'unknown')}",
            json.dumps(cov, ensure_ascii=False), result["evidences"], result)
        return {"version_id": version_id, "revision_id": rev_id,
                "evidence_count": len(result["evidences"])}

    def _table_ctx_text(self, ctx: Optional[dict]) -> str:
        if not ctx:
            return ""
        parts = [ctx.get("table_name") or ""]
        parts += ctx.get("row_headers", []) + ctx.get("col_headers", [])
        parts.append(ctx.get("unit") or "")
        return " ".join(p for p in parts if p)

    def _parse(self, fmt: str, path: str) -> dict:
        if fmt == "docx":
            return parse_docx(path)
        return parse_pdf(path)

    def _save_tree(self, revision_id: str, result: dict) -> None:
        """Word 结构树落盘，供阅读器渲染（与定位契约同源）。"""
        tree = result.get("tree")
        if tree is None:
            return
        tree_dir = self.cfg.data_dir / "trees"
        tree_dir.mkdir(parents=True, exist_ok=True)
        temp = self.cfg.tmp_dir / f"{revision_id}.part"
        temp.write_text(
            json.dumps({"tree": tree, "footnotes": result.get("footnotes", {})},
                       ensure_ascii=False), encoding="utf-8")
        temp.replace(tree_dir / f"{revision_id}.json")
