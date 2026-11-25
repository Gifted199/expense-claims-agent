"""Structure-aware chunking of the handbook (decision D25).

One chunk per subsection (``### EXP-n.m``); a section's intro text (``## EXP-n``) is its own
chunk. A chunk never crosses a subsection boundary, so every chunk has exactly one citation.
Each chunk's text starts with its heading path ("EXP-6 Travel and hotels > EXP-6.3 Hotels by
city") so the embedding knows the context. Over-long subsections are split at paragraph or
table-row boundaries, repeating a table's header rows in every part.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MAX_CHARS = 1800
_SECTION_RE = re.compile(r"^## (EXP-\d+) (.+)$")
_SUBSECTION_RE = re.compile(r"^### (EXP-\d+\.\d+) (.+)$")


@dataclass(frozen=True)
class Chunk:
    section_id: str
    section_path: str
    title: str
    chunk_index: int
    text: str  # starts with the heading path


def _blocks(body: str) -> list[list[str]]:
    """Paragraphs and tables, as lists of lines (blank lines separate blocks)."""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in body.splitlines():
        if line.strip():
            current.append(line)
        elif current:
            blocks.append(current)
            current = []
    if current:
        blocks.append(current)
    return blocks


def _is_table(block: list[str]) -> bool:
    return len(block) >= 2 and block[0].startswith("|") and set(block[1]) <= set("|-: ")


def _split(path: str, body: str, max_chars: int) -> list[str]:
    """Body text parts, each (with the path) at most ~max_chars, never splitting a line."""
    budget = max(200, max_chars - len(path) - 2)
    units: list[str] = []
    for block in _blocks(body):
        text = "\n".join(block)
        if len(text) <= budget or not _is_table(block):
            units.append(text)
            continue
        header, rows = block[:2], block[2:]
        part: list[str] = []
        for row in rows:
            candidate = "\n".join([*header, *part, row])
            if part and len(candidate) > budget:
                units.append("\n".join([*header, *part]))
                part = []
            part.append(row)
        units.append("\n".join([*header, *part]))
    parts: list[str] = []
    current = ""
    for unit in units:
        joined = f"{current}\n\n{unit}" if current else unit
        if current and len(joined) > budget:
            parts.append(current)
            current = unit
        else:
            current = joined
    if current:
        parts.append(current)
    return parts


def chunk_handbook(markdown: str, max_chars: int = MAX_CHARS) -> list[Chunk]:
    chunks: list[Chunk] = []
    section_id = section_title = ""
    unit_id = unit_title = ""
    body: list[str] = []

    def flush() -> None:
        text = "\n".join(body).strip()
        if not unit_id or not text:
            return
        if unit_id == section_id:
            path = f"{section_id} {section_title}"
        else:
            path = f"{section_id} {section_title} > {unit_id} {unit_title}"
        for i, part in enumerate(_split(path, text, max_chars)):
            chunks.append(Chunk(unit_id, path, unit_title, i, f"{path}\n\n{part}"))

    for line in markdown.splitlines():
        if m := _SECTION_RE.match(line):
            flush()
            section_id, section_title = m.group(1), m.group(2).strip()
            unit_id, unit_title, body = section_id, section_title, []
        elif m := _SUBSECTION_RE.match(line):
            flush()
            unit_id, unit_title, body = m.group(1), m.group(2).strip(), []
        elif section_id:  # everything before the first section is the document header
            body.append(line)
    flush()
    return chunks
