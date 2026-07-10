from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

COMMON_MARKDOWN_ENTITIES = {
    "&amp;": "&",
    "&lt;": "<",
    "&gt;": ">",
    "&nbsp;": " ",
}

FALSE_HEADING_LABELS = {
    "width",
    "depth",
    "range",
    "claim",
    "status",
    "date",
}

MAX_HEADING_LEVEL = 6

TABLE_HEADER_PAIRS = {
    ("Category", "Requirement"): {
        "Performance",
        "Scalability",
        "Security",
        "Usability",
        "Reliability",
        "Backup & Recovery",
        "Compliance",
    },
    ("Component", "Specification"): {
        "Database",
        "Application Stack",
        "Authentication",
        "Environment",
        "# of Virtual Machines (VMs)",
        "CPU Configuration",
        "Memory (RAM)",
        "Disk Size",
        "Operating System",
        "Storage Type",
    },
}

MIN_OCR_AREA = 35_000
MIN_OCR_DIMENSION = 50
PICTURE_OCR_TIMEOUT_SECONDS = 20
MAX_OCR_PICTURES = 40


@dataclass(frozen=True)
class HeadingContext:
    levels: dict[str, tuple[int, ...]]
    furniture: frozenset[str]


@dataclass(frozen=True)
class MarkdownTableBlock:
    start: int
    end: int
    header: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


def normalize_markdown_export(markdown: str) -> str:
    """Clean common HTML entities from Docling Markdown."""
    for escaped, plain in COMMON_MARKDOWN_ENTITIES.items():
        markdown = markdown.replace(escaped, plain)
    return markdown


def clean_markdown_for_llm(
    markdown: str,
    document_data: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
    table_repair_records: list[dict[str, Any]] | None = None,
) -> str:
    """Apply conservative Markdown cleanup for downstream LLM extraction."""
    warning_list = warnings if warnings is not None else []
    markdown = normalize_markdown_export(markdown)
    try:
        heading_context = _build_heading_context(document_data)
        markdown = _clean_heading_lines(markdown, heading_context)
    except Exception as exc:
        warning_list.append(f"Heading cleanup skipped after unexpected error: {exc}")
    try:
        markdown = _repair_native_table_continuations(
            markdown,
            document_data,
            warning_list,
            table_repair_records,
        )
    except Exception as exc:
        warning_list.append(
            f"Continued-table repair skipped after unexpected error: {exc}"
        )
    try:
        markdown = _repair_borderless_tables(markdown)
    except Exception as exc:
        warning_list.append(
            f"Borderless-table repair skipped after unexpected error: {exc}"
        )
    return markdown


def inject_picture_text(markdown: str, records: list[dict[str, Any]]) -> str:
    """Insert provenance-labelled picture text after matching image references."""
    useful_records = [record for record in records if record.get("text")]
    if not useful_records:
        return markdown

    by_uri = {
        str(record["asset_uri"]): record
        for record in useful_records
        if record.get("asset_uri")
    }
    emitted: set[str] = set()
    output: list[str] = []
    emitted_headings = _markdown_heading_signatures(markdown)
    for line in markdown.splitlines():
        output.append(line)
        for asset_uri, record in by_uri.items():
            if f"]({asset_uri})" in line and asset_uri not in emitted:
                output.extend(_format_picture_text_block(record, emitted_headings))
                emitted.add(asset_uri)

    missing = [
        record for record in useful_records if record.get("asset_uri") not in emitted
    ]
    if missing:
        output.extend(["", "## Extracted Image Text"])
        for record in missing:
            output.extend(_format_picture_text_block(record, emitted_headings))

    return "\n".join(output).rstrip() + "\n"


def inject_picture_ocr(markdown: str, records: list[dict[str, Any]]) -> str:
    """Backward-compatible wrapper for older callers and tests."""
    return inject_picture_text(markdown, records)


