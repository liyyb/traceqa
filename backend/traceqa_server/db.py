"""SQLite 模式与连接管理（手册 §4 核心实体）。"""
from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

VERSION_COLUMNS = """
  id TEXT PRIMARY KEY,
  document_id TEXT NOT NULL REFERENCES documents(id),
  sha256 TEXT NOT NULL,
  file_name TEXT NOT NULL,
  format TEXT NOT NULL,
  size_bytes INTEGER NOT NULL,
  stored_path TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'stored'
"""

SCHEMA = f"""
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS documents(
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  doc_key TEXT,
  folder_path TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS versions({VERSION_COLUMNS});

CREATE TABLE IF NOT EXISTS parse_revisions(
  id TEXT PRIMARY KEY,
  version_id TEXT NOT NULL REFERENCES versions(id),
  parser_version TEXT NOT NULL,
  index_version TEXT NOT NULL,
  status TEXT NOT NULL,
  coverage_json TEXT,
  error TEXT,
  created_at TEXT NOT NULL,
  published_at TEXT
);

CREATE TABLE IF NOT EXISTS evidence(
  id TEXT PRIMARY KEY,
  revision_id TEXT NOT NULL REFERENCES parse_revisions(id),
  chunk_no INTEGER NOT NULL,
  text_orig TEXT NOT NULL,
  text_norm TEXT NOT NULL,
  heading_path_json TEXT NOT NULL DEFAULT '[]',
  locator_json TEXT NOT NULL,
  table_ctx_json TEXT,
  page_no INTEGER,
  meta_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_evidence_rev ON evidence(revision_id);
CREATE INDEX IF NOT EXISTS idx_evidence_rev_chunk ON evidence(revision_id, chunk_no);

-- PDF 页面几何（手册 §5.1）：页面尺寸、裁切框、旋转角、印刷页码。
-- 每条证据的归一化框依赖该页几何，历史修订保留当时的值。
CREATE TABLE IF NOT EXISTS page_geometry(
  revision_id TEXT NOT NULL REFERENCES parse_revisions(id),
  page_index INTEGER NOT NULL,
  width REAL NOT NULL,
  height REAL NOT NULL,
  rotation INTEGER NOT NULL DEFAULT 0,
  printed_page TEXT,
  crop_json TEXT,
  PRIMARY KEY(revision_id, page_index)
);

CREATE VIRTUAL TABLE IF NOT EXISTS evidence_fts USING fts5(
  tokens, content='', tokenize='unicode61'
);

CREATE TABLE IF NOT EXISTS evidence_vec(
  evidence_rowid INTEGER PRIMARY KEY,
  vec BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS semantic_vectors(
  evidence_id TEXT NOT NULL REFERENCES evidence(id) ON DELETE CASCADE,
  model_key TEXT NOT NULL,
  vec BLOB NOT NULL,
  PRIMARY KEY(evidence_id, model_key)
);

CREATE TABLE IF NOT EXISTS runs(
  id TEXT PRIMARY KEY,
  request_id TEXT UNIQUE,
  question TEXT NOT NULL,
  conditions_json TEXT NOT NULL DEFAULT '{{}}',
  scope_json TEXT NOT NULL,
  model_version TEXT NOT NULL,
  rules_version TEXT NOT NULL,
  status TEXT NOT NULL,
  cancel_requested INTEGER NOT NULL DEFAULT 0,
  seq INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  parent_run_id TEXT REFERENCES runs(id),
  rerun_meta_json TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS run_events(
  run_id TEXT NOT NULL REFERENCES runs(id),
  seq INTEGER NOT NULL,
  stage TEXT NOT NULL,
  done INTEGER,
  total INTEGER,
  payload_json TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (run_id, seq)
);

CREATE TABLE IF NOT EXISTS claims(
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  seq_no INTEGER NOT NULL,
  topic TEXT NOT NULL,
  question_text TEXT NOT NULL,
  status TEXT NOT NULL,
  conclusion_text TEXT,
  conditions_json TEXT,
  followup_json TEXT,
  gap_json TEXT,
  amount_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_claims_run ON claims(run_id);

CREATE TABLE IF NOT EXISTS claim_evidence(
  id TEXT PRIMARY KEY,
  claim_id TEXT NOT NULL REFERENCES claims(id),
  evidence_id TEXT NOT NULL REFERENCES evidence(id),
  relation TEXT NOT NULL,
  group_no INTEGER NOT NULL DEFAULT 0,
  quote TEXT
);
CREATE INDEX IF NOT EXISTS idx_ce_claim ON claim_evidence(claim_id);
CREATE INDEX IF NOT EXISTS idx_ce_evidence ON claim_evidence(evidence_id);

CREATE TABLE IF NOT EXISTS notes(
  id TEXT PRIMARY KEY,
  claim_id TEXT NOT NULL REFERENCES claims(id),
  body TEXT NOT NULL,
  source TEXT NOT NULL,
  contact_time TEXT,
  signature TEXT,
  recorded_at TEXT NOT NULL,
  draft_version INTEGER NOT NULL DEFAULT 1,
  status TEXT NOT NULL DEFAULT 'saved'
);
CREATE INDEX IF NOT EXISTS idx_notes_claim ON notes(claim_id);

CREATE TABLE IF NOT EXISTS metadata_notes(
  id TEXT PRIMARY KEY,
  version_id TEXT NOT NULL REFERENCES versions(id),
  field TEXT NOT NULL,
  value TEXT NOT NULL,
  source TEXT NOT NULL,
  signature TEXT,
  created_at TEXT NOT NULL
);

-- 原件自身印出的制度字段（与经办人补录分开保存，手册 §2.3）
CREATE TABLE IF NOT EXISTS document_fields(
  version_id TEXT NOT NULL REFERENCES versions(id),
  field TEXT NOT NULL,
  value TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT '原件',
  note TEXT,
  PRIMARY KEY(version_id, field, source)
);

CREATE TABLE IF NOT EXISTS records(
  id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  serial_no TEXT NOT NULL,
  rev_no TEXT NOT NULL,
  parent_record_id TEXT REFERENCES records(id),
  correction_reason TEXT,
  frozen_json TEXT NOT NULL,
  file_path TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(serial_no, rev_no)
);
CREATE INDEX IF NOT EXISTS idx_records_run ON records(run_id);

CREATE TABLE IF NOT EXISTS supplements(
  id TEXT PRIMARY KEY,
  original_revision_id TEXT NOT NULL REFERENCES parse_revisions(id),
  gap_json TEXT NOT NULL,
  supplement_revision_id TEXT NOT NULL REFERENCES parse_revisions(id),
  status TEXT NOT NULL DEFAULT 'linked',
  created_at TEXT NOT NULL,
  UNIQUE(original_revision_id, supplement_revision_id)
);

CREATE TABLE IF NOT EXISTS scope_last(
  id INTEGER PRIMARY KEY CHECK (id = 1),
  revisions_json TEXT NOT NULL,
  run_id TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS imports(
  id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE TABLE IF NOT EXISTS import_files(
  id TEXT PRIMARY KEY,
  import_id TEXT NOT NULL REFERENCES imports(id),
  file_name TEXT NOT NULL,
  folder_path TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  version_id TEXT REFERENCES versions(id),
  revision_id TEXT REFERENCES parse_revisions(id),
  error TEXT,
  coverage_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_if_import ON import_files(import_id);
"""

