"""Receipt upload checks (decision D43): type by magic bytes, size, and a server-chosen key."""

from __future__ import annotations

import uuid

MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # Textract's synchronous AnalyzeExpense limit for images
SIGNATURES: tuple[tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"%PDF-", "pdf", "application/pdf"),
)


class UploadError(ValueError):
    pass


def sniff(body: bytes) -> tuple[str, str]:
    """(extension, content type) from the file's first bytes; the filename is ignored."""
    for magic, ext, content_type in SIGNATURES:
        if body.startswith(magic):
            return ext, content_type
    raise UploadError("only PNG, JPEG or PDF receipts are accepted")


def check_upload(body: bytes) -> tuple[str, str]:
    if not body:
        raise UploadError("the file is empty")
    if len(body) > MAX_UPLOAD_BYTES:
        raise UploadError("the file is larger than 5 MB")
    return sniff(body)


def upload_key(employee_code: str, ext: str) -> str:
    """Always under the caller's own prefix, with a random name (the user's filename is unused)."""
    return f"uploads/{employee_code}/{uuid.uuid4().hex}.{ext}"
