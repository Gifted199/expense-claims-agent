"""Set passwords for the app roles from their Secrets Manager secrets.

    uv run python -m expense_agent.db.app_roles --profile aero [--create-secret]

Secrets and the roles they hold (see DbSettings.roles_by_secret):
- ``akinsds1126-expense-db-app``: app_readonly, app_writer
- ``akinsds1126-expense-db-ml``: app_ml (separate, so the trainer can't read the other passwords)

Existing secrets are reused (no surprise rotation). A missing secret stops the script unless
``--create-secret`` is given; then strong random passwords are generated, the secret is created
FIRST, and only then are the role passwords set. Passwords are never printed or written to disk.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from collections.abc import Sequence
from typing import Any

from expense_agent.db.connection import (
    DbSettings,
    SecretNotFoundError,
    add_profile_argument,
    connect_kwargs,
    get_master_credentials,
    get_secret_json,
    make_session,
)

TAGS = [
    {"Key": "Project", "Value": "akinsds1126-expense"},
    {"Key": "ManagedBy", "Value": "expense_agent.db.app_roles"},
]


def new_secret_payload(
    host: str, port: int, dbname: str, roles: Sequence[str] | None = None
) -> dict[str, Any]:
    roles = tuple(roles) if roles is not None else ("app_readonly", "app_writer")
    return {
        "host": host,
        "port": port,
        "dbname": dbname,
        **{name: {"username": name, "password": secrets.token_urlsafe(32)} for name in roles},
    }


def alter_role_password(conn: Any, role: str, password: str) -> None:
    """DDL can't take bind parameters, so psycopg composes the statement with safe quoting."""
    from psycopg import sql

    conn.execute(
        sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(
            sql.Identifier(role), sql.Literal(password)
        )
    )


def main(argv: Sequence[str] | None = None) -> int:
    import psycopg

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    add_profile_argument(parser)
    parser.add_argument(
        "--create-secret", action="store_true", help="create any role secret that does not exist"
    )
    args = parser.parse_args(argv)

    settings = DbSettings.from_env()
    session = make_session(args.profile, settings.region)
    master = get_master_credentials(session, settings)

    # Resolve every secret first, so nothing is changed if one is missing without the flag.
    payloads: dict[str, dict[str, Any]] = {}
    missing: list[str] = []
    for secret_id in settings.roles_by_secret():
        try:
            payloads[secret_id] = get_secret_json(session, secret_id)
            print(f"using existing secret {secret_id}")
        except SecretNotFoundError:
            missing.append(secret_id)
    if missing and not args.create_secret:
        print(f"secret(s) {missing} do not exist; re-run with --create-secret", file=sys.stderr)
        return 1
    for secret_id in missing:
        roles = settings.roles_by_secret()[secret_id]
        payload = new_secret_payload(master.host, master.port, settings.dbname, roles)
        session.client("secretsmanager").create_secret(
            Name=secret_id,
            Description=f"Expense agent DB roles ({', '.join(roles)})",
            SecretString=json.dumps(payload),
            Tags=TAGS,
        )
        payloads[secret_id] = payload
        print(f"created secret {secret_id}")

    with psycopg.connect(**connect_kwargs(master, settings), autocommit=True) as conn:
        for secret_id, roles in settings.roles_by_secret().items():
            for role in roles:
                alter_role_password(conn, role, payloads[secret_id][role]["password"])
                print(f"password set for {role}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
