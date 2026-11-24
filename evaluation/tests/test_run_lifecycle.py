"""Release regressions for cancellation and in-place upgrades."""
import io
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stdout
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "parsers"))

from traceqa_server import backup, db
from traceqa_server.config import Config
from traceqa_server.imports_svc import ImportService
from traceqa_server.runs_svc import RunService


class RunLifecycleTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cfg = Config(data_dir=Path(temp.name))

    def test_legacy_documents_gain_index_without_losing_rows(self):
        with closing(sqlite3.connect(self.cfg.db_path)) as conn:
            conn.executescript(
                "CREATE TABLE documents(id TEXT PRIMARY KEY,title TEXT,created_at TEXT);"
                "INSERT INTO documents VALUES('old','旧制度','2026-01-01');"
            )
        db.init_db(self.cfg.db_path)
        db.init_db(self.cfg.db_path)
        with closing(db.connect(self.cfg.db_path)) as conn:
            row = conn.execute("SELECT title, doc_key FROM documents").fetchone()
            self.assertEqual(tuple(row), ("旧制度", None))
            indexes = {row["name"] for row in conn.execute("PRAGMA index_list(documents)")}
            self.assertIn("idx_documents_folder_key", indexes)

    def test_cancel_before_commit_publishes_no_claims(self):
        db.init_db(self.cfg.db_path)
        service = RunService(self.cfg, self.cfg.db_path)
        with closing(db.connect(self.cfg.db_path)) as conn, conn:
            conn.execute(
                "INSERT INTO runs(id, question, scope_json, model_version, rules_version, "
                "status, created_at) VALUES(?,?,?,?,?,?,?)",
                ("run", "早餐能报销吗？", "[]", "test", "test", db.RUN_QUEUED, db.now_iso()),
            )
        emit = service._emit

        def cancel_on_committing(conn, run_id, stage, done, total, payload=None):
            seq = emit(conn, run_id, stage, done, total, payload)
            if stage == db.RUN_COMMITTING:
                with closing(db.connect(self.cfg.db_path)) as other:
                    self.assertEqual(service.request_cancel(other, run_id)["status"], "cancelling")
            return seq

        with patch.object(service, "_emit", side_effect=cancel_on_committing):
            service.execute("run")
        with closing(db.connect(self.cfg.db_path)) as conn:
            snapshot = service.snapshot(conn, "run")
            self.assertEqual(snapshot["status"], db.RUN_CANCELLED)
            self.assertEqual(snapshot["claims"], [])
            self.assertEqual(service.events_since(conn, "run", 0)[-1]["stage"], db.RUN_CANCELLED)

    def test_duplicate_after_parse_error_retries_the_parser(self):
        self.cfg.ensure_dirs()
        db.init_db(self.cfg.db_path)
        imports = ImportService(self.cfg)
        doc = ROOT / "evaluation" / "fixtures" / "差旅管理办法.docx"
        data = doc.read_bytes()
        with closing(db.connect(self.cfg.db_path)) as conn:
            def upload():
                batch = imports.create_import(conn, [(doc.name, data)])
                item = imports.get_import(conn, batch)["files"][0]
                imports.process_file(conn, item["id"], doc.name, data)
                return imports.get_import(conn, batch)["files"][0]
            with patch.object(imports, "_parse", side_effect=OSError("temporary error")):
                failed = upload()
            self.assertEqual(failed["status"], "failed")
            success = upload()
            self.assertEqual(success["status"], "done")
            self.assertTrue(success["revision_id"])
            self.assertEqual(success["version_id"], failed["version_id"])
            self.assertEqual(upload()["revision_id"], success["revision_id"])
            self.assertEqual(list(self.cfg.tmp_dir.glob("*.part")), [])

    def test_missing_restore_source_does_not_block_opening_existing_library(self):
        self.cfg.ensure_dirs()
        db.init_db(self.cfg.db_path)
        snapshot = backup.create_backup(self.cfg)
        backup.schedule_restore(self.cfg, snapshot)
        with closing(db.connect(self.cfg.db_path)) as conn, conn:
            conn.execute("INSERT INTO kv VALUES('keep', 'current value')")
        snapshot.rename(snapshot.with_name("moved-snapshot"))
        with redirect_stdout(io.StringIO()) as captured:
            self.assertIsNone(backup.apply_pending_restore(self.cfg))
        self.assertIn("继续打开现有资料库", captured.getvalue())
        self.assertEqual(len(list(self.cfg.data_dir.glob("restore-failed-*.json"))), 1)
        with closing(db.connect(self.cfg.db_path)) as conn:
            self.assertEqual(conn.execute("SELECT value FROM kv WHERE key='keep'").fetchone()[0],
                             "current value")
