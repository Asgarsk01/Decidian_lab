from __future__ import annotations

import hashlib
import re
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from docx import Document
from docx.document import Document as DocxDocument
from docx.table import Table
from docx.text.paragraph import Paragraph


SCHEMA_VERSION = "1.0"
NUMBERED_HEADING_RE = re.compile(r"^\s*(\d+(?:\.\d+){0,5})\.?\s+(.+)$")
YAML_KEY_RE = re.compile(r"^\s*(?:[-?]\s+)?[A-Za-z_][\w.-]*\s*:\s*(?:.*)$")
CODE_STYLE_MARKERS = ("code", "preformatted", "source", "console", "terminal")
CODE_START_RE = re.compile(
    r"^\s*(?:#|//|/\*|```|apiVersion\s*:|kind\s*:|services\s*:|"
    r"pipeline\s*\{|stages?\s*\{|node\s*\{|FROM\s+|kubectl\s+|docker\s+)",
    re.IGNORECASE,
)
RECORD_HEADING_RE = re.compile(
    r"(?:\bmust\b|\bshall\b|\bshould\b|\brequired\b|\bUNIQUE\b|\bINDEX\b|"
    r"\bFOREIGN\s+KEY\b|\bNOT\s+NULL\b|\bCHECK\b|^[A-Za-z][\w]*_[A-Za-z][\w]*)",
    re.IGNORECASE,
)


@dataclass
class CanonicalResult:
    document: dict[str, Any]
    candidates: list[dict[str, Any]]


def build_canonical_document(
    source_path: Path,
    document_data: dict[str, Any],
    semantic_report: dict[str, Any],
    visual_report: dict[str, Any],
    picture_records: list[dict[str, Any]],
) -> CanonicalResult:
    extension = source_path.suffix.lower()
    native_error: str | None = None
    if extension == ".docx":
        try:
            blocks = _docx_blocks(source_path, document_data)
        except Exception as exc:
            native_error = f"{type(exc).__name__}: {exc}"
            blocks = _docling_blocks(document_data)
            for block in blocks:
                block["integrity_status"] = "review_required"
                block["ambiguity_reasons"] = list(dict.fromkeys([
                    *block.get("ambiguity_reasons", []),
                    "docx_native_reconciliation_failed",
                ]))
    else:
        blocks = _docling_blocks(document_data)

    _attach_integrity(blocks, semantic_report, visual_report)
    candidates = _ambiguity_candidates(blocks, semantic_report, visual_report, picture_records)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "source": {
            "filename": source_path.name,
            "extension": extension,
        },
        "blocks": blocks,
        "summary": {
            "blocks": len(blocks),
            "headings": sum(block["type"] == "heading" for block in blocks),
            "code_blocks": sum(block["type"] == "code" for block in blocks),
            "tables": sum(block["type"] == "table" for block in blocks),
            "pictures": sum(block["type"] == "picture" for block in blocks),
            "ambiguous": sum(block["integrity_status"] != "verified" for block in blocks),
            "ai_candidates": len(candidates),
            "native_reconciliation_failed": native_error is not None,
        },
        "errors": [native_error] if native_error else [],
    }
    return CanonicalResult(document=payload, candidates=candidates)


def canonical_markdown(document: dict[str, Any]) -> str:
    output: list[str] = []
    for block in document.get("blocks", []):
        block_type = block.get("type")
        if block_type == "heading":
            output.extend([f"{'#' * int(block.get('level') or 1)} {block.get('text', '')}", ""])
        elif block_type == "code":
            output.extend([
                f"```{block.get('language') or 'text'}",
                str(block.get("text", "")),
                "```",
                "",
            ])
        elif block_type == "table":
            headers = [str(item) for item in block.get("headers", [])]
            rows = [[str(cell) for cell in row] for row in block.get("rows", [])]
            if headers:
                output.append("| " + " | ".join(_escape_cell(item) for item in headers) + " |")
                output.append("| " + " | ".join("---" for _ in headers) + " |")
                for row in rows:
                    padded = (row + [""] * len(headers))[: len(headers)]
                    output.append("| " + " | ".join(_escape_cell(item) for item in padded) + " |")
                output.append("")
        elif block_type == "list_item":
            output.extend([f"- {block.get('text', '')}", ""])
        elif block_type == "picture":
            if block.get("asset_uri"):
                output.extend([f"![Picture]({block['asset_uri']})", ""])
        elif block.get("text"):
            output.extend([str(block["text"]), ""])
    return "\n".join(output).rstrip() + "\n"


