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
    assert wired["events"] == ["create_secret", "alter:app_readonly", "alter:app_writer"]
    kwargs = wired["sm"].create_secret.call_args.kwargs
    assert kwargs["Name"] == "akinsds1126-expense-db-app"
    assert {"Key": "Project", "Value": "akinsds1126-expense"} in kwargs["Tags"]


def test_existing_secret_is_reused_not_rotated(wired: dict[str, Any]) -> None:
    ro_value, rw_value = "keep-ro", "keep-rw"  # fake values for the test
    existing = {
        "app_readonly": {"username": "app_readonly", "password": ro_value},
        "app_writer": {"username": "app_writer", "password": rw_value},
    }
    seen: list[str] = []
    wired["monkeypatch"].setattr(app_roles, "get_secret_json", lambda s, sid: existing)
    wired["monkeypatch"].setattr(
        app_roles, "alter_role_password", lambda conn, role, pw: seen.append(pw)
    )
    assert app_roles.main(["--profile", "x", "--create-secret"]) == 0
    wired["sm"].create_secret.assert_not_called()
    assert seen == ["keep-ro", "keep-rw"]
