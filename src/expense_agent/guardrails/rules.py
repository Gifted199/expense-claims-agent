"""Deterministic guardrail rules (decision D41). Pure functions, no LLM, easy to test.

Input: length, prompt-injection patterns, investment-advice detection (per sentence, so a
multi-part message can have its allowed parts answered and only the investment part declined).
Output: strip investment advice, every GBP figure must come from a tool result (or the user's own
message) in this turn, policy answers must cite a returned section ID.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

MAX_INPUT_CHARS = 2000

INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\bignore\b.{0,40}\b(instructions|rules|prompt|above|previous)\b",
        r"\bdisregard\b.{0,40}\b(instructions|rules|prompt|above|previous)\b",
        r"\b(system|developer|hidden)\s+(prompt|message|instructions)\b",
        r"\byou\s+are\s+now\b",
        r"\b(developer|god|jailbreak|dan)\s+mode\b",
        r"\b(reveal|print|show|repeat)\b.{0,30}\b((your|system|hidden|initial)\s+(prompt|instructions)"
        r"|token|secret|password)\b",
        r"\bact\s+as\s+(an?\s+)?(admin|administrator|manager|system|developer)\b",
        r"\b(i\s+am|i'm|acting\s+as|log\s*in\s+as|pretend\s+to\s+be)\s+(employee\s+)?E\d{4}\b",
        r"\bas\s+(my\s+|the\s+|a\s+)?manager\b.{0,40}\b(approve|reject)\b",
        # Commands to decide a claim ("approve my claim", "you approve it"), not questions about one
        r"^\s*(please\s+)?(approve|reject)\b",
        r"\b(you|assistant)\s+(should\s+|must\s+|can\s+|will\s+)?(approve|reject)\b",
        r"\bemployee[_\s-]?id\s*[:=]",
        r"</?\s*(system|assistant|tool)\s*>",
    )
)

_PRODUCTS = (
    r"(etfs?|index\s+funds?|funds?|stocks?|shares|equit(y|ies)|bonds?|crypto(currency|currencies)?"
    r"|bitcoin|ethereum|isas?|pensions?|portfolio|tracker|s&p\s*500|ftse|nasdaq)"
)
INVESTMENT_INPUT = (
    # Advice-seeking + an investing verb. "buy"/"sell" alone are not enough (train tickets, lunch):
    # they only count next to a financial product (the product patterns below).
    re.compile(
        r"\b(should|could|would|which|what|best|good|recommend|advice|advise|worth)\b.{0,60}"
        r"\b(invest|put\s+(my\s+)?money\s+(in|into))\b",
        re.IGNORECASE,
    ),
    re.compile(rf"\b(buy(ing)?|sell(ing)?|trad(e|ing))\b.{{0,30}}\b{_PRODUCTS}\b", re.IGNORECASE),
    re.compile(r"\binvest(ing|ment)?\b.{0,40}\b(my|our|bonus|salary|savings|money)\b", re.I),
    re.compile(rf"\b(which|what|best|good|recommend)\b.{{0,40}}\b{_PRODUCTS}\b", re.IGNORECASE),
    re.compile(rf"\b{_PRODUCTS}\b.{{0,40}}\b(to\s+buy|should\s+i|worth\s+buying|recommend)", re.I),
)
INVESTMENT_ADVICE_OUTPUT = (
    re.compile(
        rf"\b(you\s+(should|could|might|may\s+want\s+to)|i\s+(would\s+)?(recommend|suggest)|consider"
        rf"|try)\b.{{0,60}}\b(buy(ing)?|invest(ing)?|sell(ing)?|put(ting)?)\b.{{0,60}}\b{_PRODUCTS}",
        re.IGNORECASE,
    ),
    re.compile(
        rf"\b(a\s+good|the\s+best|solid)\s+(choice|option|pick)\b.{{0,40}}\b{_PRODUCTS}", re.I
    ),
)

INJECTION_REFUSAL = (
    "I can't follow that request. I can help with expense claims, receipts, your spending and "
    "the expense policy, always as the signed-in user."
)
INVESTMENT_REFUSAL = (
    "I can't give investment or financial-product advice. I can help with expense claims, "
    "receipts, your spending and the expense policy."
)
INVESTMENT_DECLINE_LINE = (
    "I can't help with the investment part of your question: I don't give investment advice."
)
MIXED_INTENT_NOTE = (
    "Guardrail note: part of the user's message asks for investment advice. Do NOT give any "
    "investment advice. Briefly decline that part in one sentence, and fully answer every other "
    "part, using tools as usual."
)
TOO_LONG = f"Your message is too long. Please keep it under {MAX_INPUT_CHARS} characters."

_QUESTION_START = (
    r"(?:which|what|how|can|could|should|would|is|are|do|does|where|when|why|who|tell|give"
    r"|show|please|i|my)\b"
)
_SENTENCE_SPLIT = re.compile(
    r"(?<=[.?!;])\s+|\s+(?:and\s+also|also,?|plus)\s+|\n+"
    rf"|,?\s+(?:and|but)\s+(?={_QUESTION_START})",
    re.IGNORECASE,
)
_GBP = re.compile(r"(?:£|\bGBP\s?)\s?(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?", re.IGNORECASE)
_NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,2}))?(?![\d])")
_SECTION = re.compile(r"\bEXP-\d+(?:\.\d+)?\b")


@dataclass(frozen=True)
class InputVerdict:
    action: str  # "allow" | "refuse"
    reply: str | None = None
    flags: tuple[str, ...] = ()
    mixed_investment: bool = False


def sentences(text: str) -> list[str]:
    parts = [p.strip(" ,") for p in _SENTENCE_SPLIT.split(text or "")]
    return [p for p in parts if p and re.search(r"[A-Za-z]", p)]


def is_investment(text: str) -> bool:
    return any(p.search(text) for p in INVESTMENT_INPUT)


def check_input(message: str) -> InputVerdict:
    text = (message or "").strip()
    if not text:
        return InputVerdict("refuse", "Please type a question or request.", ("empty",))
    if len(text) > MAX_INPUT_CHARS:
        return InputVerdict("refuse", TOO_LONG, ("too_long",))
    if any(p.search(text) for p in INJECTION_PATTERNS):
        return InputVerdict("refuse", INJECTION_REFUSAL, ("prompt_injection",))
    parts = sentences(text)
    flagged = [s for s in parts if is_investment(s)]
    if flagged and len(flagged) == len(parts):
        return InputVerdict("refuse", INVESTMENT_REFUSAL, ("investment_only",))
    if flagged:
        return InputVerdict("allow", flags=("investment_mixed",), mixed_investment=True)
    return InputVerdict("allow")


def strip_investment_advice(answer: str) -> tuple[str, bool]:
    """Remove sentences that give investment advice; mentions (e.g. EXP-10) stay."""
    kept: list[str] = []
    removed = False
    for line in (answer or "").split("\n"):
        pieces = re.split(r"(?<=[.!?])\s+", line)
        good = [s for s in pieces if not any(p.search(s) for p in INVESTMENT_ADVICE_OUTPUT)]
        removed |= len(good) != len(pieces)
        if good or not line.strip():
            kept.append(" ".join(good))
    text = "\n".join(kept).strip()
    if removed and INVESTMENT_DECLINE_LINE not in text:
        text = f"{text}\n\n{INVESTMENT_DECLINE_LINE}".strip()
    return text, removed


def _amount(whole: str, frac: str | None) -> Decimal | None:
    try:
        return Decimal(f"{whole.replace(',', '')}.{frac or '0'}").quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def money_in_answer(answer: str) -> set[Decimal]:
    return {a for w, f in _GBP.findall(answer or "") if (a := _amount(w, f)) is not None}


def _plain(text: str) -> str:
    """Tool results are JSON, where "£" may be escaped as \u00a3: decode before matching."""
    data = parse_json(text)
    if data is None:
        return text or ""
    return json.dumps(data, ensure_ascii=False)


def numbers_in(texts: Iterable[str]) -> set[Decimal]:
    found: set[Decimal] = set()
    for text in texts:
        for w, f in _NUMBER.findall(_plain(text)):
            if (a := _amount(w, f)) is not None:
                found.add(a)
    return found


def unverified_money(answer: str, tool_texts: Iterable[str], user_text: str) -> list[str]:
    """GBP figures in the answer that appear in no tool result this turn nor in the user's text."""
    allowed = numbers_in([*tool_texts, user_text])
    return [f"£{a:,.2f}" for a in sorted(money_in_answer(answer) - allowed)]


