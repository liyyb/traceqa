"""Versioned semantic index with Ollama's local embeddings API."""
from __future__ import annotations

import json
import sqlite3
from functools import lru_cache
from http.client import HTTPException
from urllib.error import URLError
from urllib.request import ProxyHandler, Request, build_opener

import numpy as np

from .config import Config
from .ollama import NoRedirect
from .retrieval import vec_to_blob

BASE_URL = "http://127.0.0.1:11434/api"


def _call(endpoint: str, body: dict | None = None) -> dict:
    request = Request(f"{BASE_URL}/{endpoint}",
                      data=None if body is None else json.dumps(body).encode("utf-8"),
                      headers={"Content-Type": "application/json"})
    with build_opener(ProxyHandler({}), NoRedirect()).open(request, timeout=8) as response:
        size_limit = 8 * 1024 * 1024
        content = response.read(size_limit + 1)
    if len(content) > size_limit:
        raise ValueError("Ollama response exceeds the size limit")
    result = json.loads(content)
    if not isinstance(result, dict):
        raise ValueError("Invalid Ollama response")
    return result


def model_key(cfg: Config) -> str | None:
    if not cfg.embedding_model or not cfg.embedding_digest:
        return None
    name = cfg.embedding_model
    accepted_names = {name, f"{name}:latest"} if ":" not in name.rsplit("/", 1)[-1] else {name}
    digest = cfg.embedding_digest.removeprefix("sha256:")
    for model in _call("tags").get("models", []):
        if model.get("name") in accepted_names and model.get("digest", "").removeprefix("sha256:") == digest:
            return f"{model['name']}@{digest}"
    return None


def embed(model: str, texts: list[str]) -> np.ndarray:
    payload = _call("embed", {"model": model, "input": texts,
                             "truncate": True, "options": {"num_ctx": 2048}})
    vectors = np.asarray(payload["embeddings"], dtype="<f4")
    if vectors.ndim != 2 or vectors.shape[0] != len(texts) or not 1 <= vectors.shape[1] <= 8192:
        raise ValueError("Unexpected embedding dimensions")
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if not np.all(np.isfinite(norms)) or np.any(norms == 0):
        raise ValueError("Invalid embedding values")
    return vectors / norms


@lru_cache(maxsize=64)
def _question_vector(key: str, question: str) -> np.ndarray:
    model = key.rsplit("@", 1)[0]
    return embed(model, [question])[0]


def build_index(conn: sqlite3.Connection, cfg: Config) -> int:
    key = model_key(cfg)
    if not key:
        raise ValueError("请配置本机 Ollama 嵌入模型以及其完整摘要，再运行索引构建")
    pending = conn.execute(
        "SELECT e.id, e.text_norm FROM evidence e "
        "JOIN parse_revisions r ON r.id=e.revision_id AND r.status='ready' "
        "LEFT JOIN semantic_vectors s ON s.evidence_id=e.id AND s.model_key=? "
        "WHERE s.evidence_id IS NULL ORDER BY e.rowid", (key,),
    ).fetchall()
    for start in range(0, len(pending), 16):
        rows = pending[start:start + 16]
        vectors = embed(cfg.embedding_model, [row["text_norm"][:1600] for row in rows])
        if model_key(cfg) != key:
            raise ValueError("嵌入模型在构建过程中发生变化，请检查模型摘要")
        with conn:
            conn.executemany("INSERT OR IGNORE INTO semantic_vectors VALUES(?,?,?)",
                             [(row["id"], key, vec_to_blob(vector))
                              for row, vector in zip(rows, vectors, strict=True)])
    return len(pending)


def search(conn: sqlite3.Connection, question: str, scope: list[str],
           key: str, limit: int = 30) -> list[tuple[str, float]]:
    if not scope:
        return []
    placeholders = ",".join("?" for _ in scope)
    rows = conn.execute(
        f"SELECT s.evidence_id, s.vec FROM semantic_vectors s "
        f"JOIN evidence e ON e.id=s.evidence_id WHERE s.model_key=? "
        f"AND e.revision_id IN ({placeholders})", (key, *scope),
    ).fetchall()
    if not rows:
        return []
    query = _question_vector(key, question[:1600])
    compatible = [row for row in rows if len(row["vec"]) == query.nbytes]
    if not compatible:
        return []
    matrix = np.frombuffer(b"".join(row["vec"] for row in compatible), dtype="<f4")
    scores = matrix.reshape(len(compatible), query.size) @ query
    order = np.argsort(-scores, kind="stable")[:limit]
    return [(compatible[i]["evidence_id"], float(scores[i])) for i in order]


UNAVAILABLE = (URLError, HTTPException, OSError, ValueError, TypeError, KeyError, AttributeError)
