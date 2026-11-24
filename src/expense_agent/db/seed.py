"""Load the synthetic dataset into RDS (one transaction, parameterised inserts only).

    uv run python -m expense_agent.db.seed --profile aero [--seed 42] [--reset]

Refuses to run if claims already exist, unless --reset is given (TRUNCATEs the synthetic data
tables, not schema_migrations, in the same transaction).
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import Any

from psycopg.types.json import Jsonb

from expense_agent.db.connection import add_profile_argument, connect
from expense_agent.db.synthetic import DEFAULT_SEED, Dataset, generate

DATA_TABLES = (
    "claim_labels",
    "approvals",
    "claims",
    "audit_log",
    "policy_limits",
    "expense_categories",
    "employees",
    "departments",
)

# (table, identity column) pairs whose sequences must move past the explicit seeded IDs.
IDENTITY_COLUMNS = (
    ("departments", "department_id"),
    ("employees", "employee_id"),
    ("claims", "claim_id"),
    ("approvals", "approval_id"),
    ("audit_log", "audit_id"),
)

# Fixed statements: table and column names are constants, values are always bind parameters.
INSERT_DEPARTMENT = (
    "INSERT INTO departments (department_id, code, name, cost_centre) VALUES (%s, %s, %s, %s)"
)
INSERT_CATEGORY = (
    "INSERT INTO expense_categories"
    " (category_code, name, description, is_travel, receipt_required_over)"
    " VALUES (%s, %s, %s, %s, %s)"
)
INSERT_LIMIT = (
    "INSERT INTO policy_limits (category_code, grade, per_claim_limit, monthly_limit)"
    " VALUES (%s, %s, %s, %s)"
)
INSERT_EMPLOYEE = (
    "INSERT INTO employees (employee_id, employee_code, full_name, email, department_id,"
    " grade, manager_id, home_currency, hired_on) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
INSERT_CLAIM = (
    "INSERT INTO claims (claim_id, claim_ref, employee_id, category_code, amount, currency,"
    " incurred_at, submitted_at, merchant, description, status, created_at, updated_at)"
    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
)
INSERT_APPROVAL = (
    "INSERT INTO approvals (claim_id, decider_type, decided_by, decision, reason,"
    " requested_at, decided_at) VALUES (%s, %s, %s, %s, %s, %s, %s)"
)
INSERT_LABEL = (
    "INSERT INTO claim_labels (claim_id, anomaly_type, related_claim_id, notes)"
    " VALUES (%s, %s, %s, %s)"
)
INSERT_AUDIT = (
    "INSERT INTO audit_log (actor_type, action, entity_type, entity_id, details)"
    " VALUES (%s, %s, %s, %s, %s)"
)


def rows_for(ds: Dataset) -> dict[str, list[tuple[Any, ...]]]:
    """Turn the dataset into parameter tuples, in foreign-key order."""
    return {
        INSERT_DEPARTMENT: [
            (d.department_id, d.code, d.name, d.cost_centre) for d in ds.departments
        ],
        INSERT_CATEGORY: [
            (c.code, c.name, c.description, c.is_travel, c.receipt_required_over)
            for c in ds.categories
        ],
        INSERT_LIMIT: [
            (p.category_code, p.grade, p.per_claim_limit, p.monthly_limit) for p in ds.policy_limits
        ],
        INSERT_EMPLOYEE: [
            (
                e.employee_id,
                e.employee_code,
                e.full_name,
                e.email,
                e.department_id,
                e.grade,
                e.manager_id,
                e.home_currency,
                e.hired_on,
            )
            for e in ds.employees  # ordered so managers come before their reports
        ],
        INSERT_CLAIM: [
            (
                c.claim_id,
                c.claim_ref,
                c.employee_id,
                c.category_code,
                c.amount,
                c.currency,
                c.incurred_at,
                c.submitted_at,
                c.merchant,
                c.description,
                c.status,
                c.submitted_at,
                c.submitted_at,
            )
            for c in ds.claims
        ],
        INSERT_APPROVAL: [
            (
                a.claim_id,
                a.decider_type,
                a.decided_by,
                a.decision,
                a.reason,
                a.requested_at,
                a.decided_at,
            )
            for a in ds.approvals
        ],
        INSERT_LABEL: [
            (lb.claim_id, lb.anomaly_type, lb.related_claim_id, lb.notes) for lb in ds.labels
        ],
    }


def seed(conn: Any, ds: Dataset, *, reset: bool) -> dict[str, int]:
    from psycopg import sql

    with conn.transaction():
        existing = conn.execute("SELECT count(*) FROM claims").fetchone()[0]
        if existing and not reset:
            raise SystemExit(f"claims already has {existing} rows; re-run with --reset to reload")
        if reset:
            conn.execute(
                sql.SQL("TRUNCATE {} RESTART IDENTITY CASCADE").format(
                    sql.SQL(", ").join(sql.Identifier(t) for t in DATA_TABLES)
                )
            )
        counts: dict[str, int] = {}
        with conn.cursor() as cur:
            for statement, rows in rows_for(ds).items():
                cur.executemany(statement, rows)
                counts[statement.split()[2]] = len(rows)
            for table, column in IDENTITY_COLUMNS:
                cur.execute(
                    sql.SQL(
                        "SELECT setval(pg_get_serial_sequence({t}, {c}),"
                        " COALESCE((SELECT max({col}) FROM {tbl}), 0) + 1, false)"
                    ).format(
                        t=sql.Literal(table),
                        c=sql.Literal(column),
                        col=sql.Identifier(column),
                        tbl=sql.Identifier(table),
                    )
                )
            cur.execute(
                INSERT_AUDIT,
                (
                    "system",
                    "seed_synthetic_data",
                    "database",
                    None,
                    Jsonb({"seed": ds.seed, "reset": reset, **counts}),
                ),
            )
    return counts


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_profile_argument(parser)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--reset", action="store_true", help="truncate synthetic data first")
    args = parser.parse_args(argv)

    ds = generate(args.seed)
    with connect("master", args.profile) as conn:
        counts = seed(conn, ds, reset=args.reset)
    for table, n in counts.items():
        print(f"{table:<20} {n:>6}")
    print(f"seeded with seed={args.seed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