@dataclass
class PolicyEvidence:
    section_ids: set[str] = field(default_factory=set)
    covered: bool = False  # at least one search_policy call returned not_covered = false


def policy_evidence(tool_results: Iterable[tuple[str, str]]) -> PolicyEvidence:
    """From (tool_name, content) pairs of this turn."""
    ev = PolicyEvidence()
    for name, content in tool_results:
        if name != "search_policy":
            continue
        data = parse_json(content)
        if not isinstance(data, dict):
            continue
        if not data.get("not_covered", True):
            ev.covered = True
        ev.section_ids |= {p.get("section_id", "") for p in data.get("passages", []) if p}
    return ev


def missing_citation(answer: str, ev: PolicyEvidence) -> bool:
    if not ev.covered:
        return False
    cited = set(_SECTION.findall(answer or ""))
    returned_sections = {s.split(".")[0] for s in ev.section_ids}
    return not any(c in ev.section_ids or c.split(".")[0] in returned_sections for c in cited)


def parse_json(content: Any) -> Any:
    if isinstance(content, (dict, list)):
        return content
    text = content if isinstance(content, str) else text_of(content)
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def text_of(content: Any) -> str:
    """Flatten LangChain message content (str or a list of content blocks) to text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for block in content:
            if isinstance(block, str):
                out.append(block)
            elif isinstance(block, dict) and isinstance(block.get("text"), str):
                out.append(block["text"])
        return "\n".join(out)
    return "" if content is None else str(content)
