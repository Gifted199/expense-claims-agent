"""Small pure helpers for the UI (importable without starting Streamlit)."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

LONDON = ZoneInfo("Europe/London")

_MARKDOWN_CHARS = re.compile(r"[\[\]()!<>`*_#|\\]")


def plain(text: Any) -> str:
    """Strip markdown/HTML control characters from user-typed text shown in labels."""
    return _MARKDOWN_CHARS.sub("", str(text or ""))


def prefill_message(s3_key: str, ocr: dict[str, Any]) -> str:
    """The chat message for "Create a claim from this receipt": only the fields OCR found."""
    found = [
        f"vendor {ocr['vendor']}" if ocr.get("vendor") else "",
        f"date {ocr['date']}" if ocr.get("date") else "",
        f"total £{ocr['total']}" if ocr.get("total") else "",
    ]
    found = [f for f in found if f]
    details = (": " + ", ".join(found)) if found else ""
    return f"Please submit a claim from my receipt {s3_key}{details}. Ask me for anything missing."


def friendly_date(value: Any) -> str:
    """ISO timestamp -> "26 Oct 2026, 14:26" in London time ("—" if missing or unreadable)."""
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return str(value)
    local = parsed.astimezone(LONDON) if parsed.tzinfo else parsed
    return f"{local.day} {local:%b %Y, %H:%M}"
