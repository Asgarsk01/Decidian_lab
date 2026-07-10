from __future__ import annotations

import json
import re
import shutil
import subprocess
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


def normalize_markdown_export(markdown: str) -> str:
    """Clean common HTML entities from Docling Markdown."""
    for escaped, plain in COMMON_MARKDOWN_ENTITIES.items():
        markdown = markdown.replace(escaped, plain)
    return markdown


def clean_markdown_for_llm(markdown: str) -> str:
    """Apply conservative Markdown cleanup for downstream LLM extraction."""
    markdown = normalize_markdown_export(markdown)
    markdown = _clean_heading_lines(markdown)
    markdown = _repair_borderless_tables(markdown)
    return markdown


def inject_picture_ocr(markdown: str, records: list[dict[str, Any]]) -> str:
    """Insert OCR text after matching Markdown image references."""
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
    for line in markdown.splitlines():
        output.append(line)
        for asset_uri, record in by_uri.items():
            if f"]({asset_uri})" in line and asset_uri not in emitted:
                output.extend(_format_ocr_block(record))
                emitted.add(asset_uri)

    missing = [
        record for record in useful_records if record.get("asset_uri") not in emitted
    ]
    if missing:
        output.extend(["", "## Extracted Image Text"])
        for record in missing:
            output.extend(_format_ocr_block(record))

    return "\n".join(output).rstrip() + "\n"


def extract_picture_ocr(
    pictures_dir: Path,
    document_json_path: Path,
    output_path: Path,
    warnings: list[str],
) -> list[dict[str, Any]]:
    """Best-effort OCR for large exported pictures; never raises to callers."""
    records: list[dict[str, Any]] = []
    try:
        tesseract = shutil.which("tesseract")
        if tesseract is None:
            warnings.append("Picture OCR skipped: tesseract executable not found")
            _write_jsonl(output_path, records)
            return records

        metadata = _load_picture_metadata(document_json_path)
        candidates: list[tuple[Path, dict[str, Any]]] = []
        for index, image_path in enumerate(sorted(pictures_dir.glob("picture-*.png"))):
            meta = metadata[index] if index < len(metadata) else {}
            width, height = _picture_size(meta)
            if not _should_ocr_picture(width, height):
                continue
            candidates.append((image_path, meta))

        if len(candidates) > MAX_OCR_PICTURES:
            warnings.append(
                "Picture OCR limited to "
                f"{MAX_OCR_PICTURES} of {len(candidates)} candidate images"
            )
            candidates = candidates[:MAX_OCR_PICTURES]

        for image_path, meta in candidates:
            try:
                completed = subprocess.run(
                    [
                        tesseract,
                        str(image_path),
                        "stdout",
                        "--psm",
                        "6",
                        "-l",
                        "eng",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=PICTURE_OCR_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                warnings.append(f"Picture OCR timed out for {image_path.name}")
                continue

            if completed.returncode != 0:
                stderr = completed.stderr.strip().splitlines()
                detail = stderr[0] if stderr else "unknown tesseract error"
                warnings.append(f"Picture OCR failed for {image_path.name}: {detail}")
                continue

            text = _clean_ocr_text(completed.stdout)
            if not text:
                continue

            width, height = _picture_size(meta)
            records.append(
                {
                    "index": len(records),
                    "picture_file": image_path.name,
                    "asset_uri": _asset_uri(meta),
                    "page_number": _page_number(meta),
                    "width": width,
                    "height": height,
                    "text": text,
                }
            )
    except Exception as exc:
        warnings.append(f"Picture OCR skipped after unexpected error: {exc}")

    _write_jsonl(output_path, records)
    return records


def _clean_heading_lines(markdown: str) -> str:
    return "\n".join(_clean_heading_line(line) for line in markdown.splitlines())


def _clean_heading_line(line: str) -> str:
    match = re.match(r"^(#{1,10})\s+(.*)$", line)
    if not match:
        return line

    hashes, text = match.groups()
    text = text.strip()

    if text.lower().startswith("of "):
        return rf"\# {text}"

    text = re.sub(r"^(\d+(?:\.\d+)+)([A-Z])", r"\1 \2", text)

    parts = re.split(r"\s+(?=\d+(?:\.\d+){1,}\s+[A-Z])", text)
    if len(parts) > 1 and re.match(r"^\d+(?:\.\d+){1,}\s+", parts[0]):
        level = min(len(hashes), 4)
        return "\n".join(f"{'#' * level} {part.strip()}" for part in parts)

    normalized_label = text.lstrip("·•- ").rstrip(":").strip().lower()
    if normalized_label in FALSE_HEADING_LABELS:
        return text.lstrip("·•- ").strip()

    level = min(len(hashes), 4)
    return f"{'#' * level} {text}"


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


def _format_ocr_block(record: dict[str, Any]) -> list[str]:
    page = record.get("page_number")
    picture = record.get("picture_file")
    label = f"Image OCR"
    if page is not None:
        label += f", page {page}"
    if picture:
        label += f", {picture}"
    text = str(record.get("text", "")).strip()
    quoted = [f"> {line}" if line else ">" for line in text.splitlines()]
    return ["", f"> {label}:", *quoted]


def _load_picture_metadata(document_json_path: Path) -> list[dict[str, Any]]:
    data = json.loads(document_json_path.read_text(encoding="utf-8"))
    pictures = data.get("pictures", [])
    return pictures if isinstance(pictures, list) else []


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
