"""检索：FTS5 中文预分词 + 本地向量矩阵（手册 §4、§5 第 5 条）。

首版向量后端为内存/本地矩阵的精确检索；Embedder 接口可替换为
Ollama 中文向量模型（规则先行、模型后接的既定路线）。
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import struct
from typing import Optional, Sequence

import numpy as np

try:
    import jieba
    jieba.setLogLevel(60)  # 静默
    _HAS_JIEBA = True
except ImportError:
    _HAS_JIEBA = False

_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)

VEC_DIM = 256


def tokenize(text: str) -> list[str]:
    """中文预分词：jieba 搜索模式；不可用时退化为单字+双字组合。"""
    text = text.lower()
    if _HAS_JIEBA:
        toks = [t.strip() for t in jieba.cut_for_search(text)]
        return [t for t in toks if _TOKEN_RE.fullmatch(t)]
    # 退化路径：连续汉字做单字+双字，字母数字整词
    out: list[str] = []
    for m in re.finditer(r"[a-z0-9]+|[\u4e00-\u9fff]+", text):
        s = m.group(0)
        if re.fullmatch(r"[a-z0-9]+", s):
            out.append(s)
        else:
            out.extend(list(s))
            out.extend(s[i:i + 2] for i in range(len(s) - 1))
    return out


def fts_tokens_text(text: str) -> str:
    return " ".join(tokenize(text))


# 确认/疑问式问法：句中的金额是用户待核实的猜想值，不是检索线索。
# 否则“住宿标准不低于500元吗”会把库里所有含 500 的档位拉进来制造伪冲突。
_GUESS_AMOUNT_RE = re.compile(
    r"(?:是不是|是否|是|吗|吧|能不能|可以|够不够|够吗|不低于|不高于|不超过|至少)\s*"
    r"([0-9][0-9,，.]*)\s*(?:元|块)")
_AMOUNT_TOKEN_RE = re.compile(r"^[0-9][0-9,，.]*$")


def fts_match_query(question: str, extra_terms: Sequence[str] = ()) -> str:
    """把问题转成 FTS5 MATCH 表达式（词项 OR）。"""
    terms = tokenize(question) + list(extra_terms)
    # 疑问式金额只用于比对结论，不作为检索词；主题词仍然保留。
    guessed = {t for t in _GUESS_AMOUNT_RE.findall(question)}
    if guessed:
        terms = [t for t in terms if not (t in guessed or _AMOUNT_TOKEN_RE.fullmatch(t))]
    terms = list(dict.fromkeys(terms))
    if not terms:
        return '""'
    return " OR ".join(f'"{t}"' for t in terms[:64])


class HashingEmbedder:
    """确定性哈希词袋向量：无随机性、CPU 即算、接口与真实嵌入模型一致。"""
    name = "hashing-bow-v1"

    def embed(self, text: str) -> np.ndarray:
        v = np.zeros(VEC_DIM, dtype=np.float32)
        for tok in tokenize(text):
            h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
            idx = h % VEC_DIM
            sign = 1.0 if (h >> 8) % 2 == 0 else -1.0
            v[idx] += sign
        n = np.linalg.norm(v)
        if n > 0:
            v /= n
        return v

    def embed_batch(self, texts: Sequence[str]) -> np.ndarray:
        return np.stack([self.embed(t) for t in texts]) if texts else np.zeros((0, VEC_DIM), dtype=np.float32)


def vec_to_blob(v: np.ndarray) -> bytes:
    return struct.pack(f"<{len(v)}f", *v.tolist())


def search_fts(conn: sqlite3.Connection, revision_ids: Sequence[str],
               match_expr: str, limit: int = 40) -> list[tuple[str, float]]:
    """返回 [(evidence_id, score)]，score 为 -bm25（越大越好）。"""
    if not revision_ids or not match_expr.strip('"'):
        return []
    marks = ",".join("?" * len(revision_ids))
    sql = f"""
      SELECT e.id AS id, bm25(evidence_fts) AS score
      FROM evidence_fts f JOIN evidence e ON e.rowid = f.rowid
      WHERE evidence_fts MATCH ? AND e.revision_id IN ({marks})
      ORDER BY score LIMIT ?
    """
    try:
        rows = conn.execute(sql, (match_expr, *revision_ids, limit)).fetchall()
    except sqlite3.OperationalError:
        return []
    return [(r["id"], -float(r["score"])) for r in rows]


class VectorSearch:
    """Reuse the last immutable revision scope on the single query worker."""

    def __init__(self):
        self._last = None

    def search(self, conn: sqlite3.Connection, revision_ids: Sequence[str],
               qvec: np.ndarray, limit: int = 40) -> list[tuple[str, float]]:
        if not revision_ids or not np.any(qvec):
            return []
        scope = tuple(sorted(set(revision_ids)))
        key = (scope, qvec.size)
        cached = self._last
        if cached is not None and cached[0] == key:
            _, ids, matrix = cached
        else:
            marks = ",".join("?" * len(scope))
            rows = conn.execute(
                f"""SELECT v.vec, e.id FROM evidence_vec v
                    JOIN evidence e ON e.rowid = v.evidence_rowid
                    WHERE e.revision_id IN ({marks}) ORDER BY e.rowid""", scope)
            compatible = [(row["id"], row["vec"]) for row in rows
                          if len(row["vec"]) == qvec.nbytes]
            if not compatible:
                return []
            ids = [eid for eid, _ in compatible]
            matrix = np.frombuffer(b"".join(value for _, value in compatible),
                                   dtype="<f4").reshape(len(ids), qvec.size)
            self._last = (key, ids, matrix) if matrix.nbytes <= 32 * 1024**2 else None
        scores = np.einsum("ij,j->i", matrix, qvec)
        order = np.argsort(-scores, kind="stable")[:limit]
        return [(ids[i], float(scores[i])) for i in order]


def reciprocal_rank_fusion(ranked_lists: Sequence[Sequence[tuple[str, float]]],
                           k: int = 60) -> list[str]:
    """按名次融合去重（手册 §5 第 5 条）。"""
    scores: dict[str, float] = {}
    for lst in ranked_lists:
        for rank, (eid, _s) in enumerate(lst):
            scores[eid] = scores.get(eid, 0.0) + 1.0 / (k + rank + 1)
    return [eid for eid, _ in sorted(scores.items(), key=lambda kv: -kv[1])]
