from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .chunking import MAX_TOKENS, TOKENIZER_MODEL


def build_clean_chunks(
    canonical_document: dict[str, Any],
    verified_supplements: list[dict[str, Any]] | None = None,
    tokenizer: Any | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    tokenizer = tokenizer or _tokenizer()
    blocks = canonical_document.get("blocks", []) or []
    chunks: list[dict[str, Any]] = []
    excluded = 0
    index = 0
    while index < len(blocks):
        block = blocks[index]
        if block.get("integrity_status") not in {"verified", "ai_verified"}:
            excluded += 1
            index += 1
            continue
        block_type = block.get("type")
        if block_type in {"heading", "picture"}:
            index += 1
            continue
        if block_type == "table":
            chunks.extend(_table_chunks(block, tokenizer))
        elif block_type in {"paragraph", "list_item"}:
            text = str(block.get("text", "")).strip()
            if _needs_value(text) and index + 1 < len(blocks):
                next_block = blocks[index + 1]
                if (
                    next_block.get("type") in {"paragraph", "list_item"}
                    and next_block.get("integrity_status") in {"verified", "ai_verified"}
                    and next_block.get("section_path") == block.get("section_path")
                ):
                    text = f"{text} {str(next_block.get('text', '')).strip()}"
                    index += 1
            chunks.extend(_text_chunks(block, text, tokenizer))
        elif block_type == "code":
            chunks.extend(_code_chunks(block, tokenizer))
        index += 1

    for supplement in verified_supplements or []:
        chunks.extend(_supplement_chunks(supplement, tokenizer))
    for chunk_index, chunk in enumerate(chunks):
        chunk["index"] = chunk_index
    return chunks, {
        "type": "canonical_atomic",
        "tokenizer": TOKENIZER_MODEL,
        "max_tokens": MAX_TOKENS,
        "verified_blocks": sum(
            block.get("integrity_status") in {"verified", "ai_verified"}
            for block in blocks
        ),
        "excluded_blocks": excluded,
        "verified_ai_supplements": len(verified_supplements or []),
        "chunks": len(chunks),
    }


def build_review_queue(
    canonical_document: dict[str, Any],
    candidates: list[dict[str, Any]],
    ai_results: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    block_by_id = {block["id"]: block for block in canonical_document.get("blocks", []) or []}
    queue: list[dict[str, Any]] = []
    for candidate in candidates:
        result = ai_results.get(str(candidate.get("id")), {})
        if result.get("status") == "verified":
            continue
        block = block_by_id.get(str(candidate.get("block_id")), {})
        submitted_paths = result.get("submitted_evidence_paths") or candidate.get(
            "submitted_evidence_paths", []
        )
        prepared_paths = candidate.get("prepared_evidence_paths", [])
        queue.append({
            "id": f"review-{len(queue) + 1:04d}",
            "candidate_id": candidate.get("id"),
            "kind": candidate.get("kind"),
            "block_id": candidate.get("block_id"),
            "source_refs": candidate.get("source_refs", []),
            "page_numbers": candidate.get("page_numbers", []),
            "picture_file": candidate.get("picture_file"),
            "evidence_path": (
                (submitted_paths[0] if submitted_paths else None)
                or candidate.get("evidence_path")
                or candidate.get("source_evidence_path")
            ),
            "source_evidence_path": candidate.get("source_evidence_path"),
            "prepared_evidence_paths": prepared_paths,
            "submitted_evidence_paths": submitted_paths,
            "submission_state": result.get(
                "submission_state",
                "not_submitted" if not result.get("attempted") else "submitted",
            ),
            "response_status": result.get("response_status"),
            "incomplete_details": result.get("incomplete_details"),
            "section_path": candidate.get("section_path", block.get("section_path", [])),
            "ambiguity_reasons": candidate.get("ambiguity_reasons", block.get("ambiguity_reasons", [])),
            "status": result.get("status", "not_reviewed"),
            "verdicts": result.get("verdicts", []),
            "error": result.get("error"),
            "recommended_action": "Review the cited source image/page and approve or correct the excluded content.",
        })
    return queue


def write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(item, ensure_ascii=False, default=str) + "\n" for item in records),
        encoding="utf-8",
    )


def _table_chunks(block: dict[str, Any], tokenizer: Any) -> list[dict[str, Any]]:
    headers = [str(item) for item in block.get("headers", [])]
    rows = block.get("rows", []) or []
    table_name = _context_label(block, "Table")
    result: list[dict[str, Any]] = []
    for row_index, row in enumerate(rows or [[]], start=1):
        cells = [str(cell) for cell in row]
        pairs = [
            f"{headers[index] if index < len(headers) and headers[index] else f'Column {index + 1}'} = {cell}"
            for index, cell in enumerate(cells)
        ]
        text = "; ".join(pairs) if pairs else "; ".join(headers)
        prefix = f"{table_name}\nRow {row_index}\n"
        if tokenizer.count_tokens(prefix + text) <= MAX_TOKENS:
            result.append(_payload(block, text, prefix + text, "table_row", tokenizer, row_index=row_index))
            continue
        group: list[str] = []
        for pair in pairs:
            candidate = "; ".join([*group, pair])
            if group and tokenizer.count_tokens(prefix + candidate) > MAX_TOKENS:
                group_text = "; ".join(group)
                result.append(_payload(block, group_text, prefix + group_text, "table_row", tokenizer, row_index=row_index))
                group = [pair]
            else:
                group.append(pair)
        if group:
            group_text = "; ".join(group)
            result.extend(_split_payload(block, group_text, prefix, "table_row", tokenizer, row_index=row_index))
    return result


