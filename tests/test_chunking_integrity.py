from __future__ import annotations

from dataclasses import dataclass

from decidian_docling.chunking import (
    build_integrity_warning_supplement_chunks,
    build_picture_supplement_chunks,
    serialize_chunk,
)


class FakeTokenizer:
    def count_tokens(self, text: str) -> int:
        return len(text.split())


class FakeChunker:
    tokenizer = FakeTokenizer()

    def contextualize(self, chunk):
        return chunk.text


@dataclass
class FakeItem:
    self_ref: str = "#/texts/0"
    label: str = "text"
    prov: list[object] | None = None


@dataclass
class FakeMeta:
    doc_items: list[FakeItem]
    headings: list[str]
    captions: list[str]


@dataclass
class FakeChunk:
    text: str
    meta: FakeMeta


def test_chunk_without_page_provenance_is_explicitly_section_only() -> None:
    chunk = FakeChunk(
        text="Requirement text",
        meta=FakeMeta(doc_items=[FakeItem(prov=[])], headings=["Architecture"], captions=[]),
    )

    payload = serialize_chunk(chunk, FakeChunker(), 0)

    assert payload["page_numbers"] == []
    assert payload["provenance_scope"] == "section_only"


def test_chunk_without_any_source_context_is_explicitly_unavailable() -> None:
    chunk = FakeChunk(text="Orphan", meta=FakeMeta(doc_items=[], headings=[], captions=[]))

    payload = serialize_chunk(chunk, FakeChunker(), 0)

    assert payload["provenance_scope"] == "unavailable"


def test_blocking_finding_creates_provenance_linked_warning_chunk() -> None:
    chunks = build_integrity_warning_supplement_chunks(
        [
            {
                "id": "si-0001",
                "message": "Picture text was not recovered.",
                "llm_warning": "Do not infer image semantics.",
                "blocks_llm_readiness": True,
                "source_refs": ["#/pictures/0"],
                "picture_file": "picture-0001.png",
                "pages": [],
            }
        ],
        tokenizer=FakeTokenizer(),
    )

    assert chunks[0]["origin"] == "semantic_integrity_warning"
    assert chunks[0]["integrity_status"] == "review_required"
    assert chunks[0]["source_refs"][0]["self_ref"] == "#/pictures/0"
    assert chunks[0]["provenance_scope"] == "unavailable"


def test_low_trust_picture_chunk_keeps_picture_and_heading_provenance() -> None:
    chunks = build_picture_supplement_chunks(
        [
            {
                "picture_file": "picture-0001.png",
                "source_ref": "#/pictures/0",
                "source": "tesseract_ocr",
                "trust": "low",
                "text": "Recovered label",
                "context_items": [
                    {
                        "self_ref": "#/texts/10",
                        "label": "section_header",
                        "text": "8.2 Architecture",
                        "provenance": [],
                    }
                ],
            }
        ],
        tokenizer=FakeTokenizer(),
    )

    assert chunks[0]["headings"] == ["8.2 Architecture"]
    assert [ref["self_ref"] for ref in chunks[0]["source_refs"]] == [
        "#/pictures/0",
        "#/texts/10",
    ]
    assert chunks[0]["provenance_scope"] == "section_only"