def _docx_blocks(source_path: Path, document_data: dict[str, Any]) -> list[dict[str, Any]]:
    document = Document(source_path)
    ref_lookup = _docling_text_refs(document_data)
    table_ref_lookup = _docling_table_refs(document_data)
    blocks: list[dict[str, Any]] = []
    section_path: list[str] = []
    code_buffer: list[tuple[str, dict[str, Any]]] = []
    paragraph_index = 0
    table_index = 0
    picture_index = 0
    docling_pictures = document_data.get("pictures", []) or []
    native_picture_refs: set[str] = set()

    def flush_code() -> None:
        if not code_buffer:
            return
        text = "\n".join(item[0] for item in code_buffer).strip("\n")
        metadata = code_buffer[0][1]
        blocks.append(
            _block(
                len(blocks),
                "code",
                text=text,
                section_path=section_path,
                source_refs=list(dict.fromkeys(
                    ref
                    for item in code_buffer
                    for ref in (item[1].get("source_ref"), item[1].get("native_ref"))
                    if ref
                )),
                native=metadata,
                language=_code_language(text),
            )
        )
        code_buffer.clear()

    def append_native_pictures(count: int, native_ref: str) -> None:
        nonlocal picture_index
        for _ in range(count):
            if picture_index >= len(docling_pictures):
                return
            picture = docling_pictures[picture_index]
            ref = str(picture.get("self_ref") or f"#/pictures/{picture_index}")
            prov = picture.get("prov", []) or []
            pages = sorted({
                int(item["page_no"])
                for item in prov
                if isinstance(item.get("page_no"), (int, float))
            })
            blocks.append(
                _block(
                    len(blocks),
                    "picture",
                    section_path=section_path,
                    source_refs=[ref],
                    page_numbers=pages,
                    bbox=prov[0].get("bbox") if prov else None,
                    asset_uri=(picture.get("image") or {}).get("uri"),
                    native={
                        "source": "docx_ooxml_drawing_order",
                        "native_ref": native_ref,
                        "anchor_ref": str((picture.get("parent") or {}).get("$ref") or "") or None,
                        "anchor_matched": True,
                    },
                )
            )
            native_picture_refs.add(ref)
            picture_index += 1

    for item in _iter_docx_content(document):
        if isinstance(item, Paragraph):
            native_ref = f"docx://paragraphs/{paragraph_index}"
            paragraph_index += 1
            drawing_count = _drawing_count(item._p)
            text = item.text.strip()
            if not text:
                flush_code()
                append_native_pictures(drawing_count, native_ref)
                continue
            metadata = _paragraph_metadata(item, ref_lookup, native_ref)
            heading_level = _docx_heading_level(item, text)
            is_list = metadata["numbered"] and heading_level is None
            is_code = _is_code_paragraph(item, text, bool(code_buffer))
            if is_code:
                code_buffer.append((text, metadata))
                append_native_pictures(drawing_count, native_ref)
                continue
            flush_code()
            if heading_level is not None:
                while len(section_path) >= heading_level:
                    section_path.pop()
                section_path.append(text)
                blocks.append(
                    _block(
                        len(blocks),
                        "heading",
                        text=text,
                        level=heading_level,
                        section_path=section_path,
                        source_refs=list(dict.fromkeys(filter(None, [
                            metadata.get("source_ref"),
                            metadata.get("native_ref"),
                        ]))),
                        native=metadata,
                    )
                )
            else:
                blocks.append(
                    _block(
                        len(blocks),
                        "list_item" if is_list else "paragraph",
                        text=text,
                        section_path=section_path,
                        source_refs=list(dict.fromkeys(filter(None, [
                            metadata.get("source_ref"),
                            metadata.get("native_ref"),
                        ]))),
                        native=metadata,
                    )
                )
            append_native_pictures(drawing_count, native_ref)
        elif isinstance(item, Table):
            flush_code()
            native_ref = f"docx://tables/{table_index}"
            table_index += 1
            rows = [[cell.text.strip() for cell in row.cells] for row in item.rows]
            headers = rows[0] if rows else []
            table_signature = _grid_signature(rows)
            docling_ref = (
                table_ref_lookup[table_signature].popleft()
                if table_ref_lookup.get(table_signature)
                else None
            )
            blocks.append(
                _block(
                    len(blocks),
                    "table",
                    section_path=section_path,
                    source_refs=list(dict.fromkeys(filter(None, [docling_ref, native_ref]))),
                    headers=headers,
                    rows=rows[1:] if rows else [],
                    native={"source": "docx_ooxml", "native_ref": native_ref},
                )
            )
            append_native_pictures(_drawing_count(item._tbl), native_ref)
    flush_code()
    return _insert_docling_pictures(
        blocks,
        document_data,
        section_path,
        consumed_picture_refs=native_picture_refs,
    )


