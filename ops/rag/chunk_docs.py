"""Parses the docx files under documents_generated/ into a list of structured chunks.

Pure local logic, no dependency on any Databricks connection, which makes it easy to
unit-test (tests/test_chunk_docs.py).

Chunking strategy: the documents themselves are short (a few paragraphs of body text
plus 1-2 tables each), so chunking by "logical section" gives better retrieval
precision than a fixed-length sliding window — each numbered section (a paragraph
starting with a heading like "1. Purpose and Scope") is merged into one chunk; tables
are chunked row by row (header + a single data row combined into one chunk), so a
question like "what's the Tier 2 payment term" can hit that exact row directly instead
of needing to match the whole table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import docx

_HEADING_RE = re.compile(r"^\d+\.\s+\S")


@dataclass
class Chunk:
    chunk_id: str
    source_file: str
    chunk_seq: int
    section_title: str
    chunk_type: str  # "paragraph" | "table_row"
    content: str


def _iter_block_items(document: docx.Document):
    """Yields Paragraph or Table objects in the actual order they appear in the docx
    XML."""
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    body = document.element.body
    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, document)
        elif child.tag == qn("w:tbl"):
            yield Table(child, document)


def chunk_document(path: Path) -> list[Chunk]:
    document = docx.Document(str(path))
    source_file = path.name
    chunks: list[Chunk] = []
    seq = 0
    current_title = "Header"
    current_paragraphs: list[str] = []

    def flush_paragraph_chunk():
        nonlocal seq, current_paragraphs
        text = "\n".join(p for p in current_paragraphs if p.strip())
        if text.strip():
            seq += 1
            chunks.append(
                Chunk(
                    chunk_id=f"{source_file}::p{seq}",
                    source_file=source_file,
                    chunk_seq=seq,
                    section_title=current_title,
                    chunk_type="paragraph",
                    content=text,
                )
            )
        current_paragraphs = []

    for block in _iter_block_items(document):
        cls_name = type(block).__name__
        if cls_name == "Paragraph":
            text = block.text.strip()
            if not text:
                continue
            if _HEADING_RE.match(text):
                flush_paragraph_chunk()
                current_title = text
            current_paragraphs.append(text)
        elif cls_name == "Table":
            flush_paragraph_chunk()
            rows = [[c.text.strip() for c in row.cells] for row in block.rows]
            if not rows:
                continue
            is_key_value_table = len(rows[0]) == 2
            if is_key_value_table:
                # This is the Policy ID / Effective Date / Approved By / Applicable To
                # metadata table at the top of the document, not business-rule content.
                # In practice this kind of short, generic text gets an unusually high
                # similarity score in vector search (it ranks near the top for almost
                # any query), crowding out the actually-relevant rule paragraphs (e.g.
                # a question like "who needs to approve a 15% overage" fails to surface
                # section 3's approval-workflow paragraph, because these few metadata
                # rows rank above it instead). Low retrieval value, high noise — skip
                # indexing it entirely.
                continue
            header = rows[0]
            data_rows = rows[1:] or rows
            for row in data_rows:
                seq += 1
                row_text = "; ".join(f"{h}: {v}" for h, v in zip(header, row) if h or v)
                chunks.append(
                    Chunk(
                        chunk_id=f"{source_file}::t{seq}",
                        source_file=source_file,
                        chunk_seq=seq,
                        section_title=current_title,
                        chunk_type="table_row",
                        content=f"[{current_title}] {row_text}",
                    )
                )
    flush_paragraph_chunk()
    return chunks


def chunk_all(docs_dir: Path) -> list[Chunk]:
    all_chunks: list[Chunk] = []
    for path in sorted(docs_dir.glob("*.docx")):
        all_chunks.extend(chunk_document(path))
    return all_chunks
