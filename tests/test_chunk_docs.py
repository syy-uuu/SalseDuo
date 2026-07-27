"""纯本地测试：docx 切块逻辑（不依赖任何 Databricks 连接）。"""

from pathlib import Path

from src.setup.chunk_docs import chunk_all

_DOCS_DIR = Path(__file__).resolve().parent.parent / "documents_generated"


def test_chunk_all_produces_chunks_from_both_documents():
    chunks = chunk_all(_DOCS_DIR)
    source_files = {c.source_file for c in chunks}
    assert len(source_files) == 2
    assert len(chunks) > 0


def test_credit_tier_matrix_rows_are_chunked_individually():
    chunks = chunk_all(_DOCS_DIR)
    tier_chunks = [c for c in chunks if "Tier 2 Preferred Account" in c.content]
    assert len(tier_chunks) == 1
    assert "Net 60 Days" in tier_chunks[0].content
    assert "$750,000" in tier_chunks[0].content


def test_settlement_method_rows_are_chunked_individually():
    chunks = chunk_all(_DOCS_DIR)
    cheque_chunks = [c for c in chunks if "Corporate Cheques" in c.content]
    assert len(cheque_chunks) == 1
    assert "Strictly Prohibited" in cheque_chunks[0].content


def test_every_chunk_has_required_fields():
    chunks = chunk_all(_DOCS_DIR)
    for c in chunks:
        assert c.chunk_id
        assert c.source_file
        assert c.chunk_type in ("paragraph", "table_row")
        assert c.content.strip()
