"""Receipt OCR with Amazon Textract AnalyzeExpense (decision D35).

* Only receipts the caller may see are read: the shared synthetic demo receipts under
  ``samples/receipts/`` or the caller's own uploads under ``uploads/<employee_code>/``.
* Logs hold the S3 key, which fields were found and the confidences, **never** OCR text.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_RECEIPTS_BUCKET = "akinsds1126-expense-receipts"
SAMPLES_PREFIX = "samples/receipts/"
UPLOADS_PREFIX = "uploads/"
REVIEW_BELOW = 0.80  # field confidence (0-1) under which the user should check the value
_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/_.\-]{0,1023}$")
_DATE_FORMATS = ("%d/%m/%Y", "%d/%m/%y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%d %b %Y", "%d %B %Y")
_SYMBOLS = {"£": "GBP", "€": "EUR", "$": "USD"}


class ReceiptKeyError(ValueError):
    """The S3 key is malformed or not one the caller may read."""


def receipts_bucket() -> str:
    return os.environ.get("EXPENSE_RECEIPTS_BUCKET", DEFAULT_RECEIPTS_BUCKET)


def uploads_prefix(employee_code: str) -> str:
    return f"{UPLOADS_PREFIX}{employee_code}/"


def check_receipt_key(key: str, employee_code: str) -> str:
    """Return the key if the caller may read it; raise ReceiptKeyError otherwise.
    The same message is used for "malformed" and "someone else's", so nothing leaks."""
    key = (key or "").strip()
    if not _KEY_RE.match(key) or ".." in key or "//" in key:
        raise ReceiptKeyError("receipt key is not valid")
    for prefix in (SAMPLES_PREFIX, uploads_prefix(employee_code)):
        if key.startswith(prefix) and len(key) > len(prefix):
            return key
    raise ReceiptKeyError("receipt not found")


@dataclass(frozen=True)
class ReceiptFields:
    vendor: str | None
    date: str | None  # ISO yyyy-mm-dd when it could be parsed
    total: str | None  # decimal string, e.g. "45.60"
    currency: str | None
    confidence: float  # lowest confidence of vendor, date and total (0 if any is missing)
    needs_review: bool
    missing: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_amount(text: str) -> Decimal | None:
    cleaned = re.sub(r"[^0-9.,\-]", "", text or "")
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(",", "")
    elif cleaned.count(",") == 1 and len(cleaned.split(",")[1]) == 2:
        cleaned = cleaned.replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    try:
        value = Decimal(cleaned).quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError):
        return None
    return value if value > 0 else None


def parse_date(text: str) -> date | None:
    text = (text or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _best(fields: list[dict[str, Any]], *types: str) -> dict[str, Any] | None:
    """The highest-confidence summary field of the first type that is present."""
    for wanted in types:
        found = [f for f in fields if (f.get("Type") or {}).get("Text") == wanted]
        found = [f for f in found if (f.get("ValueDetection") or {}).get("Text")]
        if found:
            return max(found, key=lambda f: f["ValueDetection"].get("Confidence", 0.0))
    return None


def parse_expense(response: dict[str, Any]) -> ReceiptFields:
    docs = response.get("ExpenseDocuments") or []
    fields: list[dict[str, Any]] = docs[0].get("SummaryFields", []) if docs else []
    vendor_f = _best(fields, "VENDOR_NAME", "NAME")
    date_f = _best(fields, "INVOICE_RECEIPT_DATE")
    total_f = _best(fields, "TOTAL", "AMOUNT_PAID")

    vendor = vendor_f["ValueDetection"]["Text"].strip() if vendor_f else None
    parsed_date = parse_date(date_f["ValueDetection"]["Text"]) if date_f else None
    total_text = total_f["ValueDetection"]["Text"] if total_f else ""
    total = parse_amount(total_text)
    currency = None
    if total_f and (total_f.get("Currency") or {}).get("Code"):
        currency = str(total_f["Currency"]["Code"]).upper()
    else:
        upper = total_text.upper()
        currency = next((code for sym, code in _SYMBOLS.items() if sym in total_text), None)
        currency = currency or next((c for c in _SYMBOLS.values() if c in upper), None)

    missing = [
        name
        for name, value in (("vendor", vendor), ("date", parsed_date), ("total", total))
        if value is None
    ]
    confs = [
        f["ValueDetection"].get("Confidence", 0.0) / 100.0
        for f in (vendor_f, date_f, total_f)
        if f is not None
    ]
    confidence = round(min(confs), 3) if confs and not missing else 0.0
    return ReceiptFields(
        vendor=vendor,
        date=parsed_date.isoformat() if parsed_date else None,
        total=str(total) if total is not None else None,
        currency=currency,
        confidence=confidence,
        needs_review=bool(missing) or confidence < REVIEW_BELOW,
        missing=missing,
    )


def extract_receipt(textract: Any, key: str, employee_code: str, bucket: str | None = None) -> dict:
    """Run AnalyzeExpense on an S3 receipt the caller may read. Logs no OCR text."""
    key = check_receipt_key(key, employee_code)
    response = textract.analyze_expense(
        Document={"S3Object": {"Bucket": bucket or receipts_bucket(), "Name": key}}
    )
    fields = parse_expense(response)
    log.info(
        "extract_receipt key=%s confidence=%.3f missing=%s",
        key,
        fields.confidence,
        ",".join(fields.missing) or "-",
    )
    return {"s3_key": key, **fields.to_dict()}
