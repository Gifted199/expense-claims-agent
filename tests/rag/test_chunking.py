"""Structure-aware chunking: one subsection per chunk, path prefixed, tables split safely."""

from __future__ import annotations

from expense_agent.rag.chunking import chunk_handbook
from expense_agent.rag.handbook import LATEST, PolicyData, render

DOC = """# Title

Header text that is not a section.

## EXP-1 First section

Intro of section one.

### EXP-1.1 Alpha

Alpha body.

### EXP-1.2 Beta

Beta body line one.

Beta paragraph two.

## EXP-2 Second

### EXP-2.1 Gamma

Gamma body.
"""


def test_one_chunk_per_subsection_with_path() -> None:
    chunks = chunk_handbook(DOC)
    assert [c.section_id for c in chunks] == ["EXP-1", "EXP-1.1", "EXP-1.2", "EXP-2.1"]
    beta = chunks[2]
    assert beta.text.startswith("EXP-1 First section > EXP-1.2 Beta")
    assert "Beta paragraph two." in beta.text and "Gamma" not in beta.text
    assert all("Header text" not in c.text for c in chunks)


def test_empty_section_intro_is_skipped() -> None:
    assert "EXP-2" not in [c.section_id for c in chunk_handbook(DOC)]


def test_long_table_split_repeats_header() -> None:
    rows = "\n".join(f"| Row {i} | {'x' * 40} |" for i in range(80))
    doc = f"## EXP-9 Big\n\n### EXP-9.1 Table\n\n| Name | Value |\n|---|---|\n{rows}\n"
    chunks = chunk_handbook(doc, max_chars=600)
    assert len(chunks) > 1
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))
    for c in chunks:
        assert c.section_id == "EXP-9.1"
        assert "| Name | Value |\n|---|---|" in c.text
        assert len(c.text) <= 700
    joined = "".join(c.text for c in chunks)
    assert all(f"| Row {i} |" in joined for i in range(80))


def test_long_prose_split_at_paragraphs() -> None:
    paras = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(10))
    chunks = chunk_handbook(f"## EXP-3 P\n\n### EXP-3.1 Long\n\n{paras}\n", max_chars=800)
    assert len(chunks) > 1
    joined = " ".join(c.text for c in chunks)
    assert all(f"Paragraph {i} " in joined for i in range(10))


def test_real_handbook_chunks_are_bounded(policy_data: PolicyData) -> None:
    chunks = chunk_handbook(render(policy_data, LATEST))
    assert len(chunks) >= 30
    assert all(c.text.startswith(c.section_path) for c in chunks)
    assert max(len(c.text) for c in chunks) <= 1900
