"""Connection helper with boto3 mocked (no AWS calls)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from expense_agent.db import connection as c


class _NotFound(Exception):
    pass


def fake_session(secrets: dict[str, dict], master_secret: dict | None = None) -> MagicMock:
    rds = MagicMock()
    instance = {"Endpoint": {"Address": "db.example.internal", "Port": 5432}}
    if master_secret is not None:
        instance["MasterUserSecret"] = master_secret
    rds.describe_db_instances.return_value = {"DBInstances": [instance]}

    sm = MagicMock()
    sm.exceptions.ResourceNotFoundException = _NotFound

    def get_secret_value(SecretId: str) -> dict:  # noqa: N803 - boto3 argument name
        if SecretId not in secrets:
            raise _NotFound(SecretId)
        return {"SecretString": json.dumps(secrets[SecretId])}

    sm.get_secret_value.side_effect = get_secret_value
    session = MagicMock()
    session.client.side_effect = lambda name: {"rds": rds, "secretsmanager": sm}[name]
    return session


ARN = "arn:aws:secretsmanager:eu-west-2:000000000000:secret:rds!db-test"
SETTINGS = c.DbSettings()


def test_master_credentials_come_from_master_user_secret() -> None:
    session = fake_session(
        {ARN: {"username": "admin", "password": "fake"}},  # pragma: allowlist secret
        master_secret={"SecretArn": ARN},
    )
    creds = c.get_master_credentials(session, SETTINGS)
    assert (creds.host, creds.port, creds.user, creds.dbname) == (
        "db.example.internal",
        5432,
        "admin",
        "expense",
    )
    session.client("rds").describe_db_instances.assert_called_once_with(
        DBInstanceIdentifier="akinsds1126-expense-db"
    )
    session.client("secretsmanager").get_secret_value.assert_called_once_with(SecretId=ARN)


def test_missing_master_user_secret_is_an_error() -> None:
    with pytest.raises(RuntimeError, match="MasterUserSecret"):
        c.get_master_credentials(fake_session({}), SETTINGS)


def test_app_credentials_per_role() -> None:
    payload = {
        "host": "h",
        "port": 5432,
        "dbname": "expense",
        "app_readonly": {"username": "app_readonly", "password": "r"},
        "app_writer": {"username": "app_writer", "password": "w"},
    }
    session = fake_session({SETTINGS.app_secret_id: payload})
    assert c.get_app_credentials(session, SETTINGS, "readonly").user == "app_readonly"
    assert c.get_app_credentials(session, SETTINGS, "writer").password == "w"


def test_missing_app_secret_raises_clear_error() -> None:
    with pytest.raises(c.SecretNotFoundError):
        c.get_app_credentials(fake_session({}), SETTINGS, "readonly")


def test_connect_kwargs_enforce_verify_full() -> None:
    creds = c.DbCredentials("h", 5432, "expense", "u", "p")
    kwargs = c.connect_kwargs(creds, SETTINGS)
    assert kwargs["sslmode"] == "verify-full"
    assert Path(kwargs["sslrootcert"]) == c.DEFAULT_CA_BUNDLE
    assert kwargs["password"] == "p"


def test_missing_ca_bundle_fails(tmp_path: Path) -> None:
    settings = c.DbSettings(ca_bundle=tmp_path / "missing.pem")
    with pytest.raises(FileNotFoundError):
        c.connect_kwargs(c.DbCredentials("h", 5432, "expense", "u", "p"), settings)


def test_repr_masks_password() -> None:
    creds = c.DbCredentials("h", 5432, "expense", "u", "super-secret-value")
    assert "super-secret-value" not in repr(creds)
    assert "super-secret-value" not in str(creds)


def test_settings_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPENSE_DB_NAME", "other")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    s = c.DbSettings.from_env()
    assert (s.dbname, s.region, s.instance_id) == ("other", "eu-west-1", "akinsds1126-expense-db")


def test_secret_for_respects_custom_secret_ids() -> None:
    s = c.DbSettings(ml_secret_id="custom-ml", app_secret_id="custom-app")
    assert s.secret_for("ml") == "custom-ml"
    assert s.secret_for("readonly") == s.secret_for("writer") == "custom-app"
    assert s.roles_by_secret() == {
        "custom-app": ("app_readonly", "app_writer"),
        "custom-ml": ("app_ml",),
        "akinsds1126-expense-db-ingest": ("app_ingest",),
    }


FAKE_VALUE = "fake"  # not a real credential


def test_app_credentials_read_from_custom_ml_secret() -> None:
    name = "custom-ml"
    s = c.DbSettings(ml_secret_id=name)
    payload = {
        "host": "h",
        "port": 5432,
        "dbname": "expense",
        "app_ml": {"username": "app_ml", "password": FAKE_VALUE},
    }
    session = fake_session({name: payload})
    assert c.get_app_credentials(session, s, "ml").user == "app_ml"
    session.client("secretsmanager").get_secret_value.assert_called_once_with(SecretId=name)


def test_secret_ids_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EXPENSE_DB_ML_SECRET_ID", "env-ml")
    assert c.DbSettings.from_env().secret_for("ml") == "env-ml"
