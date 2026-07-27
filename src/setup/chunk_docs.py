"""把 documents_generated/ 下的 docx 解析成结构化 chunk 列表。

纯本地逻辑，不依赖任何 Databricks 连接，便于单测（tests/test_chunk_docs.py）。

切块策略：文档本身很短（每份仅几段正文 + 1-2 张表），按"逻辑段落"切块比固定长度滑窗更利于
检索精度——每个编号小节（"1. Purpose and Scope" 这种标题起始的段落）合并为一个 chunk；
表格按行切块（表头 + 单行数据拼成一个 chunk），这样"Tier 2 的账期是多少"这类问题能直接命中
对应的那一行，而不必命中整张表。
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
    """按 docx XML 中出现的真实顺序，依次产出段落(Paragraph)或表格(Table)对象。"""
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
                # 这是文档头部的 Policy ID / Effective Date / Approved By / Applicable To
                # 元数据表，不是业务规则内容。实测发现这类短小、通用的文本在向量检索里
                # 会有异常高的相似度分数（几乎对任何查询都排前列），把真正相关的规则段落
                # 挤到后面（例如"超限15%需要谁审批"这个问题检索不到第3节的审批流程段落，
                # 反而排前面的全是这几行元数据）。这里索引价值低、噪音大，直接跳过不索引。
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
