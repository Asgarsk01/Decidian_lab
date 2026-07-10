from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from decidian_docling.chunking import (
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
