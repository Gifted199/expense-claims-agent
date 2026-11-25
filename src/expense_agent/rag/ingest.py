"""One-off ingestion (and evaluation) job for the policy handbook. Runs as an ECS Fargate task.

    python -m expense_agent.rag.ingest --models text-embedding-3-small,text-embedding-3-large \
        [--evaluate] [--prune-other-models]

1. Read policy_limits, expense_categories and policy_city_hotel_caps as ``app_ingest``.
2. Render every handbook version from those tables (D24) and upload each to
   s3://<bucket>/policy/<version>/handbook.md with a manifest.json.
3. Upsert policy_documents; exactly one version is_latest.
4. Chunk each version (D25) and embed it with EACH model in --models, replacing that
   (version, model)'s rows in one transaction (D27).
5. --evaluate: run the labelled questions through vector / keyword / hybrid for every model,
   embedding each question with the SAME model as the chunks; write results to S3 (eval/rag/).
6. --prune-other-models: delete chunk rows for models not in --models (production = winner only).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from expense_agent.rag.chunking import Chunk, chunk_handbook
from expense_agent.rag.handbook import (
    LATEST,
    PREVIOUS,
    VERSIONS,
    PolicyData,
    policy_data_from_rows,
    render,
)

POLICY_PREFIX = "policy"
EVAL_PREFIX = "eval/rag"

LOAD_CATEGORIES_SQL = (
    "SELECT category_code, name, description, is_travel, receipt_required_over"
    " FROM expense_categories ORDER BY category_code"
)
LOAD_LIMITS_SQL = (
    "SELECT category_code, grade, per_claim_limit, monthly_limit FROM policy_limits"
    " ORDER BY category_code, grade"
)
LOAD_CITY_CAPS_SQL = (
    "SELECT city, grade, nightly_cap FROM policy_city_hotel_caps ORDER BY city, grade"
)
UPSERT_DOCUMENT_SQL = (
    "INSERT INTO policy_documents (version, effective_from, is_latest, s3_key, sha256)"
    " VALUES (%s, %s, false, %s, %s)"
    " ON CONFLICT (version) DO UPDATE SET effective_from = EXCLUDED.effective_from,"
    " is_latest = false, s3_key = EXCLUDED.s3_key, sha256 = EXCLUDED.sha256"
)
SET_LATEST_SQL = "UPDATE policy_documents SET is_latest = true WHERE version = %s"
SYNC_CHUNK_LATEST_SQL = "UPDATE policy_chunks SET is_latest = (version = %s)"
DELETE_CHUNKS_SQL = "DELETE FROM policy_chunks WHERE version = %s AND embedding_model = %s"
INSERT_CHUNK_SQL = (
    "INSERT INTO policy_chunks (version, is_latest, section_id, section_path, title, text,"
    " chunk_index, embedding_model, embedding)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::vector)"
)
PRUNE_SQL = "DELETE FROM policy_chunks WHERE NOT (embedding_model = ANY(%s))"


def run_id(now: datetime, git_sha: str) -> str:
    return f"{now.astimezone(UTC):%Y%m%dT%H%M%SZ}-{(git_sha or 'unknown')[:7]}"


def load_policy_data(conn: Any) -> PolicyData:
    return policy_data_from_rows(
        conn.execute(LOAD_CATEGORIES_SQL).fetchall(),
        conn.execute(LOAD_LIMITS_SQL).fetchall(),
        conn.execute(LOAD_CITY_CAPS_SQL).fetchall(),
    )


def render_versions(data: PolicyData) -> dict[str, str]:
    return {spec.version: render(data, spec) for spec in VERSIONS}


def publish_handbooks(s3: Any, bucket: str, docs: dict[str, str]) -> dict[str, tuple[str, str]]:
    """Upload each version's Markdown and manifest. Returns version -> (s3_key, sha256)."""
    out: dict[str, tuple[str, str]] = {}
    specs = {s.version: s for s in VERSIONS}
    for version, markdown in docs.items():
        body = markdown.encode("utf-8")
        sha = hashlib.sha256(body).hexdigest()
        key = f"{POLICY_PREFIX}/{version}/handbook.md"
        s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="text/markdown")
        manifest = {
            "version": version,
            "effective_from": specs[version].effective_from.isoformat(),
            "is_latest": version == LATEST.version,
            "sha256": sha,
            "handbook_key": key,
        }
        s3.put_object(
            Bucket=bucket,
            Key=f"{POLICY_PREFIX}/{version}/manifest.json",
            Body=json.dumps(manifest, indent=2).encode(),
            ContentType="application/json",
        )
        out[version] = (key, sha)
    return out


