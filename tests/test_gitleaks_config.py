"""The gitleaks allowlist only lets our secret NAMES through, never values (D23)."""

from __future__ import annotations

import re
import secrets
import string
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG = tomllib.loads((ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))
SECRET_NAMES = [
    "akinsds1126-expense-db-app",
    "akinsds1126-expense-db-ml",
    "akinsds1126-expense-db-ingest",
    "akinsds1126-expense-azure-openai",
    "akinsds1126-expense-mcp-internal",
]


def _regexes() -> list[re.Pattern[str]]:
    return [re.compile(r) for a in CONFIG["allowlists"] for r in a["regexes"]]


def test_extends_default_rules() -> None:
    assert CONFIG["extend"]["useDefault"] is True
    assert "rules" not in CONFIG  # no rule is redefined or weakened


def test_allowlists_target_secret_values_only() -> None:
    assert CONFIG["allowlists"]
    for allow in CONFIG["allowlists"]:
        assert allow["regexTarget"] == "secret"
        assert set(allow) <= {"description", "regexTarget", "regexes"}  # no paths/commits/rules
        for r in allow["regexes"]:
            assert r.startswith("^") and r.endswith("$")
    assert "allowlist" not in CONFIG


def test_secret_names_are_allowed() -> None:
    for name in SECRET_NAMES:
        assert any(r.fullmatch(name) for r in _regexes()), name


def test_key_like_values_are_not_allowed() -> None:
    alphabet = string.ascii_letters + string.digits
    random_key = "".join(secrets.choice(alphabet) for _ in range(40))
    for value in (
        random_key,
        "akinsds1126-expense-Ab3xK9pQ2mZ7",  # mixed case
        "akinsds1126-expense-db-app " + random_key,  # key appended after a name
        "sk-" + random_key,
    ):
        assert not any(r.search(value) for r in _regexes()), value