# 任务状态机（手册 §4）：排队→检索→核查→提交→完成；执行阶段可入失败/取消
RUN_QUEUED = "queued"
RUN_RETRIEVING = "retrieving"
RUN_CHECKING = "checking"
RUN_COMMITTING = "committing"
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"
RUN_CANCELLED = "cancelled"
RUN_INTERRUPTED = "interrupted"
RUN_ACTIVE = {RUN_QUEUED, RUN_RETRIEVING, RUN_CHECKING, RUN_COMMITTING}
RUN_TERMINAL = {RUN_COMPLETED, RUN_FAILED, RUN_CANCELLED}

# 子问题业务状态（手册 §5 第 7 条、§4.1 读模型）
CLAIM_ANSWERED = "answered"
CLAIM_NEEDS_CONDITION = "needs_condition"
CLAIM_NO_EVIDENCE = "no_evidence"
CLAIM_CONFLICT = "conflict"
CLAIM_COVERAGE_GAP = "coverage_gap"

STAGE_LABELS = {
    RUN_QUEUED: "已排队",
    RUN_RETRIEVING: "正在检索",
    RUN_CHECKING: "正在核对依据",
    RUN_COMMITTING: "正在提交结果",
    RUN_COMPLETED: "已完成",
    RUN_FAILED: "失败",
    RUN_CANCELLED: "已取消",
    RUN_INTERRUPTED: "已中断",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
        # 轻量迁移：早期库没有 documents.doc_key，补列后由导入端回填
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(documents)")}
        if "doc_key" not in cols:
            conn.execute("ALTER TABLE documents ADD COLUMN doc_key TEXT")
        if "folder_path" not in cols:
            conn.execute("ALTER TABLE documents ADD COLUMN folder_path TEXT NOT NULL DEFAULT ''")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_folder_key "
                     "ON documents(folder_path, doc_key) WHERE doc_key IS NOT NULL")
        conn.execute("DROP INDEX IF EXISTS idx_documents_key")
        import_cols = {r["name"] for r in conn.execute("PRAGMA table_info(import_files)")}
        if "folder_path" not in import_cols:
            conn.execute("ALTER TABLE import_files ADD COLUMN folder_path TEXT NOT NULL DEFAULT ''")
        # 轻量迁移：早期库没有 claims.amount_json，补列后新提交写入金额契约
        claim_cols = {r["name"] for r in conn.execute("PRAGMA table_info(claims)")}
        if "amount_json" not in claim_cols:
            conn.execute("ALTER TABLE claims ADD COLUMN amount_json TEXT")
        conn.commit()
        _allow_shared_originals(conn)
    finally:
        conn.close()


