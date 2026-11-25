"""Print row counts, anomaly rate by type and sample rows, and check role privileges.

    uv run python -m expense_agent.db.verify --profile aero

Counts and samples are read as app_readonly (which also proves that login works); anomaly
labels are read as master, because no app role may see them. Exits non-zero if a check fails.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Any

from psycopg import sql

from expense_agent.db.connection import add_profile_argument, connect

COUNT_TABLES = (
    "departments",
    "employees",
    "expense_categories",
    "policy_limits",
    "claims",
    "approvals",
)

# (role, table, privilege, expected)
PRIVILEGE_MATRIX: tuple[tuple[str, str, str, bool], ...] = (
    ("app_readonly", "claims", "SELECT", True),
    ("app_readonly", "claims", "INSERT", False),
    ("app_readonly", "claims", "UPDATE", False),
    ("app_readonly", "claim_labels", "SELECT", False),
    ("app_readonly", "audit_log", "SELECT", False),
    ("app_readonly", "schema_migrations", "SELECT", False),
    ("app_writer", "claims", "INSERT", True),
    ("app_writer", "claims", "UPDATE", False),  # table-wide UPDATE revoked (005); columns below
    ("app_writer", "claims", "DELETE", False),
    ("app_writer", "approvals", "INSERT", True),
    ("app_writer", "approvals", "UPDATE", False),  # decisions are append-only (D10)
    ("app_writer", "approvals", "DELETE", False),
    ("app_writer", "audit_log", "INSERT", True),
    ("app_writer", "audit_log", "UPDATE", False),
    ("app_writer", "audit_log", "DELETE", False),
    ("app_writer", "claim_labels", "SELECT", False),
    ("app_writer", "policy_limits", "UPDATE", False),
    ("app_writer", "schema_migrations", "SELECT", False),
    ("app_ml", "claims", "SELECT", True),
    ("app_ml", "claim_labels", "SELECT", True),  # evaluation only (D16)
    ("app_ml", "policy_limits", "SELECT", True),
    ("app_ml", "approvals", "SELECT", False),
    ("app_ml", "audit_log", "SELECT", False),
    ("app_ml", "claims", "INSERT", False),
    ("app_ml", "claims", "UPDATE", False),
    ("app_ml", "claims", "DELETE", False),
    ("app_ml", "claim_labels", "UPDATE", False),
    ("app_ml", "employees", "SELECT", False),  # table-wide revoked in 007; columns below
    # RAG (008): only app_ingest writes policy_*; it can't see business data (D28)
    ("app_ingest", "policy_limits", "SELECT", True),
    ("app_ingest", "policy_city_hotel_caps", "SELECT", True),
    ("app_ingest", "policy_chunks", "INSERT", True),
    ("app_ingest", "policy_chunks", "DELETE", True),
    ("app_ingest", "policy_documents", "UPDATE", True),
    ("app_ingest", "claims", "SELECT", False),
    ("app_ingest", "employees", "SELECT", False),
    ("app_ingest", "claim_labels", "SELECT", False),
    ("app_ingest", "policy_limits", "UPDATE", False),
    ("app_readonly", "policy_chunks", "SELECT", True),
    ("app_readonly", "policy_city_hotel_caps", "SELECT", True),
    ("app_readonly", "policy_chunks", "INSERT", False),
    ("app_readonly", "policy_documents", "DELETE", False),
    ("app_writer", "policy_city_hotel_caps", "SELECT", True),
    ("app_writer", "policy_chunks", "INSERT", False),
    ("app_ml", "policy_chunks", "SELECT", False),
)

# (role, table, column, privilege, expected): app_writer may only move claims through the workflow.
COLUMN_PRIVILEGES: tuple[tuple[str, str, str, str, bool], ...] = (
    ("app_writer", "claims", "status", "UPDATE", True),
    ("app_writer", "claims", "anomaly_score", "UPDATE", True),
    ("app_writer", "claims", "receipt_s3_key", "UPDATE", True),
    ("app_writer", "claims", "amount", "UPDATE", False),
    ("app_writer", "claims", "employee_id", "UPDATE", False),
    ("app_writer", "claims", "incurred_at", "UPDATE", False),
    # app_ml reads only the employee columns the model needs (007, D22)
    ("app_ml", "employees", "employee_id", "SELECT", True),
    ("app_ml", "employees", "grade", "SELECT", True),
    ("app_ml", "employees", "full_name", "SELECT", False),
    ("app_ml", "employees", "email", "SELECT", False),
)

# A claim counts as over limit if it exceeds the per-claim limit, or if the running total of
# non-rejected claims (same employee, category, calendar month, in submission order, including
# this one) exceeds the monthly limit. None of those may have a system approval.
OVER_LIMIT_AUTO_APPROVED = """
WITH running AS (
    SELECT c.claim_id, c.amount, p.per_claim_limit, p.monthly_limit,
           COALESCE(sum(CASE WHEN c.status <> 'rejected' THEN c.amount END) OVER (
               PARTITION BY c.employee_id, c.category_code,
                            date_trunc('month', c.incurred_at AT TIME ZONE 'Europe/London')
               ORDER BY c.claim_id
               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING), 0) + c.amount AS month_total
    FROM claims c
    JOIN employees e USING (employee_id)
    JOIN policy_limits p ON p.category_code = c.category_code AND p.grade = e.grade
)
SELECT count(*) FROM running r
JOIN approvals a ON a.claim_id = r.claim_id AND a.decider_type = 'system'
WHERE r.amount > r.per_claim_limit OR r.month_total > r.monthly_limit
"""


def print_table(title: str, header: Sequence[str], rows: Sequence[Sequence[Any]]) -> None:
    print(f"\n== {title}")
    widths = [max(len(str(x)) for x in col) for col in zip(header, *rows, strict=False)]
    print("  ".join(str(h).ljust(w) for h, w in zip(header, widths, strict=True)))
    for row in rows:
        print("  ".join(str(v).ljust(w) for v, w in zip(row, widths, strict=True)))


def readonly_report(conn: Any) -> None:
    rows = [
        (
            t,
            conn.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(t))).fetchone()[
                0
            ],
        )
        for t in COUNT_TABLES
    ]
    print_table("Row counts (as app_readonly)", ("table", "rows"), rows)
    print_table(
        "Claims by status",
        ("status", "claims", "total_gbp"),
        conn.execute(
            "SELECT status, count(*), sum(amount) FROM claims GROUP BY status ORDER BY 2 DESC"
        ).fetchall(),
    )
    print_table(
        "Claims per month",
        ("month", "claims", "total_gbp"),
        conn.execute(
            "SELECT to_char(incurred_at AT TIME ZONE 'Europe/London', 'YYYY-MM'), count(*),"
            " sum(amount) FROM claims GROUP BY 1 ORDER BY 1"
        ).fetchall(),
    )
    print_table(
        "Sample claims",
        ("ref", "employee", "category", "amount", "merchant", "status"),
        conn.execute(
            "SELECT c.claim_ref, e.full_name, c.category_code, c.amount, c.merchant, c.status"
            " FROM claims c JOIN employees e USING (employee_id)"
            " ORDER BY c.claim_id LIMIT 5"
        ).fetchall(),
    )


def master_report(conn: Any) -> bool:
    total = conn.execute("SELECT count(*) FROM claims").fetchone()[0]
    rows = conn.execute(
        "SELECT anomaly_type, count(*), round(100.0 * count(*) / %s, 2)"
        " FROM claim_labels GROUP BY 1 ORDER BY 2 DESC",
        (total,),
    ).fetchall()
    labelled = sum(r[1] for r in rows)
    print_table("Anomalies by type (eval-only labels, as master)", ("type", "claims", "pct"), rows)
    rate = 100.0 * labelled / total if total else 0.0
    print(f"anomaly rate: {labelled}/{total} = {rate:.2f}%")

    print("\n== Security checks")
    results = security_checks(conn)
    for description, passed in results:
        print(f"  [{'PASS' if passed else 'FAIL'}] {description}")
    return all(passed for _, passed in results)


def security_checks(conn: Any) -> list[tuple[str, bool]]:
    """Privilege matrix, column grants, pgvector, and the never-auto-approve-over-limit rule."""
    results: list[tuple[str, bool]] = []
    for role, table, priv, expected in PRIVILEGE_MATRIX:
        actual = conn.execute(
            "SELECT has_table_privilege(%s, %s, %s)", (role, table, priv)
        ).fetchone()[0]
        verb = "can" if expected else "cannot"
        results.append((f"{role} {verb} {priv} {table}", actual == expected))
    for role, table, column, priv, expected in COLUMN_PRIVILEGES:
        actual = conn.execute(
            "SELECT has_column_privilege(%s, %s, %s, %s)", (role, table, column, priv)
        ).fetchone()[0]
        verb = "can" if expected else "cannot"
        results.append((f"{role} {verb} {priv} {table}.{column}", actual == expected))
    has_vector = conn.execute("SELECT count(*) FROM pg_extension WHERE extname = 'vector'")
    results.append(("pgvector extension installed", bool(has_vector.fetchone()[0])))
    latest = conn.execute("SELECT count(*) FROM policy_documents WHERE is_latest").fetchone()[0]
    results.append((f"at most one latest policy version ({latest})", latest <= 1))
    over_auto = conn.execute(OVER_LIMIT_AUTO_APPROVED).fetchone()[0]
    results.append(
        (
            f"no over-limit claim (per-claim or monthly) was auto-approved ({over_auto})",
            over_auto == 0,
        )
    )
    return results


def login_checks(profile: str) -> bool:
    ok = True
    for role, expected in (("writer", "app_writer"), ("ml", "app_ml"), ("ingest", "app_ingest")):
        with connect(role, profile) as conn:  # type: ignore[arg-type]
            user = conn.execute("SELECT current_user").fetchone()[0]
        passed = user == expected
        ok &= passed
        print(f"  [{'PASS' if passed else 'FAIL'}] {expected} can log in")
    return ok


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_profile_argument(parser)
    args = parser.parse_args(argv)

    with connect("readonly", args.profile) as conn:
        readonly_report(conn)
    with connect("master", args.profile) as conn:
        ok = master_report(conn)
    ok &= login_checks(args.profile)
    print("\nALL CHECKS PASSED" if ok else "\nSOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
