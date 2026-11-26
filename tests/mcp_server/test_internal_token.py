"""The token secret is created once, never overwritten, and its value never printed."""

from __future__ import annotations

import json
from typing import Any

import pytest

from expense_agent.mcp_server import internal_token
from expense_agent.mcp_server.auth import MIN_TOKEN_LENGTH, TOKEN_KEY


class NotFound(Exception):
    pass


class FakeSM:
    exceptions = type("Exceptions", (), {"ResourceNotFoundException": NotFound})

    def __init__(self, exists: bool) -> None:
        self.exists = exists
        self.created: dict[str, Any] = {}

    def describe_secret(self, SecretId: str) -> dict:  # noqa: N803
        if not self.exists:
            raise NotFound(SecretId)
        return {"Name": SecretId}

    def create_secret(self, **kwargs: Any) -> dict:
        self.created = kwargs
        return {"ARN": "arn:aws:secretsmanager:eu-west-2:1:secret:x"}


def test_creates_a_long_random_token() -> None:
    sm = FakeSM(exists=False)
    arn = internal_token.create_secret(sm, "name")
    token = json.loads(sm.created["SecretString"])[TOKEN_KEY]
    assert len(token) >= MIN_TOKEN_LENGTH and token not in arn
    assert internal_token.new_token() != internal_token.new_token()


def test_refuses_to_overwrite() -> None:
    sm = FakeSM(exists=True)
    with pytest.raises(SystemExit, match="already exists"):
        internal_token.create_secret(sm, "name")
    assert sm.created == {}
