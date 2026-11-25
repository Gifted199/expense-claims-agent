"""Synthetic receipts: 5 PNGs plus a manifest; upload goes to samples/receipts/ (S3 mocked)."""

from __future__ import annotations

import json
from datetime import date

import pytest

pytest.importorskip("PIL")

from expense_agent.ocr import make_receipts as mr  # noqa: E402
from expense_agent.ocr.textract import check_receipt_key  # noqa: E402


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def put_object(self, Bucket: str, Key: str, Body: bytes, ContentType: str) -> None:  # noqa: N803
        self.objects[Key] = (Body, ContentType)


def test_build_makes_five_pngs_and_a_consistent_manifest() -> None:
    images, manifest = mr.build(date(2026, 11, 25))
    assert len(images) == 5
    assert all(body.startswith(b"\x89PNG") for body in images.values())
    entries = manifest["receipts"]
    assert [e["key"] for e in entries] == [f"samples/receipts/r0{i}.png" for i in range(1, 6)]
    hotel = next(e for e in entries if e["category"] == "hotel")
    assert (hotel["city"], hotel["nights"], hotel["total"]) == ("London", 2, "520.00")
    assert (
        entries[0]["vendor"] == entries[4]["vendor"] and entries[0]["total"] == entries[4]["total"]
    )
    assert all(check_receipt_key(e["key"], "E0001") for e in entries)  # readable by everyone


def test_items_add_up() -> None:
    mr.check_receipts()
    bad = mr.Receipt("x", "V", "A", 0, (("a", "1.00"),), "2.00", "meals")
    with pytest.raises(ValueError, match="add up"):
        mr.check_receipts([bad])


def test_upload_writes_images_then_manifest() -> None:
    images, manifest = mr.build(date(2026, 11, 25))
    s3 = FakeS3()
    keys = mr.upload(s3, "bucket", images, manifest)
    assert keys[-1] == "samples/receipts/manifest.json"
    assert s3.objects["samples/receipts/r01.png"][1] == "image/png"
    assert json.loads(s3.objects["samples/receipts/manifest.json"][0])["receipts"]
