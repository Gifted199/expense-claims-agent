"""Expense Claims Assistant: Streamlit UI (port 8501).

``uv run python -m streamlit run src/expense_agent/ui/app.py``

Talks only to the agent API (``EXPENSE_API_URL``) and holds no secrets. The user switcher is a
demo login over synthetic employees; production would use Amazon Cognito (D42, D45).
"""

from __future__ import annotations

import uuid
from typing import Any

import streamlit as st

from expense_agent.ui.client import ApiClient, ApiError
from expense_agent.ui.helpers import friendly_date, plain, prefill_message

CAPABILITIES: tuple[tuple[str, str, str], ...] = (
    ("📘", "Ask about policy", "Answers from the latest expense handbook, with section citations."),
    ("📊", "Check my spending", "Totals by category or month, straight from your claims."),
    (
        "🧾",
        "Submit a claim",
        "Fixed checks decide: within policy is approved, the rest goes to your manager.",
    ),
    ("📷", "Scan a receipt", "Upload a photo or PDF and we read the vendor, date and total."),
    ("🔎", "Track my claims", "See the status of any claim and why it was decided that way."),
)
EXAMPLES: tuple[str, ...] = (
    "What's the hotel cap in London for my grade?",
    "How much have I spent this year by category?",
    "Do I need a receipt for a £15 taxi?",
    "What's the status of my latest claim?",
    "Can I claim alcohol at a client dinner?",
)
CHAT_HEIGHT = 520  # px: fits a laptop screen with the input below
STATUSES = ("all", "approved", "pending_manager", "submitted", "rejected")

CSS = """
<style>
.cap-card {border: 1px solid rgba(49,51,63,.15); border-radius: 12px; padding: 14px 14px 10px;
           height: 100%; background: rgba(240,244,250,.55);}
.cap-card h4 {margin: 0 0 6px; font-size: 1rem;}
.cap-card p {margin: 0; font-size: .86rem; color: rgba(49,51,63,.75);}
.cap-icon {font-size: 1.4rem;}
.reason {font-size: .9rem; margin: 0 0 2px;}
</style>
"""


def state_defaults() -> None:
    st.session_state.setdefault("employee_id", None)
    st.session_state.setdefault("thread_id", str(uuid.uuid4()))
    st.session_state.setdefault("messages", [])
    st.session_state.setdefault("pending", None)
    st.session_state.setdefault("ocr", None)


def reset_conversation() -> None:
    st.session_state.thread_id = str(uuid.uuid4())
    st.session_state.messages = []
    st.session_state.ocr = None


def person_label(p: dict[str, Any]) -> str:
    role = " · manager" if p.get("is_manager") else ""
    return f"{p['full_name']} ({p['employee_code']}, {p['grade']}{role})"


def sidebar(people: list[dict[str, Any]]) -> ApiClient:
    st.sidebar.title("Expense Claims")
    ids = [p["employee_id"] for p in people]
    by_id = {p["employee_id"]: p for p in people}
    current = st.session_state.employee_id if st.session_state.employee_id in by_id else ids[0]
    chosen = st.sidebar.selectbox(
        "Signed in as (demo)",
        ids,
        index=ids.index(current),
        format_func=lambda i: person_label(by_id[i]),
    )
    st.sidebar.caption("Demo login over synthetic employees. Production uses Amazon Cognito.")
    if chosen != st.session_state.employee_id:
        st.session_state.employee_id = chosen
        reset_conversation()
    if st.sidebar.button("New conversation", width="stretch"):
        reset_conversation()
    client = ApiClient(chosen)
    receipt_panel(client)
    return client


def receipt_panel(client: ApiClient) -> None:
    st.sidebar.divider()
    st.sidebar.subheader("Scan a receipt")
    file = st.sidebar.file_uploader(
        "PNG, JPEG or PDF, up to 5 MB", type=["png", "jpg", "jpeg", "pdf"]
    )
    if file is not None and st.sidebar.button("Read receipt", width="stretch"):
        with st.sidebar.status("Reading the receipt..."):
            try:
                st.session_state.ocr = client.upload(file.name, file.getvalue(), file.type)
            except ApiError as exc:
                st.sidebar.error(str(exc))
    result = st.session_state.ocr
    if not result:
        return
    ocr = result.get("ocr")
    if not ocr:
        st.sidebar.warning(result.get("error") or "The receipt couldn't be read.")
        return
    st.sidebar.markdown(
        f"**Vendor:** {ocr.get('vendor') or '—'}  \n**Date:** {ocr.get('date') or '—'}  \n"
        f"**Total:** {ocr.get('currency') or ''} {ocr.get('total') or '—'}  \n"
        f"**Confidence:** {ocr.get('confidence', 0):.0%}"
    )
    if ocr.get("needs_review"):
        st.sidebar.warning("Please check these values before submitting.")
    if st.sidebar.button("Create a claim from this receipt", type="primary", width="stretch"):
        st.session_state.pending = prefill_message(result["s3_key"], ocr)


def welcome() -> None:
    st.markdown("### What I can help you with")
    cols = st.columns(len(CAPABILITIES))
    for col, (icon, title, text) in zip(cols, CAPABILITIES, strict=True):
        col.markdown(
            f'<div class="cap-card"><div class="cap-icon">{icon}</div><h4>{title}</h4>'
            f"<p>{text}</p></div>",
            unsafe_allow_html=True,
        )
    st.markdown("#### Try asking")
    ex_cols = st.columns(2)
    for i, example in enumerate(EXAMPLES):
        if ex_cols[i % 2].button(example, key=f"ex{i}", width="stretch"):
            st.session_state.pending = example


