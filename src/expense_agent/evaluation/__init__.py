"""Phase 8 evaluation data: labelled conversations (see conversations.json)."""

from __future__ import annotations

import json
from importlib import resources
from typing import Any


def load_conversations() -> list[dict[str, Any]]:
    text = resources.files(__package__).joinpath("conversations.json").read_text("utf-8")
    return list(json.loads(text)["cases"])
