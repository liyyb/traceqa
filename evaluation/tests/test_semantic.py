"""Protect embedding cache keys and revision scope."""
import sqlite3
import sys
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
from traceqa_server import semantic


class SemanticIndexTests(unittest.TestCase):
    def test_search_scopes_vectors_and_reuses_query_embedding(self):
        with closing(sqlite3.connect(":memory:")) as conn:
            conn.row_factory = sqlite3.Row
            conn.executescript(
                "CREATE TABLE evidence(id TEXT, revision_id TEXT);"
                "CREATE TABLE semantic_vectors(evidence_id TEXT, model_key TEXT, vec BLOB);"
                "INSERT INTO evidence VALUES ('a', 'chosen'), ('b', 'other');"
            )
            key = "test:latest@sha"
            vector = np.array([1, 0], dtype="<f4")
            conn.executemany("INSERT INTO semantic_vectors VALUES (?,?,?)",
                             [(eid, key, vector.tobytes()) for eid in ("a", "b")])
            self.addCleanup(semantic._question_vector.cache_clear)
            with patch.object(semantic, "embed", return_value=vector[None, :]) as embed:
                self.assertEqual(semantic.search(conn, "住宿", ["chosen"], key), [("a", 1.0)])
                self.assertEqual(semantic.search(conn, "住宿", ["other"], key), [("b", 1.0)])
                self.assertEqual(semantic.search(conn, "住宿", ["chosen"], "test:latest@other"), [])
                embed.assert_called_once_with("test:latest", ["住宿"])