def ask(client: ApiClient, message: str) -> dict[str, Any]:
    """One chat turn through the agent API, as an assistant message for the history."""
    try:
        out = client.chat(st.session_state.thread_id, message)
        return {"role": "assistant", "content": out["reply"], "tools": out.get("tools_used", [])}
    except ApiError as exc:
        return {"role": "assistant", "content": f"⚠️ {exc}", "tools": []}


def render_message(m: dict[str, Any]) -> None:
    with st.chat_message(m["role"]):
        st.markdown(m["content"])
        if m.get("tools"):
            st.caption("Used: " + ", ".join(dict.fromkeys(m["tools"])))


def assistant_tab(client: ApiClient) -> None:
    """A normal chat layout: a fixed-height, auto-scrolling message area (oldest at the top,
    newest at the bottom), with the chat input always below it. An empty conversation shows the
    welcome cards and examples inside the message area."""
    box = st.container(height=CHAT_HEIGHT, border=True, autoscroll=True, key="chat_box")
    typed = st.chat_input("Ask about policy, your spending, or submit a claim...", key="chat_input")
    message = typed or st.session_state.pending
    st.session_state.pending = None
    with box:
        for m in st.session_state.messages:
            render_message(m)
        if message:
            user = {"role": "user", "content": message}
            st.session_state.messages.append(user)
            render_message(user)
            with st.chat_message("assistant"), st.spinner("Thinking..."):
                reply = ask(client, message)
            st.session_state.messages.append(reply)
            st.rerun()  # redraw the history in order, with the new reply at the bottom
        elif not st.session_state.messages:
            welcome()
            if st.session_state.pending:
                st.rerun()


def claims_tab(client: ApiClient) -> None:
    status = st.selectbox("Status", STATUSES, key="claims_status")
    try:
        data = client.claims(None if status == "all" else status)
    except ApiError as exc:
        st.error(str(exc))
        return
    rows = data.get("claims", [])
    if not rows:
        st.info("No claims to show.")
        return
    st.dataframe(
        [
            {
                "Ref": c["claim_ref"],
                "Spent": friendly_date(c.get("incurred_at")),
                "Submitted": friendly_date(c.get("submitted_at")),
                "Category": c["category"],
                "Merchant": c["merchant"],
                "Amount (£)": c["amount"],
                "Status": c["status"],
            }
            for c in rows
        ],
        hide_index=True,
        width="stretch",
    )
    st.caption(f"Your {len(rows)} most recently submitted claims, newest first.")


def queue_tab(client: ApiClient) -> None:
    try:
        claims = client.queue().get("claims", [])
    except ApiError as exc:
        st.error(str(exc))
        return
    if not claims:
        st.success("Nothing waiting for you.")
        return
    st.caption(f"{len(claims)} claim(s) from your team need a decision.")
    for c in claims:
        # Merchant, description and city are typed by employees: never render them as markdown
        # (a markdown image link would make the manager's browser load an external URL).
        title = (
            f"{c['claim_ref']} · {plain(c['employee_name'])} · {c['category']} · £{c['amount']} "
            f"· {plain(c['merchant'])}"
        )
        with st.expander(title):
            score = c.get("anomaly_score")
            score = "—" if score is None else score
            hotel = f"Hotel: {c['city']}, {c['nights']} night(s)\n" if c.get("city") else ""
            st.text(
                f"Spent: {friendly_date(c.get('incurred_at'))}\n"
                f"Submitted: {friendly_date(c.get('submitted_at'))}\n"
                f"Description: {c.get('description') or '—'}\n"
                f"{hotel}Anomaly score: {score}"
            )
            reasons = c.get("review_reasons") or []
            st.markdown("**Why it needs you:**")
            for r in reasons or ["(no reasons recorded for this older claim)"]:
                st.markdown(f"- {r}")
            reason = st.text_input("Reason for your decision", key=f"reason{c['claim_id']}")
            left, right = st.columns(2)
            for col, decision, label in (
                (left, "approved", "Approve"),
                (right, "rejected", "Reject"),
            ):
                if col.button(label, key=f"{decision}{c['claim_id']}", width="stretch"):
                    if not reason.strip():
                        st.warning("Please give a reason.")
                        continue
                    try:
                        client.decide(c["claim_id"], decision, reason)
                        st.toast(f"{c['claim_ref']} {decision}.")
                        st.rerun()
                    except ApiError as exc:
                        st.error(str(exc))


def main() -> None:
    st.set_page_config(page_title="Expense Claims Assistant", page_icon="🧾", layout="wide")
    st.markdown(CSS, unsafe_allow_html=True)
    state_defaults()
    try:
        people = ApiClient(None).employees()
    except ApiError as exc:
        st.error(str(exc))
        st.stop()
    client = sidebar(people)
    me = next(p for p in people if p["employee_id"] == st.session_state.employee_id)
    st.title("Expense Claims Assistant")
    st.caption(f"Hello {me['full_name'].split()[0]}. Synthetic demo data only.")
    names = ["Assistant", "My claims"] + (["Manager queue"] if me.get("is_manager") else [])
    tabs = st.tabs(names)
    with tabs[0]:
        assistant_tab(client)
    with tabs[1]:
        claims_tab(client)
    if me.get("is_manager"):
        with tabs[2]:
            queue_tab(client)


main()
