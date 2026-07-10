from __future__ import annotations

import json
import re
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
    serialized = [
        serialize_chunk(chunk, chunker, index)
        for index, chunk in enumerate(chunker.chunk(dl_doc=document))
    ]
    chunks: list[dict[str, Any]] = []
    oversized_count = 0
    for payload in serialized:
        segments = enforce_chunk_limit(payload, tokenizer, MAX_TOKENS)
        if len(segments) > 1:
            oversized_count += 1
        chunks.extend(segments)
    for index, payload in enumerate(chunks):
        payload["index"] = index
    config = {
        "type": "hybrid",
        "tokenizer": TOKENIZER_MODEL,
        "max_tokens": MAX_TOKENS,
        "merge_peers": True,
        "repeat_table_header": True,
        "omit_header_on_overflow": False,
        "post_split_oversized_chunks": oversized_count,
    }
    return chunks, config


def enforce_chunk_limit(
    payload: dict[str, Any],
    tokenizer: Any,
    max_tokens: int = MAX_TOKENS,
) -> list[dict[str, Any]]:
    """Losslessly split a serialized chunk when context pushes it over limit."""
    contextualized = str(payload.get("contextualized_text", ""))
    if tokenizer.count_tokens(contextualized) <= max_tokens:
        payload["token_count"] = tokenizer.count_tokens(contextualized)
        return [payload]

    text = str(payload.get("text", ""))
    prefix = _context_prefix(payload)
    while prefix and tokenizer.count_tokens(prefix) >= max_tokens:
        lines = prefix.rstrip("\n").splitlines()
        prefix = "\n".join(lines[1:]) + ("\n" if len(lines) > 1 else "")

    units = re.findall(r"\S+\s*", text)
    if not units:
        return [payload]

    segments: list[str] = []
    start = 0
    while start < len(units):
        low = start + 1
        high = len(units)
        best = start
        while low <= high:
            middle = (low + high) // 2
            candidate = "".join(units[start:middle]).strip()
            count = tokenizer.count_tokens(f"{prefix}{candidate}")
            if count <= max_tokens:
                best = middle
                low = middle + 1
            else:
                high = middle - 1
        if best == start:
            best = start + 1
        segments.append("".join(units[start:best]).strip())
        start = best

    result: list[dict[str, Any]] = []
    segment_count = len(segments)
    for segment_index, segment in enumerate(segments):
        contextualized_segment = f"{prefix}{segment}"
        item = dict(payload)
        item.update(
            {
                "text": segment,
                "contextualized_text": contextualized_segment,
                "token_count": tokenizer.count_tokens(contextualized_segment),
                "segment_index": segment_index,
                "segment_count": segment_count,
            }
        )
        result.append(item)
    return result


def _context_prefix(payload: dict[str, Any]) -> str:
    contextualized = str(payload.get("contextualized_text", ""))
    text = str(payload.get("text", ""))
    if text and contextualized.endswith(text):
        return contextualized[: -len(text)]
    headings = [str(value) for value in payload.get("headings", []) if value]
    return "\n".join(headings) + ("\n" if headings else "")


def write_chunks_jsonl(path: Path, chunks: Iterable[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            handle.write(json.dumps(chunk, ensure_ascii=False, default=str))
            handle.write("\n")
