from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from decidian_docling.chunking import (
    build_picture_supplement_chunks,
    enforce_chunk_limit,
    serialize_chunk,
    write_chunks_jsonl,
)


@dataclass
class FakeProvenance:
    page_no: int

    def model_dump(self, **_kwargs):
        return {"page_no": self.page_no}


@dataclass
class FakeItem:
    self_ref: str
    label: str
    prov: list[FakeProvenance]


@dataclass
class FakeMeta:
    doc_items: list[FakeItem]
    headings: list[str]
    captions: list[str]


@dataclass
class FakeChunk:
    text: str
    meta: FakeMeta


class FakeTokenizer:
    def count_tokens(self, text: str) -> int:
        return len(text.split())


class FakeChunker:
    tokenizer = FakeTokenizer()

    def contextualize(self, chunk: FakeChunk) -> str:
        return "\n".join([*chunk.meta.headings, chunk.text])


def test_chunk_serialization_and_jsonl(tmp_path: Path) -> None:
    chunk = FakeChunk(
        text="Retries must stop after three attempts.",
        meta=FakeMeta(
            doc_items=[
                FakeItem("#/texts/4", "paragraph", [FakeProvenance(2)]),
                FakeItem("#/texts/5", "paragraph", [FakeProvenance(3)]),
            ],
            headings=["Retry policy"],
            captions=[],
        ),
    )
    payload = serialize_chunk(chunk, FakeChunker(), 0)
    assert payload["page_numbers"] == [2, 3]
    assert payload["headings"] == ["Retry policy"]
    assert payload["source_refs"][0]["self_ref"] == "#/texts/4"

    output = tmp_path / "chunks.jsonl"
    write_chunks_jsonl(output, [payload])
    stored = json.loads(output.read_text(encoding="utf-8"))
    assert stored["contextualized_text"].startswith("Retry policy")


def test_enforce_chunk_limit_splits_without_losing_text() -> None:
    words = [f"word-{index}" for index in range(18)]
    payload = {
        "index": 0,
        "text": " ".join(words),
        "contextualized_text": "Architecture\n" + " ".join(words),
        "headings": ["Architecture"],
        "captions": [],
        "page_numbers": [9],
        "source_refs": [{"self_ref": "#/tables/1"}],
        "token_count": 19,
    }

    segments = enforce_chunk_limit(payload, FakeTokenizer(), max_tokens=10)

    assert len(segments) > 1
    assert all(segment["token_count"] <= 10 for segment in segments)
    assert " ".join(segment["text"] for segment in segments).split() == words
    assert all(segment["source_refs"] == payload["source_refs"] for segment in segments)


def test_picture_supplement_chunks_preserve_text_trust_and_provenance() -> None:
    records = [
        {
            "picture_file": "picture-0003.png",
            "asset_uri": "assets/diagram.png",
            "page_number": 7,
            "source": "docling_structured",
            "trust": "medium",
            "text": "Promote the replica after health checks pass.",
            "items": [
                {
                    "self_ref": "#/texts/9",
                    "label": "section_header",
                    "text": "4.2 Recovery Sequence",
                    "provenance": [{"page_no": 7}],
                },
                {
                    "self_ref": "#/texts/10",
                    "label": "text",
                    "text": "Promote the replica after health checks pass.",
                    "provenance": [{"page_no": 7}],
                },
            ],
        }
    ]

    chunks = build_picture_supplement_chunks(records, tokenizer=FakeTokenizer())

    assert len(chunks) == 1
    assert chunks[0]["origin"] == "picture_text"
    assert chunks[0]["trust"] == "medium"
    assert chunks[0]["headings"] == ["4.2 Recovery Sequence"]
    assert chunks[0]["page_numbers"] == [7]
    assert chunks[0]["source_refs"][0]["self_ref"] == "#/texts/9"
    assert "Picture text (page 7, picture-0003.png)" in chunks[0]["contextualized_text"]
