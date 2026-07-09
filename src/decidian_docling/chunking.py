from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

TOKENIZER_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
MAX_TOKENS = 1200


def _value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def serialize_chunk(chunk: Any, chunker: Any, index: int) -> dict[str, Any]:
    doc_items = list(chunk.meta.doc_items)
    page_numbers: set[int] = set()
    source_refs: list[dict[str, Any]] = []

    for item in doc_items:
        provenance = []
        for prov in getattr(item, "prov", []) or []:
            page_no = getattr(prov, "page_no", None)
            if page_no is not None:
                page_numbers.add(int(page_no))
            if hasattr(prov, "model_dump"):
                provenance.append(prov.model_dump(mode="json", exclude_none=True))

        source_refs.append(
            {
                "self_ref": str(getattr(item, "self_ref", "")),
                "label": _value(getattr(item, "label", None)),
                "provenance": provenance,
            }
        )

    contextualized = chunker.contextualize(chunk=chunk)
    headings = list(chunk.meta.headings or [])
    if hasattr(chunk.meta, "model_dump"):
        meta_payload = chunk.meta.model_dump(mode="json", exclude_none=True)
        captions = list(meta_payload.get("captions", []))
    else:
        captions = list(getattr(chunk.meta, "captions", []) or [])
    return {
        "index": index,
        "text": chunk.text,
        "contextualized_text": contextualized,
        "headings": headings,
        "captions": captions,
        "page_numbers": sorted(page_numbers),
        "source_refs": source_refs,
        "token_count": chunker.tokenizer.count_tokens(contextualized),
    }


def build_chunks(document: Any) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    from docling.chunking import HybridChunker
    from docling_core.transforms.chunker.tokenizer.huggingface import (
        HuggingFaceTokenizer,
    )

    tokenizer = HuggingFaceTokenizer.from_pretrained(
        model_name=TOKENIZER_MODEL,
        max_tokens=MAX_TOKENS,
        # This tokenizer is used only for counting. HybridChunker enforces the
        # real 1,200-token limit after inspecting potentially larger segments.
        model_max_length=1_000_000,
    )
    chunker = HybridChunker(
        tokenizer=tokenizer,
        merge_peers=True,
        repeat_table_header=True,
        omit_header_on_overflow=False,
    )
    chunks = [
        serialize_chunk(chunk, chunker, index)
        for index, chunk in enumerate(chunker.chunk(dl_doc=document))
    ]
    config = {
        "type": "hybrid",
        "tokenizer": TOKENIZER_MODEL,
        "max_tokens": MAX_TOKENS,
        "merge_peers": True,
        "repeat_table_header": True,
        "omit_header_on_overflow": False,
    }
    return chunks, config


def write_chunks_jsonl(path: Path, chunks: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            handle.write(json.dumps(chunk, ensure_ascii=False, default=str))
            handle.write("\n")