def _allow_shared_originals(conn: sqlite3.Connection) -> None:
    """Retain historical version IDs while deduplicating originals across folders on disk."""
    old_constraint = conn.execute(
        """SELECT 1 FROM pragma_index_list('versions') AS idx
           JOIN pragma_index_info(idx.name) AS col
           WHERE idx.origin = 'u' AND col.name = 'sha256'"""
    ).fetchone()
    if old_constraint:
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(f"CREATE TABLE versions_new({VERSION_COLUMNS})")
                columns = ", ".join(row["name"] for row in conn.execute("PRAGMA table_info(versions_new)"))
                conn.execute(f"INSERT INTO versions_new({columns}) SELECT {columns} FROM versions")
                conn.execute("DROP TABLE versions")
                conn.execute("ALTER TABLE versions_new RENAME TO versions")
                if conn.execute("PRAGMA foreign_key_check").fetchone():
                    raise sqlite3.IntegrityError("材料库迁移后检测到无效引用")
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
    with conn:
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_versions_document_hash "
                     "ON versions(document_id, sha256)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_versions_hash ON versions(sha256)")


def recover_interrupted(conn: sqlite3.Connection) -> list[str]:
    """应用异常退出后把中断任务标为可重试（手册 §4）。返回受影响 run_id。"""
    cur = conn.execute(
        "SELECT id FROM runs WHERE status IN (?, ?, ?, ?)",
        (RUN_QUEUED, RUN_RETRIEVING, RUN_CHECKING, RUN_COMMITTING))
    ids = [r["id"] for r in cur.fetchall()]
    if ids:
        conn.executemany(
            "UPDATE runs SET status=?, error='进程退出导致任务中断，可重试' WHERE id=?",
            [(RUN_INTERRUPTED, i) for i in ids])
        conn.commit()
    return ids
