"""The Streamlit UI renders with a mocked agent API (no network)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest  # noqa: E402

from expense_agent.ui import client as ui_client  # noqa: E402

APP = Path(__file__).resolve().parents[2] / "src" / "expense_agent" / "ui" / "app.py"
PEOPLE = [
    {
        "employee_id": 1,
        "employee_code": "E0001",
        "full_name": "Zane Ellison",
        "grade": "director",
        "is_manager": True,
    },
    {
        "employee_id": 6,
        "employee_code": "E0006",
        "full_name": "Jonah Quarry",
        "grade": "senior",
        "is_manager": False,
    },
]


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(ui_client.ApiClient, "employees", lambda self: PEOPLE)
    monkeypatch.setattr(ui_client.ApiClient, "claims", lambda self, status=None: {"claims": []})
    monkeypatch.setattr(ui_client.ApiClient, "queue", lambda self: {"claims": []})

    def chat(self: ui_client.ApiClient, thread: str, message: str) -> dict:
        calls.append(("chat", self.employee_id, message))
        return {"reply": "The London cap is £230.00 (EXP-6.3).", "tools_used": ["search_policy"]}

    monkeypatch.setattr(ui_client.ApiClient, "chat", chat)
    return calls


def test_welcome_shows_capability_cards_and_examples(fake_api: list) -> None:
    at = AppTest.from_file(str(APP), default_timeout=30).run()
    assert not at.exception
    html = " ".join(m.value for m in at.markdown)
    for title in (
        "Ask about policy",
        "Check my spending",
        "Submit a claim",
        "Scan a receipt",
        "Track my claims",
    ):
        assert title in html
    assert [t.label for t in at.tabs] == [
        "Assistant",
        "My claims",
        "Manager queue",
    ]  # E0001 manages


def test_example_question_is_sent_to_the_chat_as_the_signed_in_user(fake_api: list) -> None:
    at = AppTest.from_file(str(APP), default_timeout=30).run()
    at.button(key="ex0").click().run()
    assert fake_api == [("chat", 1, "What's the hotel cap in London for my grade?")]
    assert any("EXP-6.3" in m.value for m in at.markdown)


def test_non_manager_has_no_queue_tab(fake_api: list) -> None:
    at = AppTest.from_file(str(APP), default_timeout=30).run()
    at.sidebar.selectbox[0].set_value(6).run()
    assert [t.label for t in at.tabs] == ["Assistant", "My claims"]


def test_receipt_prefill_mentions_only_what_ocr_found() -> None:
    from expense_agent.ui.helpers import plain, prefill_message

    full = {"vendor": "CityCab", "date": "2026-11-24", "total": "24.00"}
    msg = prefill_message("uploads/E0006/x.png", full)
    assert "vendor CityCab, date 2026-11-24, total £24.00" in msg
    msg = prefill_message("uploads/E0006/x.png", {"vendor": "CityCab", "date": None, "total": None})
    assert "None" not in msg and "£" not in msg and "vendor CityCab" in msg
    assert plain("![x](http://evil/img.png) Cafe") == "xhttp://evil/img.png Cafe"


def _assistant_parts(at: AppTest) -> tuple[Any, Any]:
    children = list(at.tabs[0].children.values())
    return children[0], children[1]


def test_assistant_tab_is_a_chat_layout_with_the_input_below_the_messages(fake_api: list) -> None:
    at = AppTest.from_file(str(APP), default_timeout=30).run()
    box, below = _assistant_parts(at)
    assert below.type == "chat_input"  # the input comes after (below) the message area
    proto = box.proto
    assert proto.height_config.pixel_height == 520 and proto.autoscroll
    assert sum("cap-card" in m.value for m in box.markdown) == 5  # welcome inside the box
    assert box.button(key="ex0") is not None


def test_messages_stay_in_order_oldest_first_inside_the_box(fake_api: list) -> None:
    at = AppTest.from_file(str(APP), default_timeout=30).run()
    at.chat_input[0].set_value("first question").run()
    at.chat_input[0].set_value("second question").run()
    box, below = _assistant_parts(at)
    roles = [m.name for m in box.chat_message]
    assert roles == ["user", "assistant", "user", "assistant"]
    assert box.chat_message[0].markdown[0].value == "first question"
    assert box.chat_message[2].markdown[0].value == "second question"
    assert not any("cap-card" in m.value for m in box.markdown)  # welcome gone once chatting
    assert below.type == "chat_input"
    assert [c[2] for c in fake_api] == ["first question", "second question"]


def test_friendly_dates_in_the_ui() -> None:
    from expense_agent.ui.helpers import friendly_date

    assert friendly_date("2026-10-26T14:26:00+00:00") == "26 Oct 2026, 14:26"
    assert friendly_date(None) == "—" and friendly_date("not a date") == "not a date"


def test_version_label_and_healthcheck() -> None:
    from expense_agent.healthcheck import healthy, main
    from expense_agent.ui.helpers import version_label

    assert version_label("release-2026-11") == "Version release"
    assert version_label("unknown") == version_label("") == "Version dev"
    assert healthy("http://example.com/") is False  # only this container's own port
    assert main([]) == 1 and main(["http://127.0.0.1:1/health"]) == 1