def _iter_docx_content(document: DocxDocument) -> Iterable[Paragraph | Table]:
    if hasattr(document, "iter_inner_content"):
        yield from document.iter_inner_content()
        return
    for child in document.element.body.iterchildren():
        if child.tag.endswith("}p"):
            yield Paragraph(child, document)
        elif child.tag.endswith("}tbl"):
            yield Table(child, document)


def _drawing_count(element: Any) -> int:
    embedded = element.xpath(
        ".//*[local-name()='blip']/@*[local-name()='embed']"
    )
    legacy = element.xpath(
        ".//*[local-name()='imagedata']/@*[local-name()='id']"
    )
    return len(embedded) + len(legacy)


def _paragraph_metadata(
    paragraph: Paragraph,
    ref_lookup: dict[str, deque[str]],
    native_ref: str,
) -> dict[str, Any]:
    text_key = _signature(paragraph.text)
    source_ref = ref_lookup[text_key].popleft() if ref_lookup.get(text_key) else None
    p_pr = paragraph._p.pPr
    numbered = bool(p_pr is not None and p_pr.numPr is not None)
    outline = None
    if p_pr is not None:
        values = p_pr.xpath("./w:outlineLvl/@w:val")
        if values:
            try:
                outline = int(values[0]) + 1
            except (TypeError, ValueError):
                pass
    return {
        "source": "docx_ooxml",
        "style": paragraph.style.name if paragraph.style else None,
        "style_id": paragraph.style.style_id if paragraph.style else None,
        "outline_level": outline,
        "numbered": numbered,
        "left_indent": str(paragraph.paragraph_format.left_indent or ""),
        "monospace": any(
            run.font.name and run.font.name.casefold() in {"consolas", "courier new", "courier"}
            for run in paragraph.runs
        ),
        "source_ref": source_ref,
        "native_ref": native_ref,
    }


def _docx_heading_level(paragraph: Paragraph, text: str) -> int | None:
    metadata_style = (paragraph.style.name if paragraph.style else "").casefold()
    p_pr = paragraph._p.pPr
    outline = None
    if p_pr is not None:
        values = p_pr.xpath("./w:outlineLvl/@w:val")
        if values:
            try:
                outline = int(values[0]) + 1
            except (TypeError, ValueError):
                pass
    styled = re.search(r"heading\s*([1-6])", metadata_style)
    level = int(styled.group(1)) if styled else outline
    numbered = bool(p_pr is not None and p_pr.numPr is not None)
    if numbered and RECORD_HEADING_RE.search(text):
        return None
    if numbered and level is None:
        return None
    number_match = NUMBERED_HEADING_RE.match(text)
    if (
        level is None
        and number_match
        and len(text) <= 140
        and any(run.bold for run in paragraph.runs if run.text.strip())
        and not RECORD_HEADING_RE.search(text)
    ):
        level = number_match.group(1).count(".") + 1
    if level is not None and number_match:
        level = min(6, number_match.group(1).count(".") + 1)
    return min(6, max(1, level)) if level is not None else None


def _is_code_paragraph(paragraph: Paragraph, text: str, in_code: bool) -> bool:
    style = (paragraph.style.name if paragraph.style else "").casefold()
    explicit = any(marker in style for marker in CODE_STYLE_MARKERS)
    monospace = any(
        run.font.name and run.font.name.casefold() in {"consolas", "courier new", "courier"}
        for run in paragraph.runs
    )
    if explicit or monospace or CODE_START_RE.match(text):
        return True
    if in_code:
        return bool(
            YAML_KEY_RE.match(text)
            or re.match(r"^\s*[-{}\[\]]", text)
            or re.match(r"^\s+[A-Za-z_]", paragraph.text)
        )
    return False