def register_documents(conn: Any, published: dict[str, tuple[str, str]]) -> None:
    """Upsert every version (all not-latest), then flag the latest, in one transaction."""
    specs = {s.version: s for s in VERSIONS}
    with conn.transaction():
        for version, (key, sha) in published.items():
            conn.execute(UPSERT_DOCUMENT_SQL, (version, specs[version].effective_from, key, sha))
        conn.execute(SET_LATEST_SQL, (LATEST.version,))


def replace_chunks(
    conn: Any, version: str, model: str, chunks: Sequence[Chunk], vectors: Sequence[Sequence[float]]
) -> int:
    """Delete then insert this (version, model)'s chunks atomically."""
    from expense_agent.rag.search import vector_literal

    if len(chunks) != len(vectors):
        raise ValueError("one embedding per chunk is required")
    rows = [
        (
            version,
            version == LATEST.version,
            c.section_id,
            c.section_path,
            c.title,
            c.text,
            c.chunk_index,
            model,
            vector_literal(v),
        )
        for c, v in zip(chunks, vectors, strict=True)
    ]
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(DELETE_CHUNKS_SQL, (version, model))
        cur.executemany(INSERT_CHUNK_SQL, rows)
    return len(rows)


def ingest(
    conn: Any,
    docs: dict[str, str],
    models: Sequence[str],
    embed: Callable[[Sequence[str], str], list[list[float]]],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for version, markdown in docs.items():
        chunks = chunk_handbook(markdown)
        for model in models:
            vectors = embed([c.text for c in chunks], model)
            counts[f"{version}/{model}"] = replace_chunks(conn, version, model, chunks, vectors)
    with conn.transaction():
        conn.execute(SYNC_CHUNK_LATEST_SQL, (LATEST.version,))
    return counts


def prune(conn: Any, keep_models: Sequence[str]) -> int:
    with conn.transaction():
        cur = conn.execute(PRUNE_SQL, (list(keep_models),))
    return int(getattr(cur, "rowcount", 0) or 0)


QUESTION_SETS = {"original": "eval_questions.json", "holdout": "eval_questions_holdout.json"}
PRE_DECLARED_METHODS = ("vector", "keyword", "hybrid")  # vector_id was added after Run A (D31)


def evaluate_all(
    conn: Any,
    models: Sequence[str],
    embed: Callable[[Sequence[str], str], list[list[float]]],
    k: int = 5,
) -> dict[str, Any]:
    from expense_agent.rag.evaluate import (
        choose_winner,
        load_questions,
        score_rankings,
        top_similarities,
    )
    from expense_agent.rag.search import (
        hybrid_search,
        keyword_search,
        vector_id_search,
        vector_search,
    )

    sets = {name: load_questions(fname) for name, fname in QUESTION_SETS.items()}
    combos: dict[str, dict[str, dict[str, Any]]] = {name: {} for name in sets}
    out_of_scope: dict[str, Any] = {}
    latest_ok = True
    deadline_ok = True
    for i, model in enumerate(models):
        for set_name, questions in sets.items():
            qvecs = dict(
                zip(
                    [q.id for q in questions],
                    embed([q.question for q in questions], model),  # SAME model as the chunks
                    strict=True,
                )
            )
            runs: dict[str, dict[str, list[Any]]] = {"vector": {}, "hybrid": {}, "vector_id": {}}
            if i == 0:
                runs["keyword"] = {}  # model-independent: computed once
            for q in questions:
                v = qvecs[q.id]
                runs["vector"][q.id] = vector_search(conn, v, model, k)
                runs["hybrid"][q.id] = hybrid_search(conn, q.question, v, model, k)
                runs["vector_id"][q.id] = vector_id_search(conn, q.question, v, model, k)
                if "keyword" in runs:
                    runs["keyword"][q.id] = keyword_search(conn, q.question, model, k)
            for method, by_q in runs.items():
                key = "keyword" if method == "keyword" else f"{method}/{model}"
                combos[set_name][key] = score_rankings(
                    questions, {qid: [p.section_id for p in ps] for qid, ps in by_q.items()}
                )
                for ps in by_q.values():
                    latest_ok &= all(p.version == LATEST.version for p in ps)
                text = " ".join(p.text for p in by_q.get("q01", []))
                deadline_ok &= f"{PREVIOUS.deadline_days} days" not in text  # old text never
            if any(not q.in_scope for q in questions):
                top = {qid: (ps[0].score if ps else 0.0) for qid, ps in runs["vector"].items()}
                out_of_scope[model] = top_similarities(questions, lambda q, t=top: t[q.id])
    original = sets["original"]
    n_in_scope = sum(1 for q in original if q.in_scope)
    pre_declared = {
        key: m
        for key, m in combos["original"].items()
        if key.partition("/")[0] in PRE_DECLARED_METHODS
    }
    winner = choose_winner(pre_declared, n_in_scope)
    return {
        "k": k,
        "n_questions": len(original),
        "n_in_scope": n_in_scope,
        "n_holdout": len(sets["holdout"]),
        "combos": combos["original"],
        "holdout_combos": combos["holdout"],
        "out_of_scope_similarity": out_of_scope,
        "latest_only_ok": latest_ok,
        "deadline_uses_latest_text": deadline_ok,
        "winner": winner,
        "winner_model": winner.partition("/")[2] or None,
        "selection_rule": (
            "Pre-declared methods only (vector, keyword, hybrid): Recall@5 (ties within one "
            "question), then MRR (ties within 0.01), then cost keyword<small<large, then "
            "simplicity keyword<vector<hybrid"
        ),
    }


def _table(combos: dict[str, dict[str, Any]]) -> list[str]:
    lines = [
        "| Method / model | Recall@3 | Recall@5 | MRR | Misses@5 |",
        "|---|---|---|---|---|",
    ]
    for key, m in sorted(combos.items(), key=lambda kv: (-kv[1]["recall@5"], kv[0])):
        lines.append(
            f"| {key} | {m['recall@3']:.3f} | {m['recall@5']:.3f} | {m['mrr']:.3f} "
            f"| {', '.join(m['misses@5']) or '-'} |"
        )
    return lines


def results_markdown(results: dict[str, Any]) -> str:
    lines = [f"Original set: {results['n_in_scope']} in-scope of {results['n_questions']}.", ""]
    lines += _table(results["combos"])
    if results.get("holdout_combos"):
        lines += ["", f"Held-out set: {results.get('n_holdout', '?')} questions.", ""]
        lines += _table(results["holdout_combos"])
    lines += ["", f"Winner (pre-declared rule): **{results['winner']}**", ""]
    lines.append(
        f"Latest-only check: {results['latest_only_ok']}; "
        f"deadline question uses latest text: {results['deadline_uses_latest_text']}"
    )
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - cloud entrypoint
    import boto3

    from expense_agent.db.connection import add_profile_argument, connect, make_session
    from expense_agent.rag.azure import Embedder, load_settings

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_profile_argument(parser)
    parser.add_argument("--models", required=True, help="comma-separated embedding deployments")
    parser.add_argument("--evaluate", action="store_true")
    parser.add_argument("--prune-other-models", action="store_true")
    args = parser.parse_args(argv)
    models = [m.strip() for m in args.models.split(",") if m.strip()]

    bucket = os.environ["EXPENSE_ARTIFACT_BUCKET"]
    rid = run_id(datetime.now(UTC), os.environ.get("GIT_SHA", "unknown"))
    print(f"run {rid}; models {models}", flush=True)

    session = make_session(args.profile, os.environ.get("AWS_REGION", "eu-west-2"))
    embedder = Embedder(load_settings(session))
    s3 = boto3.client("s3")

    with connect("ingest", args.profile) as conn:
        docs = render_versions(load_policy_data(conn))
        published = publish_handbooks(s3, bucket, docs)
        register_documents(conn, published)
        print(f"published handbooks: {sorted(published)}; latest {LATEST.version}", flush=True)
        counts = ingest(conn, docs, models, embedder.embed)
        print(f"chunks written: {counts}", flush=True)
        if args.prune_other_models:
            print(f"pruned {prune(conn, models)} chunk rows of other models", flush=True)
        if args.evaluate:
            results = evaluate_all(conn, models, embedder.embed)
            table = results_markdown(results)
            print(table, flush=True)
            print(json.dumps(results["out_of_scope_similarity"], indent=2), flush=True)
            s3.put_object(
                Bucket=bucket,
                Key=f"{EVAL_PREFIX}/{rid}/results.json",
                Body=json.dumps(results, indent=2).encode(),
                ContentType="application/json",
            )
            s3.put_object(
                Bucket=bucket,
                Key=f"{EVAL_PREFIX}/{rid}/results.md",
                Body=table.encode(),
                ContentType="text/markdown",
            )
            print(f"evaluation written to s3://{bucket}/{EVAL_PREFIX}/{rid}/", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
