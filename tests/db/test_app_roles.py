"""app_roles: secret payload and ordering (secret created BEFORE passwords are set)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from expense_agent.db import app_roles
from expense_agent.db.connection import DbCredentials, SecretNotFoundError


def test_payload_has_both_roles_with_strong_distinct_passwords() -> None:
    payload = app_roles.new_secret_payload("h", 5432, "expense")
    assert (payload["host"], payload["port"], payload["dbname"]) == ("h", 5432, "expense")
    ro, rw = payload["app_readonly"], payload["app_writer"]
    assert (ro["username"], rw["username"]) == ("app_readonly", "app_writer")
    assert ro["password"] != rw["password"]
    assert min(len(ro["password"]), len(rw["password"])) >= 40


def test_alter_role_quotes_password_safely() -> None:
    conn = MagicMock()
    app_roles.alter_role_password(conn, "app_writer", "it's-a-test")
    composed = conn.execute.call_args.args[0]
    rendered = composed.as_string(None)
    assert rendered == "ALTER ROLE \"app_writer\" WITH LOGIN PASSWORD 'it''s-a-test'"


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    events: list[str] = []
    session = MagicMock()
    sm = session.client.return_value
    sm.create_secret.side_effect = lambda **kw: events.append("create_secret")
    monkeypatch.setattr(app_roles, "make_session", lambda profile, region: session)
    monkeypatch.setattr(
        app_roles,
        "get_master_credentials",
        lambda s, settings: DbCredentials("h", 5432, "expense", "admin", "x"),
    )
    monkeypatch.setattr(app_roles, "connect_kwargs", lambda creds, settings: {})
    monkeypatch.setattr(
        app_roles,
        "alter_role_password",
        lambda conn, role, pw: events.append(f"alter:{role}"),
    )
    import psycopg

    conn = MagicMock()
    conn.__enter__.return_value = conn
    monkeypatch.setattr(psycopg, "connect", lambda **kw: conn)
    return {"events": events, "sm": sm, "monkeypatch": monkeypatch}


def _missing(*args: Any) -> Any:
    raise SecretNotFoundError("missing")


def test_missing_secret_without_flag_changes_nothing(wired: dict[str, Any]) -> None:
    wired["monkeypatch"].setattr(app_roles, "get_secret_json", _missing)
    assert app_roles.main(["--profile", "x"]) == 1
    assert wired["events"] == []


def test_create_secret_happens_before_alter_role(wired: dict[str, Any]) -> None:
    wired["monkeypatch"].setattr(app_roles, "get_secret_json", _missing)
    assert app_roles.main(["--profile", "x", "--create-secret"]) == 0
    assert wired["events"] == [
        "create_secret",
        "create_secret",
        "create_secret",
        "alter:app_readonly",
        "alter:app_writer",
        "alter:app_ml",
        "alter:app_ingest",
    ]
    created = {c.kwargs["Name"]: c.kwargs for c in wired["sm"].create_secret.call_args_list}
    assert set(created) == {
        "akinsds1126-expense-db-app",
        "akinsds1126-expense-db-ml",
        "akinsds1126-expense-db-ingest",
    }
    for kwargs in created.values():
        assert {"Key": "Project", "Value": "akinsds1126-expense"} in kwargs["Tags"]


def test_ml_secret_holds_only_app_ml(wired: dict[str, Any]) -> None:
    import json

    wired["monkeypatch"].setattr(app_roles, "get_secret_json", _missing)
    app_roles.main(["--profile", "x", "--create-secret"])
    created = {c.kwargs["Name"]: c.kwargs for c in wired["sm"].create_secret.call_args_list}
    ml = json.loads(created["akinsds1126-expense-db-ml"]["SecretString"])
    app = json.loads(created["akinsds1126-expense-db-app"]["SecretString"])
    assert "app_ml" in ml and "app_writer" not in ml and "app_readonly" not in ml
    assert "app_ml" not in app


def test_only_missing_secret_is_created(wired: dict[str, Any]) -> None:
    fake = "keep"  # fake value for the test
    app = {
        "app_readonly": {"username": "app_readonly", "password": fake},
        "app_writer": {"username": "app_writer", "password": fake},
    }

    def lookup(session: Any, secret_id: str) -> Any:
        if secret_id.endswith("-ml"):
            raise SecretNotFoundError(secret_id)
        if secret_id.endswith("-ingest"):
            return {"app_ingest": {"username": "app_ingest", "password": fake}}
        return app

    wired["monkeypatch"].setattr(app_roles, "get_secret_json", lookup)
    assert app_roles.main(["--profile", "x", "--create-secret"]) == 0
    names = [c.kwargs["Name"] for c in wired["sm"].create_secret.call_args_list]
    assert names == ["akinsds1126-expense-db-ml"]


def test_existing_secrets_are_reused_not_rotated(wired: dict[str, Any]) -> None:
    ro_value, rw_value, ml_value = "keep-ro", "keep-rw", "keep-ml"  # fake values for the test
    secrets = {
        "akinsds1126-expense-db-app": {
            "app_readonly": {"username": "app_readonly", "password": ro_value},
            "app_writer": {"username": "app_writer", "password": rw_value},
        },
        "akinsds1126-expense-db-ml": {"app_ml": {"username": "app_ml", "password": ml_value}},
        "akinsds1126-expense-db-ingest": {
            "app_ingest": {"username": "app_ingest", "password": ml_value}
        },
    }
    seen: list[str] = []
    wired["monkeypatch"].setattr(app_roles, "get_secret_json", lambda s, sid: secrets[sid])
    wired["monkeypatch"].setattr(
        app_roles, "alter_role_password", lambda conn, role, pw: seen.append(pw)
    )
    assert app_roles.main(["--profile", "x", "--create-secret"]) == 0
    wired["sm"].create_secret.assert_not_called()
    assert seen == ["keep-ro", "keep-rw", "keep-ml", "keep-ml"]