def _docling_blocks(document_data: dict[str, Any]) -> list[dict[str, Any]]:
    indexes = {
        "texts": document_data.get("texts", []) or [],
        "tables": document_data.get("tables", []) or [],
        "pictures": document_data.get("pictures", []) or [],
        "groups": document_data.get("groups", []) or [],
    }
    ordered = list(_walk_refs(document_data.get("body", {}).get("children", []), indexes, set()))
    blocks: list[dict[str, Any]] = []
    section_path: list[str] = []
    for ref, item in ordered:
        kind = ref.split("/")[1] if ref.startswith("#/") and "/" in ref[2:] else ""
        prov = item.get("prov", []) or []
        page_numbers = sorted({int(p["page_no"]) for p in prov if isinstance(p.get("page_no"), (int, float))})
        if kind == "texts":
            text = str(item.get("text", "")).strip()
            if not text or item.get("content_layer") == "furniture":
                continue
            label = str(item.get("label", "text"))
            level = int(item.get("level") or 1) if label == "section_header" else None
            numeric = NUMBERED_HEADING_RE.match(text)
            ambiguities: list[str] = []
            if level and numeric:
                expected = numeric.group(1).count(".") + 1
                if expected != level:
                    ambiguities.append("heading_hierarchy_mismatch")
                    level = min(6, expected)
            if label == "section_header" and CODE_START_RE.match(text):
                block_type = "code"
                ambiguities.append("code_comment_misclassified_as_heading")
            else:
                block_type = "heading" if label == "section_header" else "list_item" if label in {"list_item", "checkbox_selected", "checkbox_unselected"} else "paragraph"
            if block_type == "heading":
                while len(section_path) >= int(level or 1):
                    section_path.pop()
                section_path.append(text)
            blocks.append(
                _block(
                    len(blocks),
                    block_type,
                    text=text,
                    level=level,
                    language=_code_language(text) if block_type == "code" else None,
                    section_path=section_path,
                    source_refs=[ref],
                    page_numbers=page_numbers,
                    bbox=prov[0].get("bbox") if prov else None,
                    ambiguity_reasons=ambiguities,
                    integrity_status="ambiguous" if ambiguities else "verified",
                )
            )
        elif kind == "tables":
            headers, rows = _docling_table_rows(item)
            blocks.append(
                _block(
                    len(blocks),
                    "table",
                    section_path=section_path,
                    source_refs=[ref],
                    page_numbers=page_numbers,
                    bbox=prov[0].get("bbox") if prov else None,
                    headers=headers,
                    rows=rows,
                )
            )
        elif kind == "pictures":
            blocks.append(
                _block(
                    len(blocks),
                    "picture",
                    section_path=section_path,
                    source_refs=[ref],
                    page_numbers=page_numbers,
                    bbox=prov[0].get("bbox") if prov else None,
                    asset_uri=(item.get("image") or {}).get("uri"),
                )
            )
    _mark_mixed_column_ambiguity(blocks, document_data)
    return _coalesce_docling_code(blocks)


def _mark_mixed_column_ambiguity(
    blocks: list[dict[str, Any]],
    document_data: dict[str, Any],
) -> None:
    """Flag strongly alternating PDF column order for targeted page review."""
    pages = document_data.get("pages", {}) or {}
    by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for block in blocks:
        if block.get("type") not in {"paragraph", "heading", "list_item"}:
            continue
        if block.get("page_numbers") and block.get("bbox"):
            by_page[int(block["page_numbers"][0])].append(block)
    for page_number, page_blocks in by_page.items():
        page_data = pages.get(str(page_number), pages.get(page_number, {})) or {}
        width = float((page_data.get("size") or {}).get("width") or 0)
        if width <= 0:
            continue
        sides = []
        for block in page_blocks:
            bbox = block.get("bbox") or {}
            center = (float(bbox.get("l") or 0) + float(bbox.get("r") or 0)) / 2
            sides.append("left" if center < width / 2 else "right")
        transitions = sum(left != right for left, right in zip(sides, sides[1:]))
        if "left" not in sides or "right" not in sides or transitions < 3:
            continue
        for block in page_blocks:
            block["integrity_status"] = "ambiguous"
            block["ambiguity_reasons"] = list(dict.fromkeys([
                *block.get("ambiguity_reasons", []),
                "mixed_column_reading_order",
            ]))