def _text_chunks(block: dict[str, Any], text: str, tokenizer: Any) -> list[dict[str, Any]]:
    prefix = _context_prefix(block)
    return _split_payload(block, text, prefix, str(block.get("type")), tokenizer)


def _code_chunks(block: dict[str, Any], tokenizer: Any) -> list[dict[str, Any]]:
    text = str(block.get("text", ""))
    prefix = _context_prefix(block) + f"Code language: {block.get('language') or 'text'}\n"
    if tokenizer.count_tokens(prefix + text) <= MAX_TOKENS:
        return [_payload(block, text, prefix + text, "code", tokenizer)]
    result: list[dict[str, Any]] = []
    group: list[str] = []
    for line in text.splitlines():
        candidate = "\n".join([*group, line])
        if group and tokenizer.count_tokens(prefix + candidate) > MAX_TOKENS:
            content = "\n".join(group)
            result.append(_payload(block, content, prefix + content, "code", tokenizer))
            group = [line]
        else:
            group.append(line)
    if group:
        content = "\n".join(group)
        result.extend(_split_payload(block, content, prefix, "code", tokenizer))
    return result


def _supplement_chunks(item: dict[str, Any], tokenizer: Any) -> list[dict[str, Any]]:
    statement = str(item.get("statement", "")).strip()
    prefix = "\n".join([*item.get("section_path", []), "Gemini-verified source evidence"]) + "\n"
    block = {
        "id": item.get("block_id") or item.get("candidate_id"),
        "section_path": item.get("section_path", []),
        "source_refs": item.get("source_refs", []),
        "page_numbers": item.get("page_numbers", []),
        "integrity_finding_ids": item.get("integrity_finding_ids", []),
    }
    chunks = _split_payload(block, statement, prefix, "ai_verified_evidence", tokenizer)
    for chunk in chunks:
        chunk.update({
            "origin": "gemini_verified",
            "candidate_id": item.get("candidate_id"),
            "claim_id": item.get("claim_id"),
            "evidence": item.get("evidence", []),
            "model": item.get("model"),
        })
    return chunks


def _split_payload(
    block: dict[str, Any],
    text: str,
    prefix: str,
    content_type: str,
    tokenizer: Any,
    **extra: Any,
) -> list[dict[str, Any]]:
    if tokenizer.count_tokens(prefix + text) <= MAX_TOKENS:
        return [_payload(block, text, prefix + text, content_type, tokenizer, **extra)]
    units = [unit for unit in re.split(r"(?<=[.!?])\s+|\n\n+", text) if unit]
    if len(units) == 1:
        units = re.findall(r"\S+\s*", text)
    result: list[dict[str, Any]] = []
    group: list[str] = []
    for unit in units:
        separator = " " if re.search(r"[.!?]$", "".join(group).strip()) else ""
        candidate = ("".join(group) + separator + unit).strip()
        if group and tokenizer.count_tokens(prefix + candidate) > MAX_TOKENS:
            content = "".join(group).strip()
            result.append(_payload(block, content, prefix + content, content_type, tokenizer, **extra))
            group = [unit]
        else:
            group.append((separator if group else "") + unit)
    if group:
        content = "".join(group).strip()
        result.append(_payload(block, content, prefix + content, content_type, tokenizer, **extra))
    return result


def _payload(
    block: dict[str, Any],
    text: str,
    contextualized: str,
    content_type: str,
    tokenizer: Any,
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        "index": 0,
        "text": text,
        "contextualized_text": contextualized,
        "content_type": content_type,
        "section_path": block.get("section_path", []),
        "headings": block.get("section_path", []),
        "page_numbers": block.get("page_numbers", []),
        "provenance_scope": "page" if block.get("page_numbers") else "section_only" if block.get("section_path") or block.get("source_refs") else "unavailable",
        "source_refs": [{"self_ref": ref, "label": content_type, "provenance": []} for ref in block.get("source_refs", [])],
        "block_ids": [block.get("id")] if block.get("id") else [],
        "integrity_status": "verified",
        "integrity_finding_ids": block.get("integrity_finding_ids", []),
        "origin": "canonical",
        "token_count": tokenizer.count_tokens(contextualized),
    }
    payload.update(extra)
    return payload


def _context_prefix(block: dict[str, Any]) -> str:
    headings = [str(item) for item in block.get("section_path", [])]
    return ("\n".join(headings) + "\n") if headings else ""


def _context_label(block: dict[str, Any], fallback: str) -> str:
    headings = block.get("section_path", []) or []
    return f"{headings[-1]} — {fallback}" if headings else fallback


def _needs_value(text: str) -> bool:
    return bool(re.search(r"(?:=|:)\s*$", text))


def _tokenizer() -> Any:
    from docling_core.transforms.chunker.tokenizer.huggingface import HuggingFaceTokenizer

    return HuggingFaceTokenizer.from_pretrained(
        model_name=TOKENIZER_MODEL,
        max_tokens=MAX_TOKENS,
        model_max_length=1_000_000,
    )
