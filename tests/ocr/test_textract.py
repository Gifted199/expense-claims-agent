"""Textract parsing, receipt key scoping and log hygiene (no AWS calls)."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from expense_agent.ocr.textract import (
    ReceiptKeyError,
    check_receipt_key,
    extract_receipt,
    parse_amount,
    parse_date,
    parse_expense,
)


def field(kind: str, text: str, conf: float = 99.0, currency: str | None = None) -> dict:
    f: dict[str, Any] = {
        "Type": {"Text": kind, "Confidence": 99.0},
        "ValueDetection": {"Text": text, "Confidence": conf},
    }
    if currency:
        f["Currency"] = {"Code": currency, "Confidence": 90.0}
    return f


RESPONSE = {
    "ExpenseDocuments": [
        {
            "SummaryFields": [
                field("VENDOR_NAME", "Riverside Lodge Hotel", 97.0),
                field("INVOICE_RECEIPT_DATE", "20/11/2026", 95.5),
                field("TOTAL", "£520.00", 98.0, "GBP"),
                field("TOTAL", "£52.00", 40.0),  # lower-confidence duplicate is ignored
                field("OTHER", "Room, 2 nights x 260.00 SECRET-LINE-ITEM"),
            ]
        }
    ]
}


class FakeTextract:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def analyze_expense(self, **kwargs: Any) -> dict:
        self.calls.append(kwargs)
        return RESPONSE


def test_parse_expense_picks_best_fields() -> None:
    f = parse_expense(RESPONSE)
    assert (f.vendor, f.date, f.total, f.currency) == (
        "Riverside Lodge Hotel",
        "2026-11-20",
        "520.00",
        "GBP",
    )
    assert f.confidence == pytest.approx(0.955)
    assert not f.needs_review and f.missing == []


def test_missing_fields_need_review() -> None:
    f = parse_expense({"ExpenseDocuments": [{"SummaryFields": [field("TOTAL", "12.00")]}]})
    assert f.missing == ["vendor", "date"] and f.needs_review and f.confidence == 0.0
    assert parse_expense({}).missing == ["vendor", "date", "total"]


@pytest.mark.parametrize(
    ("text", "value"),
    [("£520.00", "520.00"), ("1,234.50", "1234.50"), ("12,50", "12.50"), ("abc", None)],
)
def test_parse_amount(text: str, value: str | None) -> None:
    got = parse_amount(text)
    assert (str(got) if got is not None else None) == value


def test_parse_date_formats() -> None:
    assert str(parse_date("20/11/2026")) == "2026-11-20"
    assert str(parse_date("2026-11-20")) == "2026-11-20"
    assert parse_date("next tuesday") is None


@pytest.mark.parametrize(
    "key", ["samples/receipts/r01.png", "uploads/E0005/2026/r.png", " uploads/E0005/x.jpg "]
)
def test_allowed_keys(key: str) -> None:
    assert check_receipt_key(key, "E0005") == key.strip()


@pytest.mark.parametrize(
    "key",
    [
        "uploads/E0006/r.png",  # someone else's upload
        "uploads/E0005/",  # no file
        "samples/receipts/../../uploads/E0006/r.png",
        "/samples/receipts/r01.png",
        "models/anomaly/latest.json",
        "samples//receipts/r01.png",
        "",
    ],
)
def test_refused_keys(key: str) -> None:
    with pytest.raises(ReceiptKeyError):
        check_receipt_key(key, "E0005")


def test_extract_calls_textract_on_receipts_bucket_and_logs_no_ocr_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    textract = FakeTextract()
    with caplog.at_level(logging.DEBUG):
        out = extract_receipt(textract, "samples/receipts/r03.png", "E0005", bucket="b")
    assert textract.calls == [
        {"Document": {"S3Object": {"Bucket": "b", "Name": "samples/receipts/r03.png"}}}
    ]
    assert out["total"] == "520.00" and out["s3_key"] == "samples/receipts/r03.png"
    logged = caplog.text
    for ocr_text in ("Riverside", "520", "SECRET-LINE-ITEM", "20/11/2026"):
        assert ocr_text not in logged


def test_extract_refuses_before_calling_textract() -> None:
    textract = FakeTextract()
    with pytest.raises(ReceiptKeyError):
        extract_receipt(textract, "uploads/E0006/r.png", "E0005")
    assert textract.calls == []


def test_currency_from_code_text_when_textract_gives_none() -> None:
    resp = {"ExpenseDocuments": [{"SummaryFields": [field("TOTAL", "GBP 24.00")]}]}
    f = parse_expense(resp)
    assert (f.total, f.currency) == ("24.00", "GBP")