def _coalesce_docling_code(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Reconstruct PDF code/config runs without turning comment lines into headings."""
    output: list[dict[str, Any]] = []
    index = 0
    while index < len(blocks):
        block = blocks[index]
        if block.get("type") != "code":
            output.append(block)
            index += 1
            continue
        group = [block]
        cursor = index + 1
        while cursor < len(blocks):
            next_block = blocks[cursor]
            next_text = str(next_block.get("text", ""))
            if (
                next_block.get("type") in {"paragraph", "code"}
                and next_block.get("section_path") == block.get("section_path")
                and (
                    next_block.get("type") == "code"
                    or YAML_KEY_RE.match(next_text)
                    or re.match(r"^\s*[-{}\[\]]", next_text)
                )
            ):
                group.append(next_block)
                cursor += 1
                continue
            break
        merged = dict(block)
        merged["text"] = "\n".join(str(item.get("text", "")) for item in group)
        merged["source_refs"] = list(dict.fromkeys(
            ref for item in group for ref in item.get("source_refs", [])
        ))
        merged["language"] = _code_language(merged["text"])
        if len(group) > 1:
            merged["integrity_status"] = "verified"
            merged["ambiguity_reasons"] = []
        output.append(merged)
        index = cursor
    for new_index, block in enumerate(output):
        block["order"] = new_index
        block["id"] = f"block-{new_index + 1:05d}"
    return output


def _walk_refs(
    children: list[dict[str, Any]],
    indexes: dict[str, list[dict[str, Any]]],
    seen: set[str],
) -> Iterable[tuple[str, dict[str, Any]]]:
    for child in children:
        ref = str(child.get("$ref", ""))
        if not ref or ref in seen:
            continue
        seen.add(ref)
        match = re.match(r"^#/([^/]+)/(\d+)$", ref)
        if not match:
            continue
        collection, raw_index = match.groups()
        items = indexes.get(collection, [])
        index = int(raw_index)
        if index >= len(items):
            continue
        item = items[index]
        if collection == "groups":
            yield from _walk_refs(item.get("children", []) or [], indexes, seen)
        else:
            yield ref, item


def _docling_table_rows(table: dict[str, Any]) -> tuple[list[str], list[list[str]]]:
    data = table.get("data") or {}
    row_count = int(data.get("num_rows") or 0)
    column_count = int(data.get("num_cols") or 0)
    grid = [["" for _ in range(column_count)] for _ in range(row_count)]
    header_rows: set[int] = set()
    for cell in data.get("table_cells", []) or []:
        row = int(cell.get("start_row_offset_idx") or 0)
        column = int(cell.get("start_col_offset_idx") or 0)
        if row < row_count and column < column_count:
            grid[row][column] = str(cell.get("text", "")).strip()
            if cell.get("column_header") or cell.get("row_header"):
                header_rows.add(row)
    header_index = max(header_rows) if header_rows else 0
    return (grid[header_index] if grid else [], grid[header_index + 1 :] if grid else [])


def _insert_docling_pictures(
    blocks: list[dict[str, Any]],
    document_data: dict[str, Any],
    fallback_section_path: list[str],
    consumed_picture_refs: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Place DOCX pictures beside their Docling parent instead of at EOF.

    Docling's DOCX picture records retain a ``parent.$ref`` pointing to the
    nearby caption/heading/text item. Native paragraph reconciliation retains
    the same text reference, which gives us a stable structural anchor even
    when Word itself has no page provenance. Unmatched generated DrawingML
    pictures remain available at the end and are explicitly marked as such.
    """
    by_parent: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unmatched: list[dict[str, Any]] = []
    consumed_picture_refs = consumed_picture_refs or set()
    for index, picture in enumerate(document_data.get("pictures", []) or []):
        picture_ref = str(picture.get("self_ref") or f"#/pictures/{index}")
        if picture_ref in consumed_picture_refs:
            continue
        prov = picture.get("prov", []) or []
        pages = sorted({int(item["page_no"]) for item in prov if isinstance(item.get("page_no"), (int, float))})
        parent_ref = str((picture.get("parent") or {}).get("$ref") or "")
        payload = {
            "picture": picture,
            "index": index,
            "pages": pages,
            "parent_ref": parent_ref,
        }
        if parent_ref:
            by_parent[parent_ref].append(payload)
        else:
            unmatched.append(payload)

    output: list[dict[str, Any]] = []
    matched_parents: set[str] = set()

    def append_picture(item: dict[str, Any], section_path: list[str], matched: bool) -> None:
        picture = item["picture"]
        parent_ref = item["parent_ref"]
        output.append(
            _block(
                len(output),
                "picture",
                section_path=section_path,
                source_refs=[str(picture.get("self_ref") or f"#/pictures/{item['index']}")],
                page_numbers=item["pages"],
                bbox=(picture.get("prov") or [{}])[0].get("bbox") if picture.get("prov") else None,
                asset_uri=(picture.get("image") or {}).get("uri"),
                native={
                    "source": "docling_parent_alignment",
                    "anchor_ref": parent_ref or None,
                    "anchor_matched": matched,
                },
            )
        )

    for block in blocks:
        block = dict(block)
        block["id"] = f"block-{len(output) + 1:05d}"
        block["order"] = len(output)
        output.append(block)
        for ref in block.get("source_refs", []):
            anchored = by_parent.get(str(ref), [])
            if not anchored:
                continue
            matched_parents.add(str(ref))
            for item in anchored:
                append_picture(item, list(block.get("section_path", [])), True)

    for parent_ref, items in by_parent.items():
        if parent_ref in matched_parents:
            continue
        unmatched.extend(items)
    for item in unmatched:
        append_picture(item, list(fallback_section_path), False)
    return output


def _attach_integrity(
    blocks: list[dict[str, Any]],
    semantic_report: dict[str, Any],
    visual_report: dict[str, Any],
) -> None:
    findings = [*(semantic_report.get("findings", []) or []), *(visual_report.get("findings", []) or [])]
    by_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for finding in findings:
        for ref in [*(finding.get("source_refs", []) or []), *(finding.get("source_table_refs", []) or [])]:
            by_ref[str(ref)].append(finding)
    for block in blocks:
        related = []
        for ref in block.get("source_refs", []):
            related.extend(by_ref.get(str(ref), []))
        if related:
            block["integrity_finding_ids"] = [item["id"] for item in related if item.get("id")]
            if any(item.get("blocks_llm_readiness") or item.get("status") == "review_required" for item in related):
                block["integrity_status"] = "review_required"
                block["ambiguity_reasons"] = list(dict.fromkeys([
                    *block.get("ambiguity_reasons", []),
                    *(str(item.get("category", "integrity_finding")) for item in related),
                ]))


def _ambiguity_candidates(
    blocks: list[dict[str, Any]],
    semantic_report: dict[str, Any],
    visual_report: dict[str, Any],
    picture_records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    record_by_ref = {str(item.get("source_ref", "")): item for item in picture_records}
    findings_by_ref: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for finding in visual_report.get("findings", []) or []:
        for ref in finding.get("source_refs", []) or []:
            findings_by_ref[str(ref)].append(finding)
    for block in blocks:
        if block["type"] == "picture":
            ref = str((block.get("source_refs") or [""])[0])
            record = record_by_ref.get(ref, {})
            findings = findings_by_ref.get(ref, [])
            if not record and not findings:
                continue
            record_headings = [
                str(item.get("text", "")).strip()
                for item in [*(record.get("context_items", []) or []), *(record.get("items", []) or [])]
                if item.get("label") == "section_header" and item.get("text")
            ]
            picture_file = record.get("picture_file") or _picture_name_from_ref(ref)
            candidates.append({
                "id": f"ai-{len(candidates) + 1:04d}",
                "kind": "diagram",
                "block_id": block["id"],
                "source_refs": block.get("source_refs", []),
                "page_numbers": block.get("page_numbers", []),
                "section_path": list(dict.fromkeys(record_headings)) or block.get("section_path", []),
                "picture_file": picture_file,
                "source_evidence_path": f"pictures/{picture_file}" if picture_file else None,
                "asset_uri": block.get("asset_uri"),
                "ocr_hint": record.get("text", ""),
                "ambiguity_reasons": [str(item.get("category")) for item in findings] or ["content_bearing_picture"],
                "question": "Extract only visible diagram labels, components, directed relationships, and explicit architecture decisions.",
            })
        elif block.get("integrity_status") in {"ambiguous", "review_required"}:
            candidates.append({
                "id": f"ai-{len(candidates) + 1:04d}",
                "kind": "structural_ambiguity",
                "block_id": block["id"],
                "source_refs": block.get("source_refs", []),
                "page_numbers": block.get("page_numbers", []),
                "section_path": block.get("section_path", []),
                "text": block.get("text", ""),
                "native": block.get("native", {}),
                "ambiguity_reasons": block.get("ambiguity_reasons", []),
                "question": "Classify this block and resolve only structure directly supported by the supplied evidence.",
            })
    known_refs = {ref for item in candidates for ref in item.get("source_refs", [])}
    for finding in semantic_report.get("findings", []) or []:
        refs = [*(finding.get("source_refs", []) or []), *(finding.get("source_table_refs", []) or [])]
        if finding.get("status") != "review_required" or any(str(ref) in known_refs for ref in refs):
            continue
        candidates.append({
            "id": f"ai-{len(candidates) + 1:04d}",
            "kind": "pdf_semantic_ambiguity",
            "finding_id": finding.get("id"),
            "source_refs": [str(ref) for ref in refs],
            "page_numbers": finding.get("pages", []),
            "ambiguity_reasons": [finding.get("category")],
            "text": finding.get("message", ""),
            "question": "Resolve the table or layout ambiguity only when the visible evidence proves the correction.",
        })
    return candidates


def _block(index: int, block_type: str, **values: Any) -> dict[str, Any]:
    payload = {
        "id": f"block-{index + 1:05d}",
        "order": index,
        "type": block_type,
        "text": values.pop("text", ""),
        "level": values.pop("level", None),
        "language": values.pop("language", None),
        "section_path": list(values.pop("section_path", [])),
        "source_refs": list(values.pop("source_refs", [])),
        "page_numbers": list(values.pop("page_numbers", [])),
        "bbox": values.pop("bbox", None),
        "provenance_scope": "page" if values.get("page_numbers") else "section_only",
        "integrity_status": values.pop("integrity_status", "verified"),
        "integrity_finding_ids": [],
        "ambiguity_reasons": list(values.pop("ambiguity_reasons", [])),
    }
    payload.update(values)
    payload["provenance_scope"] = "page" if payload["page_numbers"] else "section_only" if payload["section_path"] or payload["source_refs"] else "unavailable"
    return payload


def _docling_text_refs(document_data: dict[str, Any]) -> dict[str, deque[str]]:
    result: dict[str, deque[str]] = defaultdict(deque)
    for index, item in enumerate(document_data.get("texts", []) or []):
        text = str(item.get("text", ""))
        result[_signature(text)].append(str(item.get("self_ref") or f"#/texts/{index}"))
    return result


def _docling_table_refs(document_data: dict[str, Any]) -> dict[str, deque[str]]:
    result: dict[str, deque[str]] = defaultdict(deque)
    for index, item in enumerate(document_data.get("tables", []) or []):
        headers, rows = _docling_table_rows(item)
        result[_grid_signature([headers, *rows])].append(
            str(item.get("self_ref") or f"#/tables/{index}")
        )
    return result


def _grid_signature(rows: list[list[str]]) -> str:
    return _signature("\n".join("\t".join(str(cell) for cell in row) for row in rows))


def _signature(text: str) -> str:
    return re.sub(r"\W+", "", text.casefold())


def _code_language(text: str) -> str:
    lowered = text.casefold()
    if "apiversion:" in lowered or "kind:" in lowered:
        return "yaml"
    if "services:" in lowered or YAML_KEY_RE.search(text):
        return "yaml"
    if "pipeline" in lowered or "stage(" in lowered:
        return "groovy"
    if lowered.startswith(("docker ", "kubectl ", "#!/")):
        return "bash"
    return "text"


def _picture_name_from_ref(ref: str) -> str | None:
    match = re.search(r"/(\d+)$", ref)
    return f"picture-{int(match.group(1)) + 1:04d}.png" if match else None


def _escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", "<br>")


def stable_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
