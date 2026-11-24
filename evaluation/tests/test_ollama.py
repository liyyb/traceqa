"""Exercise the local HTTP contract without downloading a model."""
import json
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "backend"))
sys.path.insert(0, str(PROJECT / "parsers"))
from traceqa_server import db, ollama
from traceqa_server.config import Config
from traceqa_server.imports_svc import ImportService
from traceqa_server.runs_svc import RunService


class OllamaTests(unittest.TestCase):
    def setUp(self):
        self.answer = {"indices": [1, 0, 1]}
        self.request = None
        case = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                case.request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                body = json.dumps({"done": True, "response": json.dumps(case.answer)}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = HTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(server.server_close)
        self.url = f"http://127.0.0.1:{server.server_port}/api/generate"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(thread.join)
        self.addCleanup(server.shutdown)
        self.rows = [{"id": "first", "text_orig": "原条款一"},
                     {"id": "second", "text_orig": "原条款二"}]

    def test_returns_original_rows_and_sends_bounded_structured_request(self):
        with patch.object(ollama, "URL", self.url):
            selected = ollama.select_passages("local-test", "如何审批？", self.rows)
        self.assertEqual(selected, [self.rows[1], self.rows[0]])
        self.assertEqual(self.request["model"], "local-test")
        self.assertFalse(self.request["stream"])
        self.assertEqual(json.loads(self.request["prompt"])["passages"][1]["text"], "原条款二")

    def test_rejects_unissued_indices_or_accepts_empty_selection(self):
        with patch.object(ollama, "URL", self.url):
            for invalid in ([2], [-1], [True], ["1"], [0, 1, 0, 1]):
                with self.subTest(invalid=invalid):
                    self.answer = {"indices": invalid}
                    self.assertIsNone(ollama.select_passages("local-test", "问题", self.rows))
            self.answer = {"indices": []}
            self.assertEqual(ollama.select_passages("local-test", "问题", self.rows), [])

    def test_connection_failure_keeps_rule_result_available(self):
        with patch.object(ollama, "build_opener") as build:
            build.return_value.open.side_effect = TimeoutError
            self.assertIsNone(ollama.select_passages("local-test", "问题", self.rows))

    def test_natural_entry_point_records_passages_without_upgrading_claim(self):
        with tempfile.TemporaryDirectory() as temp:
            cfg = Config(data_dir=Path(temp), ollama_model="local-test")
            cfg.ensure_dirs()
            db.init_db(cfg.db_path)
            imports = ImportService(cfg)
            runs = RunService(cfg, cfg.db_path)
            with closing(db.connect(cfg.db_path)) as conn:
                fixture = PROJECT / "evaluation" / "fixtures" / "差旅管理办法.docx"
                data = fixture.read_bytes()
                imp = imports.create_import(conn, [(fixture.name, data)])
                file = imports.get_import(conn, imp)["files"][0]
                imports.process_file(conn, file["id"], fixture.name, data)
                revision = imports.get_import(conn, imp)["files"][0]["revision_id"]
                status, submitted = runs.submit(conn, "req", "早餐单独报销吗？", [revision], {})
                self.assertEqual(status, 202)
                self.answer = {"indices": [0]}
                with patch.object(ollama, "URL", self.url):
                    runs.execute(submitted["run_id"])
                snapshot = runs.snapshot(conn, submitted["run_id"])
                self.assertEqual(snapshot["status"], db.RUN_COMPLETED)
                self.assertTrue(snapshot["model_version"].endswith("+ollama:local-test"))
                self.assertEqual(snapshot["claims"][0]["status"], db.CLAIM_NO_EVIDENCE)
                evidence, = snapshot["claims"][0]["evidences"]
                self.assertEqual(evidence["relation"], "related")
                self.assertEqual(evidence["quote"], evidence["text_orig"])