def extract_picture_text(
    pictures_dir: Path,
    document_json_path: Path,
    output_path: Path,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Best-effort structured-first picture extraction; never raises."""
    try:
        return _extract_picture_text_impl(
            pictures_dir,
            document_json_path,
            output_path,
            warnings,
        )
    except Exception as exc:
        warnings.append(f"Picture text extraction skipped after unexpected error: {exc}")
        _write_jsonl(output_path, [])
        return []


def _extract_picture_text_impl(
    pictures_dir: Path,
    document_json_path: Path,
    output_path: Path,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Prefer Docling picture-child text and OCR only pictures lacking it."""
    records: list[dict[str, Any]] = []
    data = json.loads(document_json_path.read_text(encoding="utf-8"))
    pictures = data.get("pictures", [])
    texts_by_ref = {
        str(item.get("self_ref")): item for item in data.get("texts", [])
    }
    heading_context = _build_heading_context(data)
    ocr_candidates: list[tuple[Path, dict[str, Any]]] = []

    for index, image_path in enumerate(sorted(pictures_dir.glob("picture-*.png"))):
        meta = pictures[index] if index < len(pictures) else {}
        items = _picture_child_items(
            meta,
            texts_by_ref,
            heading_context,
            _picture_heading_ceiling(meta, data, texts_by_ref, heading_context),
        )
        if _has_useful_structured_picture_text(items):
            width, height = _picture_size(meta)
            records.append(
                {
                    "index": len(records),
                    "picture_file": image_path.name,
                    "asset_uri": _asset_uri(meta),
                    "page_number": _page_number(meta),
                    "width": width,
                    "height": height,
                    "source": "docling_structured",
                    "trust": "medium",
                    "items": items,
                    "text": "\n".join(item["text"] for item in items),
                }
            )
            continue

        width, height = _picture_size(meta)
        if _should_ocr_picture(width, height):
            ocr_candidates.append((image_path, meta))

    if len(ocr_candidates) > MAX_OCR_PICTURES:
        warnings.append(
            "Picture OCR limited to "
            f"{MAX_OCR_PICTURES} of {len(ocr_candidates)} fallback images"
        )
        ocr_candidates = ocr_candidates[:MAX_OCR_PICTURES]

    if ocr_candidates:
        tesseract = shutil.which("tesseract")
        if tesseract is None:
            warnings.append("Picture OCR fallback skipped: tesseract executable not found")
        else:
            for image_path, meta in ocr_candidates:
                record = _ocr_picture(image_path, meta, tesseract, warnings)
                if record is not None:
                    record.update(
                        {
                            "index": len(records),
                            "source": "tesseract_ocr",
                            "trust": "low",
                        }
                    )
                    records.append(record)

    _write_jsonl(output_path, records)
    return records


def _picture_child_items(
    picture: dict[str, Any],
    texts_by_ref: dict[str, dict[str, Any]],
    heading_context: HeadingContext | None,
    heading_ceiling: int | None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for child in picture.get("children", []) or []:
        item = texts_by_ref.get(str(child.get("$ref")))
        if not item:
            continue
        text = str(item.get("text", "")).strip()
        label = str(item.get("label", "text"))
        if not text or label in {"page_header", "page_footer"}:
            continue
        if heading_context and _text_signature(text) in heading_context.furniture:
            continue
        level = item.get("level")
        normalized_level = int(level) if isinstance(level, (int, float)) else None
        if normalized_level is not None and heading_ceiling is not None:
            normalized_level = min(normalized_level, heading_ceiling)
        items.append(
            {
                "self_ref": str(item.get("self_ref", "")),
                "label": label,
                "level": normalized_level,
                "text": text,
                "provenance": item.get("prov", []),
            }
        )
    return items


def _picture_heading_ceiling(
    picture: dict[str, Any],
    document_data: dict[str, Any],
    texts_by_ref: dict[str, dict[str, Any]],
    heading_context: HeadingContext | None,
) -> int | None:
    picture_ref = str(picture.get("self_ref", ""))
    body_children = (document_data.get("body") or {}).get("children", []) or []
    picture_index = next(
        (
            index
            for index, child in enumerate(body_children)
            if str(child.get("$ref", "")) == picture_ref
        ),
        None,
    )
    if picture_index is None:
        return None
    for child in reversed(body_children[:picture_index]):
        item = texts_by_ref.get(str(child.get("$ref", "")))
        if not item or item.get("label") != "section_header":
            continue
        text = str(item.get("text", ""))
        if heading_context and _text_signature(text) in heading_context.furniture:
            continue
        level = item.get("level")
        if isinstance(level, (int, float)):
            return min(int(level) + 1, MAX_HEADING_LEVEL)
    return None


def _has_useful_structured_picture_text(items: list[dict[str, Any]]) -> bool:
    if not items:
        return False
    total_chars = sum(len(str(item.get("text", "")).strip()) for item in items)
    return (
        total_chars >= 40
        or len(items) >= 4
        or any(item.get("label") in {"section_header", "code"} for item in items)
    )


def _ocr_picture(
    image_path: Path,
    meta: dict[str, Any],
    tesseract: str,
    warnings: list[str],
) -> dict[str, Any] | None:
    try:
        completed = subprocess.run(
            [tesseract, str(image_path), "stdout", "--psm", "6", "-l", "eng"],
            check=False,
            capture_output=True,
            text=True,
            timeout=PICTURE_OCR_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        warnings.append(f"Picture OCR timed out for {image_path.name}")
        return None
    if completed.returncode != 0:
        stderr = completed.stderr.strip().splitlines()
        detail = stderr[0] if stderr else "unknown tesseract error"
        warnings.append(f"Picture OCR failed for {image_path.name}: {detail}")
        return None
    text = _clean_ocr_text(completed.stdout)
    if not text:
        return None
    width, height = _picture_size(meta)
    return {
        "picture_file": image_path.name,
        "asset_uri": _asset_uri(meta),
        "page_number": _page_number(meta),
        "width": width,
        "height": height,
        "text": text,
    }


def extract_picture_ocr(
    pictures_dir: Path,
    document_json_path: Path,
    output_path: Path,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Backward-compatible name for structured-first picture extraction."""
    return extract_picture_text(
        pictures_dir,
        document_json_path,
        output_path,
        warnings,
    )


def _build_heading_context(
    document_data: dict[str, Any] | None,
) -> HeadingContext | None:
    if not document_data:
        return None

    levels: dict[str, list[int]] = defaultdict(list)
    furniture = {
        _text_signature(str(item.get("text", "")))
        for item in document_data.get("texts", [])
        if item.get("label") in {"page_header", "page_footer"}
    }
    boundary_counts: Counter[str] = Counter()
    for item in document_data.get("texts", []):
        if item.get("label") != "section_header":
            continue
        signature = _text_signature(str(item.get("text", "")))
        level = item.get("level")
        if signature and isinstance(level, (int, float)):
            levels[signature].append(int(level))
        if signature and _item_is_near_page_boundary(item, document_data):
            boundary_counts[signature] += 1

    furniture.update(
        signature for signature, count in boundary_counts.items() if count >= 2
    )
    furniture.discard("")
    return HeadingContext(
        levels={key: tuple(value) for key, value in levels.items()},
        furniture=frozenset(furniture),
    )


def _clean_heading_lines(
    markdown: str,
    context: HeadingContext | None = None,
) -> str:
    output: list[str] = []
    in_fence = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            output.append(line)
            continue
        output.append(line if in_fence else _clean_heading_line(line, context))
    return "\n".join(output)


def _clean_heading_line(
    line: str,
    context: HeadingContext | None = None,
) -> str:
    match = re.match(r"^(#{1,10})\s+(.*)$", line)
    if not match:
        return line

    hashes, text = match.groups()
    text = text.strip()

    signature = _text_signature(text)
    if context is not None and signature in context.furniture:
        return ""

    if text.lower().startswith("of "):
        return rf"\# {text}"

    text = re.sub(r"^(\d+(?:\.\d+)+)([A-Z])", r"\1 \2", text)

    parts = re.split(r"\s+(?=\d+(?:\.\d+){1,}\s+[A-Z])", text)
    if len(parts) > 1 and re.match(r"^\d+(?:\.\d+){1,}\s+", parts[0]):
        return "\n".join(_format_numbered_heading(part.strip()) for part in parts)

    numbered = _format_numbered_heading(text)
    if numbered is not None:
        return numbered

    if _is_false_label_heading(text):
        return text.lstrip("·•- ").strip()

    if _is_false_sentence_heading(text):
        return _normalize_sentence_text(text)

    level = _document_heading_level(text, context)
    if level is None:
        level = min(len(hashes), MAX_HEADING_LEVEL)
    return f"{'#' * level} {text}"


def _format_numbered_heading(text: str) -> str | None:
    match = re.match(r"^(\d+(?:\.\d+)*)(\.?)\s+(.+)$", text)
    if not match:
        return None

    number, terminal_dot, title = match.groups()
    if _looks_like_date(text):
        return None

    depth = number.count(".") + 1
    level = min(depth + 1, MAX_HEADING_LEVEL)
    marker = f"{number}{terminal_dot}"
    return f"{'#' * level} {marker} {title.strip()}"


def _document_heading_level(
    text: str,
    context: HeadingContext | None,
) -> int | None:
    if context is None:
        return None
    levels = context.levels.get(_text_signature(text), ())
    if not levels:
        return None
    counts = Counter(levels)
    return min(
        (level for level, count in counts.items() if count == max(counts.values())),
        default=None,
    )


def _text_signature(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _item_is_near_page_boundary(
    item: dict[str, Any],
    document_data: dict[str, Any],
) -> bool:
    provenance = item.get("prov") or []
    if not provenance:
        return False
    prov = provenance[0]
    page_no = prov.get("page_no")
    bbox = prov.get("bbox") or {}
    height = _page_height(document_data, page_no)
    if height is None or not isinstance(bbox.get("t"), (int, float)):
        return False
    top_gap = height - float(bbox["t"])
    bottom = float(bbox.get("b", height))
    return top_gap <= height * 0.08 or bottom <= height * 0.08


def _page_height(
    document_data: dict[str, Any],
    page_no: Any,
) -> float | None:
    pages = document_data.get("pages") or {}
    page = pages.get(str(page_no)) or pages.get(page_no) or {}
    size = page.get("size") or {}
    height = size.get("height")
    return float(height) if isinstance(height, (int, float)) else None


def _is_false_sentence_heading(text: str) -> bool:
    stripped = text.strip()
    if _looks_like_date(stripped):
        return True
    if stripped.endswith(".") and len(stripped.split()) >= 5:
        return True
    return False


def _is_false_label_heading(text: str) -> bool:
    stripped = text.strip()
    label_text = stripped.lstrip("·•- ").strip()
    has_marker = label_text != stripped
    has_colon = label_text.endswith(":")
    if not has_marker and not has_colon:
        return False

    normalized_label = label_text.rstrip(":").strip().lower()
    return normalized_label in FALSE_HEADING_LABELS


def _looks_like_date(text: str) -> bool:
    return bool(
        re.match(r"^\d{1,2}[-/][A-Za-z]{3,9}[-/]\d{2,4}$", text)
        or re.match(r"^\d{4}[-/]\d{1,2}[-/]\d{1,2}$", text)
    )


def _normalize_sentence_text(text: str) -> str:
    text = re.sub(r"\s+([.,;:])", r"\1", text.strip())
    if _looks_like_date(text):
        return text
    text = re.sub(r"\s*-\s*", " - ", text)
    return text


def _repair_native_table_continuations(
    markdown: str,
    document_data: dict[str, Any] | None,
    warnings: list[str],
    repair_records: list[dict[str, Any]] | None = None,
) -> str:
    """Conservatively join table fragments only when row continuation is explicit."""
    if not document_data or not document_data.get("tables"):
        return markdown

    lines = markdown.splitlines()
    blocks = _find_markdown_table_blocks(lines)
    tables = document_data.get("tables", [])
    if len(blocks) != len(tables):
        warnings.append(
            "Native table continuation analysis skipped because Markdown and "
            f"document table counts differ ({len(blocks)} vs {len(tables)})"
        )
        return markdown

    replacements: dict[int, list[str]] = {}
    suppressed: set[int] = set()
    index = 0
    while index + 1 < len(blocks):
        first = blocks[index]
        second = blocks[index + 1]
        first_meta = tables[index]
        second_meta = tables[index + 1]
        if not _looks_like_continued_table(
            first,
            second,
            first_meta,
            second_meta,
            document_data,
        ):
            index += 1
            continue

        if not first.rows or not second.rows:
            index += 1
            continue
        left_row = first.rows[-1]
        right_row = second.rows[0]
        page_a = _table_page(first_meta)
        page_b = _table_page(second_meta)
        if not _rows_have_explicit_continuation(left_row, right_row):
            index += 1
            continue

        merged_row = tuple(
            _merge_continued_cell(left, right)
            for left, right in zip(left_row, right_row, strict=True)
        )
        merged_rows = (*first.rows[:-1], merged_row, *second.rows[1:])
        replacements[first.start] = _render_markdown_table(first.header, merged_rows)
        suppressed.update(range(first.start, first.end))
        suppressed.update(range(second.start, second.end))
        if repair_records is not None:
            repair_records.append(
                {
                    "repair_index": len(repair_records) + 1,
                    "table_indexes": [index, index + 1],
                    "table_numbers": [index + 1, index + 2],
                    "pages": [page_a, page_b],
                    "headers": list(first.header),
                    "merged_row": list(merged_row),
                    "source": "native_table_continuation",
                }
            )
        warnings.append(
            f"Repaired a continued table spanning pages {page_a} and {page_b}"
        )
        index += 2

    if not replacements:
        return markdown

    output: list[str] = []
    for line_index, line in enumerate(lines):
        replacement = replacements.get(line_index)
        if replacement is not None:
            output.extend(replacement)
        if line_index not in suppressed:
            output.append(line)
    return "\n".join(output).rstrip() + "\n"


def _find_markdown_table_blocks(lines: list[str]) -> list[MarkdownTableBlock]:
    blocks: list[MarkdownTableBlock] = []
    in_fence = False
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            index += 1
            continue
        if in_fence or not line.lstrip().startswith("|"):
            index += 1
            continue

        start = index
        table_lines: list[str] = []
        while index < len(lines) and lines[index].lstrip().startswith("|"):
            table_lines.append(lines[index].strip())
            index += 1
        parsed = [_parse_markdown_table_row(row) for row in table_lines]
        if len(parsed) < 2 or not _is_separator_row(parsed[1]):
            continue
        width = len(parsed[0])
        rows = [tuple(row) for row in parsed[2:] if len(row) == width]
        blocks.append(
            MarkdownTableBlock(
                start=start,
                end=index,
                header=tuple(parsed[0]),
                rows=tuple(rows),
            )
        )
    return blocks


def _parse_markdown_table_row(line: str) -> list[str]:
    body = line.strip().strip("|")
    return [
        cell.replace(r"\|", "|").strip()
        for cell in re.split(r"(?<!\\)\|", body)
    ]


def _is_separator_row(row: list[str]) -> bool:
    return bool(row) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in row)


def _looks_like_continued_table(
    first: MarkdownTableBlock,
    second: MarkdownTableBlock,
    first_meta: dict[str, Any],
    second_meta: dict[str, Any],
    document_data: dict[str, Any],
) -> bool:
    if len(first.header) != len(second.header):
        return False
    header_a = "|".join(_text_signature(cell) for cell in first.header)
    header_b = "|".join(_text_signature(cell) for cell in second.header)
    if not header_a or SequenceMatcher(None, header_a, header_b).ratio() < 0.95:
        return False

    page_a = _table_page(first_meta)
    page_b = _table_page(second_meta)
    if page_a is None or page_b is None or not 1 <= page_b - page_a <= 2:
        return False
    height_a = _page_height(document_data, page_a)
    height_b = _page_height(document_data, page_b)
    bbox_a = _table_bbox(first_meta)
    bbox_b = _table_bbox(second_meta)
    if not height_a or not height_b or not bbox_a or not bbox_b:
        return False
    return (
        float(bbox_a.get("b", height_a)) <= height_a * 0.12
        and float(bbox_b.get("t", 0)) >= height_b * 0.85
    )


def _rows_have_explicit_continuation(
    left: tuple[str, ...],
    right: tuple[str, ...],
) -> bool:
    if len(left) != len(right) or not left:
        return False
    if not right[0].strip():
        return True
    first_left = left[0].rstrip()
    return first_left.endswith(("_", "-", "/", "\\"))


def _merge_continued_cell(left: str, right: str) -> str:
    left = left.strip()
    right = right.strip()
    if not left:
        return right
    if not right:
        return left
    if left.endswith(("_", "-", "/", "\\")):
        return f"{left}{right}"
    if (
        " " not in left
        and " " not in right
        and left.isupper()
        and right.isupper()
        and len(right) <= 3
    ):
        return f"{left}{right}"
    return f"{left} {right}"


def _render_markdown_table(
    header: tuple[str, ...],
    rows: tuple[tuple[str, ...], ...],
) -> list[str]:
    return [
        "| " + " | ".join(_escape_table_cell(cell) for cell in header) + " |",
        "| " + " | ".join("---" for _ in header) + " |",
        *[
            "| " + " | ".join(_escape_table_cell(cell) for cell in row) + " |"
            for row in rows
        ],
    ]


def _table_page(table: dict[str, Any]) -> int | None:
    prov = table.get("prov") or []
    page = prov[0].get("page_no") if prov else None
    return int(page) if isinstance(page, (int, float)) else None


def _table_bbox(table: dict[str, Any]) -> dict[str, Any] | None:
    prov = table.get("prov") or []
    bbox = prov[0].get("bbox") if prov else None
    return bbox if isinstance(bbox, dict) else None


def _repair_borderless_tables(markdown: str) -> str:
    lines = markdown.splitlines()
    output: list[str] = []
    i = 0
    while i < len(lines):
        current = lines[i].strip()
        matched_pair = None
        next_index = _next_nonempty(lines, i + 1)
        if next_index is not None:
            for pair in TABLE_HEADER_PAIRS:
                if current == pair[0] and lines[next_index].strip() == pair[1]:
                    matched_pair = pair
                    break

        if matched_pair is None:
            output.append(lines[i])
            i += 1
            continue

        end_index, cells = _collect_table_cells(lines, next_index + 1)
        table = _format_repaired_table(matched_pair, cells)
        if table is None:
            output.append(lines[i])
            i += 1
            continue

        if output and output[-1].strip():
            output.append("")
        output.extend(table)
        output.append("")
        i = end_index

    return "\n".join(output).rstrip() + "\n"


def _next_nonempty(lines: list[str], start: int) -> int | None:
    for index in range(start, len(lines)):
        if lines[index].strip():
            return index
    return None


def _collect_table_cells(lines: list[str], start: int) -> tuple[int, list[str]]:
    cells: list[str] = []
    index = start
    while index < len(lines):
        stripped = lines[index].strip()
        if not stripped:
            index += 1
            continue
        if _starts_new_block(stripped):
            break
        cells.append(stripped.replace(r"\# ", "# "))
        index += 1
    return index, cells


def _starts_new_block(stripped: str) -> bool:
    if stripped.startswith("!["):
        return True
    if re.match(r"^#{1,6}\s+", stripped):
        return True
    if stripped.startswith("|"):
        return True
    return False


def _format_repaired_table(
    headers: tuple[str, str],
    cells: list[str],
) -> list[str] | None:
    expanded = _expand_fused_cells(headers, cells)
    if len(expanded) < 4 or len(expanded) % 2 != 0:
        return None

    rows = [expanded[index : index + 2] for index in range(0, len(expanded), 2)]
    return [
        f"| {headers[0]} | {headers[1]} |",
        "| --- | --- |",
        *[f"| {_escape_table_cell(left)} | {_escape_table_cell(right)} |" for left, right in rows],
    ]


def _expand_fused_cells(headers: tuple[str, str], cells: list[str]) -> list[str]:
    labels = sorted(TABLE_HEADER_PAIRS[headers], key=len, reverse=True)
    expanded: list[str] = []
    for cell in cells:
        split = _split_fused_cell(cell, labels)
        expanded.extend(split if split is not None else [cell])
    return expanded


def _split_fused_cell(cell: str, labels: list[str]) -> list[str] | None:
    for label in labels:
        prefix = f"{label} "
        if cell.startswith(prefix) and len(cell) > len(prefix):
            return [label, cell[len(prefix) :].strip()]
    return None


def _escape_table_cell(value: str) -> str:
    return value.replace("|", r"\|")


def _format_picture_text_block(
    record: dict[str, Any],
    emitted_headings: set[str],
) -> list[str]:
    page = record.get("page_number")
    picture = record.get("picture_file")
    location: list[str] = []
    if page is not None:
        location.append(f"page {page}")
    if picture:
        location.append(str(picture))
    source = record.get("source", "tesseract_ocr")
    if source == "docling_structured":
        label = "MEDIUM-TRUST DOCLING PICTURE TEXT"
        explanation = (
            "This text was structurally extracted from a detected visual region. "
            "It preserves Docling provenance but may still contain OCR or reading-order "
            "errors. Use it only as supporting visual context, not as authoritative "
            "requirements text."
        )
    else:
        label = "LOW-TRUST IMAGE OCR"
        explanation = (
            "This text was OCR-extracted from an image and may contain recognition "
            "errors. Use it only as supporting visual context, not as authoritative "
            "requirements text."
        )
    if location:
        label += f" - {', '.join(location)}"

    output: list[str] = []
    body_items = record.get("items") or []
    if source == "docling_structured" and body_items:
        body_lines: list[str] = []
        for item in body_items:
            text = str(item.get("text", "")).strip()
            if not text:
                continue
            if item.get("label") == "section_header":
                signature = _text_signature(text)
                if signature not in emitted_headings:
                    heading = _format_numbered_heading(text)
                    if heading is None:
                        level = item.get("level")
                        level = (
                            min(max(int(level), 1), MAX_HEADING_LEVEL)
                            if isinstance(level, (int, float))
                            else MAX_HEADING_LEVEL
                        )
                        heading = f"{'#' * level} {text}"
                    output.extend(["", heading])
                    emitted_headings.add(signature)
                continue
            body_lines.extend(text.splitlines())
        text_lines = body_lines
    else:
        text_lines = str(record.get("text", "")).strip().splitlines()

    quoted = [f"> {line}" if line else ">" for line in text_lines]
    output.extend(
        [
            "",
            f"> {label}:",
            f"> {explanation}",
            *quoted,
        ]
    )
    return output


def _format_ocr_block(record: dict[str, Any]) -> list[str]:
    """Compatibility wrapper for callers using the previous private helper."""
    return _format_picture_text_block(record, set())


def _markdown_heading_signatures(markdown: str) -> set[str]:
    signatures: set[str] = set()
    in_fence = False
    for line in markdown.splitlines():
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^#{1,6}\s+(.+)$", line)
        if match:
            signatures.add(_text_signature(match.group(1)))
    return signatures


def _picture_size(meta: dict[str, Any]) -> tuple[int | None, int | None]:
    size = ((meta.get("image") or {}).get("size") or {})
    width = size.get("width")
    height = size.get("height")
    return (
        int(width) if isinstance(width, (int, float)) else None,
        int(height) if isinstance(height, (int, float)) else None,
    )


def _should_ocr_picture(width: int | None, height: int | None) -> bool:
    if width is None or height is None:
        return False
    return (
        width * height >= MIN_OCR_AREA
        and min(width, height) >= MIN_OCR_DIMENSION
    )


def _asset_uri(meta: dict[str, Any]) -> str | None:
    uri = (meta.get("image") or {}).get("uri")
    return str(uri) if uri else None


def _page_number(meta: dict[str, Any]) -> int | None:
    prov = meta.get("prov") or []
    if not prov:
        return None
    page_no = prov[0].get("page_no")
    return int(page_no) if isinstance(page_no, (int, float)) else None


def _clean_ocr_text(text: str) -> str:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines)


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(
            json.dumps(record, ensure_ascii=False, default=str) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )
