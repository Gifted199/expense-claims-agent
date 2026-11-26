"""Draw 5 synthetic receipt images for demos and evaluation (decision D37).

``uv run python -m expense_agent.ocr.make_receipts --profile aero --upload``

All vendors are fictional and every image is stamped "SYNTHETIC RECEIPT - DEMO DATA ONLY".
Dates are relative to today so that demo claims fall inside the 30-day deadline. Images and a
``manifest.json`` of the expected values go to ``s3://<receipts>/samples/receipts/``.
"""

from __future__ import annotations

import argparse
import io
import json
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from expense_agent.ocr.textract import SAMPLES_PREFIX, receipts_bucket

WIDTH, HEIGHT = 640, 900
# The bundled Pillow font has no pound-sign glyph, so amounts are printed as "GBP 12.50".
FOOTER = "SYNTHETIC RECEIPT - DEMO DATA ONLY"


@dataclass(frozen=True)
class Receipt:
    name: str
    vendor: str
    address: str
    days_ago: int
    items: tuple[tuple[str, str], ...]
    total: str
    category: str
    city: str | None = None
    nights: int | None = None


RECEIPTS: tuple[Receipt, ...] = (
    Receipt(
        "r01",
        "Green Leaf Kitchen",
        "12 Mill Lane, Leeds",
        3,
        (("Soup of the day", "6.50"), ("Club sandwich", "9.00"), ("Sparkling water", "3.00")),
        "18.50",
        "meals",
    ),
    Receipt(
        "r02",
        "CityCab",
        "Station Road, Manchester",
        2,
        (("Station to client office", "21.00"), ("Waiting time", "3.00")),
        "24.00",
        "taxi",
    ),
    Receipt(
        "r03",
        "Riverside Lodge",
        "5 Wharf Street, London",
        5,
        (("Room, 2 nights x 260.00", "520.00"),),
        "520.00",
        "hotel",
        city="London",
        nights=2,
    ),
    Receipt(
        "r04",
        "OfficeHub",
        "8 Market Square, York",
        1,
        (("A4 notebooks x4", "14.00"), ("Gel pens x10", "12.40"), ("Desk organiser", "10.00")),
        "36.40",
        "office_supplies",
    ),
    Receipt(  # the same lunch again: for the duplicate-claim demo
        "r05",
        "Green Leaf Kitchen",
        "12 Mill Lane, Leeds",
        1,
        (("Soup of the day", "6.50"), ("Club sandwich", "9.00"), ("Sparkling water", "3.00")),
        "18.50",
        "meals",
    ),
)


def check_receipts(receipts: Sequence[Receipt] = RECEIPTS) -> None:
    """Item lines must add up to the total, so the images are self-consistent."""
    for r in receipts:
        if sum(Decimal(p) for _, p in r.items) != Decimal(r.total):
            raise ValueError(f"{r.name}: items don't add up to {r.total}")


def draw(receipt: Receipt, on: date) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    big = ImageFont.load_default(size=34)
    font = ImageFont.load_default(size=26)
    small = ImageFont.load_default(size=20)
    img = Image.new("RGB", (WIDTH, HEIGHT), "white")
    d = ImageDraw.Draw(img)
    y = 40
    d.text((40, y), receipt.vendor, fill="black", font=big)
    y += 56
    d.text((40, y), receipt.address, fill="black", font=font)
    y += 44
    d.text((40, y), f"Date: {on:%d/%m/%Y}", fill="black", font=font)
    y += 60
    for label, price in receipt.items:
        d.text((40, y), label, fill="black", font=font)
        d.text((WIDTH - 180, y), f"GBP {price}", fill="black", font=font)
        y += 42
    y += 20
    d.line((40, y, WIDTH - 40, y), fill="black", width=2)
    y += 24
    d.text((40, y), "TOTAL", fill="black", font=big)
    d.text((WIDTH - 200, y), f"GBP {receipt.total}", fill="black", font=big)
    y += 70
    d.text((40, y), "Paid by card. Thank you!", fill="black", font=font)
    d.text((40, HEIGHT - 60), FOOTER, fill="black", font=small)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def build(today: date) -> tuple[dict[str, bytes], dict[str, Any]]:
    """name -> PNG bytes, plus a manifest of the values each image should OCR to."""
    check_receipts()
    images: dict[str, bytes] = {}
    entries = []
    for r in RECEIPTS:
        on = today - timedelta(days=r.days_ago)
        images[f"{r.name}.png"] = draw(r, on)
        entry = {k: v for k, v in asdict(r).items() if k not in ("items", "days_ago", "address")}
        entries.append(
            {
                **entry,
                "key": f"{SAMPLES_PREFIX}{r.name}.png",
                "date": on.isoformat(),
                "currency": "GBP",
            }
        )
    return images, {"generated_on": today.isoformat(), "receipts": entries}


def upload(s3: Any, bucket: str, images: dict[str, bytes], manifest: dict[str, Any]) -> list[str]:
    keys = []
    for name, body in images.items():
        key = f"{SAMPLES_PREFIX}{name}"
        s3.put_object(Bucket=bucket, Key=key, Body=body, ContentType="image/png")
        keys.append(key)
    key = f"{SAMPLES_PREFIX}manifest.json"
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(manifest, indent=2).encode(),
        ContentType="application/json",
    )
    return [*keys, key]


def main(argv: Sequence[str] | None = None) -> int:
    from expense_agent.db.connection import DbSettings, add_profile_argument, make_session

    parser = argparse.ArgumentParser(description=__doc__)
    add_profile_argument(parser)
    parser.add_argument("--out", type=Path, default=None, help="also save the PNGs here")
    parser.add_argument("--upload", action="store_true", help="upload to the receipts bucket")
    args = parser.parse_args(argv)

    images, manifest = build(date.today())
    out = args.out or Path(tempfile.mkdtemp(prefix="receipts-"))
    out.mkdir(parents=True, exist_ok=True)
    for name, body in images.items():
        (out / name).write_bytes(body)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"wrote {len(images)} receipts to {out}")
    if args.upload:
        session = make_session(args.profile or None, DbSettings.from_env().region)
        keys = upload(session.client("s3"), receipts_bucket(), images, manifest)
        print("uploaded:", *keys, sep="\n  ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
