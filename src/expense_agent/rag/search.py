"""Policy retrieval: vector, keyword and hybrid search over the LATEST handbook (decision D26).

Every query filters on ``is_latest`` and on ONE embedding model, and is fully parameterised.
Results are passages with ``section_id`` so answers can cite "EXP-6.3".
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, Literal

Method = Literal["vector", "keyword", "hybrid"]
METHODS: tuple[Method, ...] = ("vector", "keyword", "hybrid")
RRF_K = 60
CANDIDATES = 20
SECTION_ID_RE = re.compile(r"\bEXP-\d+(?:\.\d+)?\b", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z0-9]+")
DEFAULT_METHOD: Method = "hybrid"  # confirmed or replaced by the Phase 3 evaluation (docs/RAG.md)

Embed = Callable[[str], Sequence[float]]

# Plain literal SQL only (CLAUDE.md): values are always bind parameters.
VECTOR_SQL = """
SELECT section_id, chunk_index, title, text, version, 1 - (embedding <=> %s::vector) AS score
FROM policy_chunks
WHERE is_latest AND embedding_model = %s
ORDER BY embedding <=> %s::vector
LIMIT %s
"""

KEYWORD_SQL = """
SELECT section_id, chunk_index, title, text, version, ts_rank_cd(tsv, q) AS score
FROM policy_chunks, to_tsquery('english', %s) AS q
WHERE is_latest AND embedding_model = %s AND tsv @@ q
ORDER BY score DESC, section_id, chunk_index
LIMIT %s
"""

SECTION_ID_SQL = """
SELECT section_id, chunk_index, title, text, version, 1.0 AS score
FROM policy_chunks
WHERE is_latest AND embedding_model = %s
  AND EXISTS (
      SELECT 1 FROM unnest(%s::text[]) AS wanted(id)
      WHERE section_id = wanted.id OR section_id LIKE wanted.id || '.%%'
  )
ORDER BY section_id, chunk_index
LIMIT %s
"""


@dataclass(frozen=True)
class Passage:
    section_id: str
    chunk_index: int
    title: str
    text: str
    version: str
    score: float
    method: str

    @property
    def key(self) -> tuple[str, int]:
        return (self.section_id, self.chunk_index)


def vector_literal(vec: Sequence[float]) -> str:
    """pgvector text format, passed as a bind parameter and cast with ::vector."""
    return "[" + ",".join(f"{float(x):.8g}" for x in vec) + "]"


def or_query(question: str) -> str:
    """Words joined with OR for to_tsquery. Only [a-z0-9] survive, so it is always valid syntax;
    Postgres stems the words and drops stop words."""
    words = list(dict.fromkeys(_WORD_RE.findall(question.lower())))
    return " | ".join(words)


def section_ids_in(question: str) -> list[str]:
    return list(dict.fromkeys(m.upper() for m in SECTION_ID_RE.findall(question)))


def _passages(rows: Sequence[Sequence[Any]], method: str) -> list[Passage]:
    return [
        Passage(str(r[0]), int(r[1]), str(r[2]), str(r[3]), str(r[4]), float(r[5]), method)
        for r in rows
    ]


def vector_search(conn: Any, qvec: Sequence[float], model: str, k: int) -> list[Passage]:
    with conn.transaction():
        # pgvector 0.8: keep scanning the HNSW index until k rows pass the filters.
        conn.execute("SET LOCAL hnsw.iterative_scan = strict_order")
        lit = vector_literal(qvec)
        rows = conn.execute(VECTOR_SQL, (lit, model, lit, k)).fetchall()
    return _passages(rows, "vector")


def keyword_search(conn: Any, question: str, model: str, k: int) -> list[Passage]:
    out: list[Passage] = []
    ids = section_ids_in(question)
    if ids:  # exact citation asked for: put those sections first
        out = _passages(conn.execute(SECTION_ID_SQL, (model, ids, k)).fetchall(), "keyword")
    query = or_query(question)
    if query and len(out) < k:
        rows = conn.execute(KEYWORD_SQL, (query, model, k)).fetchall()
        seen = {p.key for p in out}
        out += [p for p in _passages(rows, "keyword") if p.key not in seen]
    return out[:k]


def rrf(rankings: Sequence[Sequence[Passage]], k: int, rrf_k: int = RRF_K) -> list[Passage]:
    """Reciprocal Rank Fusion: score = sum over lists of 1 / (rrf_k + rank), rank from 1."""
    scores: dict[tuple[str, int], float] = {}
    first: dict[tuple[str, int], Passage] = {}
    for ranking in rankings:
        for rank, p in enumerate(ranking, start=1):
            scores[p.key] = scores.get(p.key, 0.0) + 1.0 / (rrf_k + rank)
            first.setdefault(p.key, p)
    ordered = sorted(scores, key=lambda key: (-scores[key], key))
    return [replace(first[key], score=scores[key], method="hybrid") for key in ordered[:k]]


def hybrid_search(
    conn: Any, question: str, qvec: Sequence[float], model: str, k: int
) -> list[Passage]:
    vec = vector_search(conn, qvec, model, CANDIDATES)
    kw = keyword_search(conn, question, model, CANDIDATES)
    fused = rrf([vec, kw], k)
    ids = section_ids_in(question)
    if ids:  # an explicitly cited section always leads
        exact = [p for p in kw if p.section_id in ids or p.section_id.split(".")[0] in ids]
        lead = [replace(p, method="hybrid") for p in exact]
        seen = {p.key for p in lead}
        fused = (lead + [p for p in fused if p.key not in seen])[:k]
    return fused


def search(
    conn: Any, question: str, method: Method, model: str, embed: Embed, k: int = 5
) -> list[Passage]:
    if method == "keyword":
        return keyword_search(conn, question, model, k)
    qvec = embed(question)  # always the SAME model as the stored chunks
    if method == "vector":
        return vector_search(conn, qvec, model, k)
    return hybrid_search(conn, question, qvec, model, k)


def search_policy(
    conn: Any, question: str, embed: Embed, model: str, k: int = 5, method: Method | None = None
) -> list[Passage]:
    """Production entry point (the Phase 4 MCP tool). Every passage carries its section_id."""
    chosen = method or os.environ.get("EXPENSE_RAG_METHOD", DEFAULT_METHOD)
    if chosen not in METHODS:
        raise ValueError(f"unknown retrieval method {chosen!r}")
    return search(conn, question, chosen, model, embed, k)  # type: ignore[arg-type]
