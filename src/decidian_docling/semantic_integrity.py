from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any

TABLE_INTEGRITY_WARNING = (
    "SEMANTIC INTEGRITY WARNING: Table column semantics are unresolved for "
    "this content. Do not infer column meanings from positional or numeric "
    "headers without reviewing the source table evidence."
)
INTEGRITY_WARNING_PREFIX = TABLE_INTEGRITY_WARNING
IMAGE_INTEGRITY_WARNING = (
    "SEMANTIC INTEGRITY WARNING: This image has no verified text coverage. "
    "Do not infer requirements, decisions, labels, or relationships from it "
    "without reviewing the source image."
)
OCR_INTEGRITY_WARNING = (
    "SEMANTIC INTEGRITY WARNING: This image text was recovered by low-trust "
    "OCR only. Do not treat it as verified requirements or decision evidence "
    "without reviewing the source image."
)


@dataclass(frozen=True)
class TableSnapshot:
    index: int
    number: int
    self_ref: str
    page: int | None
    bbox: dict[str, Any] | None
    num_rows: int
    num_cols: int
    rows: tuple[tuple[str, ...], ...]
    header_rows: int
    header: tuple[str, ...]
    header_is_credible: bool
    has_numeric_synthetic_header: bool
    column_edges: tuple[tuple[float, float], ...]


def apply_pdf_semantic_integrity(
    document: Any,
    document_data: dict[str, Any],
    warnings: list[str],
) -> tuple[Any, dict[str, Any]]:
    """Repair a deep-copied PDF document when table evidence is unambiguous."""
    try:
        shadow_document = copy.deepcopy(document)
        snapshots = _build_table_snapshots(document_data)
        table_items = _table_items_by_ref(shadow_document)
        findings = _analyze_and_repair(snapshots, table_items, document_data)
        report = build_integrity_report("pdf", findings)
        if report["summary"]["review_required"]:
            warnings.append(
                "PDF semantic integrity requires review for "
                f"{report['summary']['review_required']} finding(s)"
            )
        if report["summary"]["repaired_high_confidence"]:
            warnings.append(
                "PDF semantic integrity repaired "
                f"{report['summary']['repaired_high_confidence']} table finding(s)"
            )
        return shadow_document, report
    except Exception as exc:
        warnings.append(
            f"PDF semantic integrity layer skipped after unexpected error: {exc}"
        )
        return document, build_integrity_report(
            "pdf",
            [
                {
                    "id": "si-0001",
                    "category": "integrity_layer_exception",
                    "status": "preserved",
                    "message": "Semantic integrity layer failed open; baseline Docling artifacts were preserved.",
                    "rationale": [str(exc)],
                    "table_indexes": [],
                    "table_numbers": [],
                    "source_table_refs": [],
                    "pages": [],
                    "affected_artifacts": [],
                    "source_refs": [],
                    "provenance_scope": "unavailable",
                    "llm_warning": (
                        "SEMANTIC INTEGRITY WARNING: Integrity analysis failed. "
                        "Do not use this parse for unattended decision extraction."
                    ),
                    "blocks_llm_readiness": True,
                }
            ],
        )


def annotate_chunks_with_integrity(
    chunks: list[dict[str, Any]],
    report: dict[str, Any],
) -> None:
    findings = report.get("findings") or []
    by_ref: dict[str, list[dict[str, Any]]] = {}
    for finding in findings:
        for ref in (
            finding.get("source_refs")
            or finding.get("source_table_refs")
            or []
        ):
            by_ref.setdefault(str(ref), []).append(finding)

    for chunk in chunks:
        source_refs = chunk.get("source_refs") or []
        related: list[dict[str, Any]] = []
        for source_ref in source_refs:
            related.extend(by_ref.get(str(source_ref.get("self_ref", "")), []))
        statuses = [str(item.get("status", "")) for item in related]
        if any(item.get("blocks_llm_readiness") for item in related):
            statuses.append("review_required")
        status = _highest_status(statuses) if statuses else "verified"
        chunk["integrity_status"] = status
        chunk["integrity_finding_ids"] = [
            str(item["id"]) for item in related if item.get("id")
        ]
        if status == "review_required":
            warning_text = "\n\n".join(
                dict.fromkeys(
                    str(item.get("llm_warning") or TABLE_INTEGRITY_WARNING)
                    for item in related
                    if item.get("blocks_llm_readiness")
                    or item.get("status") == "review_required"
                )
            )
            if not warning_text:
                warning_text = TABLE_INTEGRITY_WARNING
            contextualized = str(chunk.get("contextualized_text", ""))
            if not contextualized.startswith(warning_text):
                chunk["contextualized_text"] = (
                    f"{warning_text}\n\n{contextualized}"
                )
            text = str(chunk.get("text", ""))
            if not text.startswith(warning_text):
                chunk["text"] = f"{warning_text}\n\n{text}"


def empty_integrity_report(scope: str) -> dict[str, Any]:
    return build_integrity_report(scope, [])


def build_integrity_report(scope: str, findings: list[dict[str, Any]]) -> dict[str, Any]:
    status_order = (
        "verified",
        "repaired_high_confidence",
        "review_required",
        "preserved",
    )
    summary = {status: 0 for status in status_order}
    for finding in findings:
        status = str(finding.get("status", "preserved"))
        summary[status] = summary.get(status, 0) + 1
    return {
        "schema_version": "1.0",
        "scope": scope,
        "llm_readiness": (
            "review_required"
            if summary.get("review_required", 0) > 0
            or any(finding.get("blocks_llm_readiness") for finding in findings)
            else "ready"
        ),
        "summary": summary,
        "findings": findings,
    }


def add_picture_integrity_findings(
    report: dict[str, Any],
    coverage: list[dict[str, Any]],
) -> dict[str, Any]:
    """Add format-neutral picture coverage findings without altering source data."""
    findings = list(report.get("findings") or [])
    for item in coverage:
        if not item.get("qualifying"):
            continue
        status = str(item.get("coverage_status") or "unknown")
        if status == "structured_text":
            continue
        recovered_by_ocr = status == "ocr_text"
        finding = {
            "id": f"si-{len(findings) + 1:04d}",
            "category": (
                "low_trust_picture_ocr"
                if recovered_by_ocr
                else "unverified_picture_text"
            ),
            "status": "review_required",
            "message": (
                "Picture text was recovered by low-trust OCR but is not verified semantic evidence."
                if recovered_by_ocr
                else "Qualifying embedded picture has no verified text coverage."
            ),
            "rationale": [
                "The picture met the local image-text qualification threshold.",
                (
                    "Tesseract output can be retained as evidence, but does not prove "
                    "diagram labels or relationships were recovered correctly."
                    if recovered_by_ocr
                    else f"Picture text coverage outcome: {status}."
                ),
            ],
            "source_refs": [str(item["source_ref"])] if item.get("source_ref") else [],
            "source_table_refs": [],
            "pages": [item["page_number"]]
            if isinstance(item.get("page_number"), int)
            else [],
            "provenance_scope": (
                "page" if isinstance(item.get("page_number"), int) else "unavailable"
            ),
            "affected_artifacts": [
                "document.md",
                "picture_text.jsonl",
                "picture_chunks.jsonl",
                "visual_integrity.json",
            ],
            "picture_file": item.get("picture_file"),
            "asset_uri": item.get("asset_uri"),
            "coverage_status": status,
            "llm_warning": (
                OCR_INTEGRITY_WARNING if recovered_by_ocr else IMAGE_INTEGRITY_WARNING
            ),
            "blocks_llm_readiness": True,
            "requires_standalone_warning": not recovered_by_ocr,
        }
        findings.append(finding)
    return build_integrity_report(str(report.get("scope") or "document"), findings)


def _build_table_snapshots(document_data: dict[str, Any]) -> list[TableSnapshot]:
    snapshots: list[TableSnapshot] = []
    for index, table in enumerate(document_data.get("tables", []) or []):
        data = table.get("data") or {}
        num_rows = int(data.get("num_rows") or 0)
        num_cols = int(data.get("num_cols") or 0)
        rows, row_header_flags = _rows_from_cells(data.get("table_cells") or [], num_rows, num_cols)
        header_rows = 0
        for flags in row_header_flags:
            if any(flags):
                header_rows += 1
            else:
                break
        header = rows[header_rows - 1] if header_rows and rows else ()
        header_is_credible = _is_credible_header(header)
        if not header_is_credible:
            header_rows = 0
            header = ()
        first_row = rows[0] if rows else ()
        snapshots.append(
            TableSnapshot(
                index=index,
                number=index + 1,
                self_ref=str(table.get("self_ref") or f"#/tables/{index}"),
                page=_table_page(table),
                bbox=_table_bbox(table),
                num_rows=num_rows,
                num_cols=num_cols,
                rows=tuple(rows),
                header_rows=header_rows,
                header=tuple(header),
                header_is_credible=header_is_credible,
                has_numeric_synthetic_header=not header_is_credible and bool(first_row),
                column_edges=_column_edges(data.get("table_cells") or [], num_cols),
            )
        )
    return snapshots


def _rows_from_cells(
    cells: list[dict[str, Any]],
    num_rows: int,
    num_cols: int,
) -> tuple[list[tuple[str, ...]], list[tuple[bool, ...]]]:
    rows = [["" for _ in range(num_cols)] for _ in range(num_rows)]
    flags = [[False for _ in range(num_cols)] for _ in range(num_rows)]
    for cell in cells:
        start_row = int(cell.get("start_row_offset_idx") or 0)
        end_row = int(cell.get("end_row_offset_idx") or start_row + 1)
        start_col = int(cell.get("start_col_offset_idx") or 0)
        end_col = int(cell.get("end_col_offset_idx") or start_col + 1)
        text = str(cell.get("text") or "").strip()
        is_header = bool(cell.get("column_header"))
        for row_index in range(max(start_row, 0), min(end_row, num_rows)):
            for col_index in range(max(start_col, 0), min(end_col, num_cols)):
                rows[row_index][col_index] = text
                flags[row_index][col_index] = is_header
    return [tuple(row) for row in rows], [tuple(row) for row in flags]


def _analyze_and_repair(
    snapshots: list[TableSnapshot],
    table_items: dict[str, Any],
    document_data: dict[str, Any],
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    repaired_refs: set[str] = set()

    for snapshot in snapshots:
        normalized = tuple(_join_header_fragment(cell) for cell in snapshot.header)
        if snapshot.header_is_credible and normalized != snapshot.header:
            changed = _repair_header_text(table_items.get(snapshot.self_ref), snapshot.header_rows, normalized)
            findings.append(
                _finding(
                    findings,
                    "wrapped_header_fragments",
                    "repaired_high_confidence" if changed else "review_required",
                    "Wrapped table header fragments were normalized."
                    if changed
                    else "Wrapped table header fragments were detected but could not be repaired in the shadow document.",
                    [snapshot],
                    [
                        "Docling marked the source row as a column header.",
                        "Only a one-letter lowercase wrap suffix was joined.",
                    ],
                )
            )
            if changed:
                repaired_refs.add(snapshot.self_ref)

    for first, second in zip(snapshots, snapshots[1:], strict=False):
        if not _same_page_boundary_flow(first, second, document_data):
            continue
        if not _compatible_columns(first, second, document_data):
            findings.append(
                _finding(
                    findings,
                    "ambiguous_table_boundary",
                    "review_required",
                    "Adjacent page-boundary tables do not have compatible column geometry.",
                    [first, second],
                    ["Column counts or x-alignment differ beyond the conservative tolerance."],
                )
            )
            continue
        if not first.header_is_credible:
            continue
        if second.header_is_credible:
            if _headers_match(first.header, second.header):
                findings.append(
                    _finding(
                        findings,
                        "repeated_continuation_header",
                        "verified",
                        "Repeated continuation header is already structurally safe.",
                        [first, second],
                        ["Both fragments have matching Docling-marked column headers."],
                    )
                )
                _repair_explicit_row_split_if_needed(first, second, table_items, findings)
            continue

        category = (
            "header_only_fragment_continuation"
            if first.num_rows <= first.header_rows
            else "missing_continuation_header"
        )
        inserted = _insert_inherited_header(
            table_items.get(second.self_ref),
            first.header,
        )
        status = "repaired_high_confidence" if inserted else "review_required"
        findings.append(
            _finding(
                findings,
                category,
                status,
                "Inherited a credible column header into a continuation table."
                if inserted
                else "Continuation table lacks credible headers and could not be repaired in the shadow document.",
                [first, second],
                [
                    "Fragments are adjacent in document flow on consecutive pages.",
                    "First fragment ends near the page boundary and second starts near the next page boundary.",
                    "Column counts and x-alignment are compatible.",
                    "The inherited header comes from Docling-marked column_header cells.",
                ],
                inherited_header=list(first.header),
            )
        )
        if inserted:
            repaired_refs.add(second.self_ref)
            _repair_explicit_row_split_if_needed(
                first,
                second,
                table_items,
                findings,
                second_row_offset=1,
            )

    for snapshot in snapshots:
        if snapshot.self_ref in repaired_refs or snapshot.header_is_credible:
            continue
        if snapshot.has_numeric_synthetic_header:
            findings.append(
                _finding(
                    findings,
                    "synthetic_numeric_headers",
                    "review_required",
                    "LLM-facing table would use positional or numeric headers.",
                    [snapshot],
                    [
                        "Docling did not mark a credible column header row.",
                        "CSV/Markdown/chunks must not infer column semantics from row position.",
                    ],
                )
            )
    return findings


def _repair_explicit_row_split_if_needed(
    first: TableSnapshot,
    second: TableSnapshot,
    table_items: dict[str, Any],
    findings: list[dict[str, Any]],
    second_row_offset: int = 0,
) -> None:
    left_index = first.num_rows - 1
    right_index = second.header_rows + second_row_offset
    if left_index < first.header_rows or right_index >= second.num_rows:
        return
    left = first.rows[left_index]
    right = second.rows[second.header_rows if second.header_rows else 0]
    if not _rows_have_explicit_continuation(left, right):
        return
    merged = tuple(_merge_continued_cell(a, b) for a, b in zip(left, right, strict=True))
    changed_left = _replace_table_row(table_items.get(first.self_ref), left_index, merged)
    changed_right = _remove_table_row(table_items.get(second.self_ref), right_index)
    findings.append(
        _finding(
            findings,
            "explicit_row_split",
            "repaired_high_confidence" if changed_left and changed_right else "review_required",
            "Merged an explicit row split across page-boundary table fragments."
            if changed_left and changed_right
            else "Explicit row split detected but could not be fully repaired in the shadow document.",
            [first, second],
            ["The left row ends with an explicit continuation marker or the right first cell is blank."],
            merged_row=list(merged),
        )
    )


def _table_items_by_ref(document: Any) -> dict[str, Any]:
    items: dict[str, Any] = {}
    iterator = getattr(document, "iterate_items", None)
    if not callable(iterator):
        return items
    for element, _level in iterator():
        data = getattr(element, "data", None)
        if data is None or not hasattr(data, "table_cells"):
            continue
        self_ref = str(getattr(element, "self_ref", ""))
        if self_ref:
            items[self_ref] = element
    return items


def _insert_inherited_header(table_item: Any, header: tuple[str, ...]) -> bool:
    data = getattr(table_item, "data", None)
    cells = getattr(data, "table_cells", None)
    if data is None or cells is None or not header:
        return False
    for cell in cells:
        _shift_cell_rows(cell, 1)
    header_cells = []
    for col_index, text in enumerate(header):
        template = cells[col_index] if col_index < len(cells) else (cells[0] if cells else None)
        cell = copy.deepcopy(template) if template is not None else _Cell()
        _set_cell(cell, "text", text)
        _set_cell(cell, "column_header", True)
        _set_cell(cell, "row_header", False)
        _set_cell(cell, "row_section", False)
        _set_cell(cell, "row_span", 1)
        _set_cell(cell, "col_span", 1)
        _set_cell(cell, "start_row_offset_idx", 0)
        _set_cell(cell, "end_row_offset_idx", 1)
        _set_cell(cell, "start_col_offset_idx", col_index)
        _set_cell(cell, "end_col_offset_idx", col_index + 1)
        header_cells.append(cell)
    cells[:0] = header_cells
    _set_cell(data, "num_rows", int(getattr(data, "num_rows", 0) or 0) + 1)
    _set_cell(data, "num_cols", max(int(getattr(data, "num_cols", 0) or 0), len(header)))
    return True


def _repair_header_text(table_item: Any, header_rows: int, normalized: tuple[str, ...]) -> bool:
    data = getattr(table_item, "data", None)
    cells = getattr(data, "table_cells", None)
    if data is None or cells is None or header_rows <= 0:
        return False
    changed = False
    target_row = header_rows - 1
    for cell in cells:
        if int(getattr(cell, "start_row_offset_idx", -1)) != target_row:
            continue
        col = int(getattr(cell, "start_col_offset_idx", -1))
        if 0 <= col < len(normalized) and getattr(cell, "text", None) != normalized[col]:
            _set_cell(cell, "text", normalized[col])
            changed = True
    return changed


def _replace_table_row(table_item: Any, row_index: int, values: tuple[str, ...]) -> bool:
    data = getattr(table_item, "data", None)
    cells = getattr(data, "table_cells", None)
    if data is None or cells is None:
        return False
    changed = False
    for cell in cells:
        if int(getattr(cell, "start_row_offset_idx", -1)) != row_index:
            continue
        col = int(getattr(cell, "start_col_offset_idx", -1))
        if 0 <= col < len(values):
            _set_cell(cell, "text", values[col])
            changed = True
    return changed


def _remove_table_row(table_item: Any, row_index: int) -> bool:
    data = getattr(table_item, "data", None)
    cells = getattr(data, "table_cells", None)
    if data is None or cells is None:
        return False
    original = len(cells)
    cells[:] = [
        cell
        for cell in cells
        if int(getattr(cell, "start_row_offset_idx", -1)) != row_index
    ]
    if len(cells) == original:
        return False
    for cell in cells:
        if int(getattr(cell, "start_row_offset_idx", 0)) > row_index:
            _shift_cell_rows(cell, -1)
    _set_cell(data, "num_rows", max(int(getattr(data, "num_rows", 0) or 0) - 1, 0))
    return True


def _shift_cell_rows(cell: Any, offset: int) -> None:
    _set_cell(
        cell,
        "start_row_offset_idx",
        int(getattr(cell, "start_row_offset_idx", 0) or 0) + offset,
    )
    _set_cell(
        cell,
        "end_row_offset_idx",
        int(getattr(cell, "end_row_offset_idx", 0) or 0) + offset,
    )


def _set_cell(item: Any, name: str, value: Any) -> None:
    if isinstance(item, dict):
        item[name] = value
    else:
        setattr(item, name, value)


class _Cell:
    bbox = None
    row_span = 1
    col_span = 1
    start_row_offset_idx = 0
    end_row_offset_idx = 1
    start_col_offset_idx = 0
    end_col_offset_idx = 1
    text = ""
    column_header = False
    row_header = False
    row_section = False
    fillable = False


def _finding(
    findings: list[dict[str, Any]],
    category: str,
    status: str,
    message: str,
    snapshots: list[TableSnapshot],
    rationale: list[str],
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        "id": f"si-{len(findings) + 1:04d}",
        "category": category,
        "status": status,
        "message": message,
        "rationale": rationale,
        "table_indexes": [snapshot.index for snapshot in snapshots],
        "table_numbers": [snapshot.number for snapshot in snapshots],
        "source_table_refs": [snapshot.self_ref for snapshot in snapshots],
        "source_refs": [snapshot.self_ref for snapshot in snapshots],
        "pages": sorted(
            {snapshot.page for snapshot in snapshots if snapshot.page is not None}
        ),
        "affected_artifacts": [
            "document.md",
            "tables/*.csv",
            "tables/*.html",
            "chunks.jsonl",
        ],
        "provenance_scope": "page" if any(snapshot.page is not None for snapshot in snapshots) else "unavailable",
        "llm_warning": TABLE_INTEGRITY_WARNING,
        "blocks_llm_readiness": status == "review_required",
    }
    payload.update(extra)
    return payload


def _same_page_boundary_flow(
    first: TableSnapshot,
    second: TableSnapshot,
    document_data: dict[str, Any],
) -> bool:
    if first.page is None or second.page is None or second.page - first.page != 1:
        return False
    if not _adjacent_in_body(first.self_ref, second.self_ref, document_data):
        return False
    height_a = _page_height(document_data, first.page)
    height_b = _page_height(document_data, second.page)
    if height_a is None or height_b is None or first.bbox is None or second.bbox is None:
        return False
    return (
        float(first.bbox.get("b", height_a)) <= height_a * 0.12
        and float(second.bbox.get("t", 0)) >= height_b * 0.85
    )


def _adjacent_in_body(ref_a: str, ref_b: str, document_data: dict[str, Any]) -> bool:
    children = (document_data.get("body") or {}).get("children", []) or []
    refs = [str(child.get("$ref", "")) for child in children]
    try:
        index_a = refs.index(ref_a)
        index_b = refs.index(ref_b)
    except ValueError:
        return False
    if index_b <= index_a:
        return False
    texts_by_ref = {
        str(item.get("self_ref")): item for item in document_data.get("texts", []) or []
    }
    for ref in refs[index_a + 1 : index_b]:
        text = texts_by_ref.get(ref)
        if text and text.get("label") in {"page_header", "page_footer"}:
            continue
        return False
    return True


def _compatible_columns(
    first: TableSnapshot,
    second: TableSnapshot,
    document_data: dict[str, Any],
) -> bool:
    if first.num_cols <= 0 or first.num_cols != second.num_cols:
        return False
    if not first.column_edges or not second.column_edges:
        return True
    if len(first.column_edges) != len(second.column_edges):
        return True
    width = _page_width(document_data, first.page) or _page_width(document_data, second.page) or 1.0
    tolerance = max(width * 0.035, 12.0)
    for (left_a, right_a), (left_b, right_b) in zip(
        first.column_edges,
        second.column_edges,
    ):
        if abs(left_a - left_b) > tolerance or abs(right_a - right_b) > tolerance:
            return False
    return True


def _column_edges(
    cells: list[dict[str, Any]],
    num_cols: int,
) -> tuple[tuple[float, float], ...]:
    edges: list[tuple[float, float] | None] = [None] * num_cols
    for cell in cells:
        if int(cell.get("col_span") or 1) != 1:
            continue
        col = int(cell.get("start_col_offset_idx") or 0)
        bbox = cell.get("bbox") or {}
        if not (0 <= col < num_cols):
            continue
        if not isinstance(bbox.get("l"), (int, float)) or not isinstance(bbox.get("r"), (int, float)):
            continue
        current = edges[col]
        left = float(bbox["l"])
        right = float(bbox["r"])
        edges[col] = (
            (min(current[0], left), max(current[1], right))
            if current is not None
            else (left, right)
        )
    return tuple(edge for edge in edges if edge is not None)


def _is_credible_header(header: tuple[str, ...]) -> bool:
    if not header or any(not cell.strip() for cell in header):
        return False
    if all(_is_numeric_label(cell) for cell in header):
        return False
    compact = [_text_signature(cell) for cell in header]
    return len(set(compact)) == len(compact) and all(compact)


def _headers_match(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    if len(left) != len(right) or not left:
        return False
    a = "|".join(_text_signature(cell) for cell in left)
    b = "|".join(_text_signature(cell) for cell in right)
    return SequenceMatcher(None, a, b).ratio() >= 0.95


def _rows_have_explicit_continuation(
    left: tuple[str, ...],
    right: tuple[str, ...],
) -> bool:
    if len(left) != len(right) or not left:
        return False
    if not right[0].strip():
        return True
    return left[0].rstrip().endswith(("_", "-", "/", "\\"))


def _merge_continued_cell(left: str, right: str) -> str:
    left = left.strip()
    right = right.strip()
    if not left:
        return right
    if not right:
        return left
    if left.endswith(("_", "-", "/", "\\")):
        return f"{left}{right}"
    return f"{left} {right}"


def _join_header_fragment(cell: str) -> str:
    match = re.fullmatch(r"([A-Z][A-Za-z]{3,})\s+([a-z])", cell.strip())
    return f"{match.group(1)}{match.group(2)}" if match else cell


def _is_numeric_label(text: str) -> bool:
    return bool(re.fullmatch(r"\d+", text.strip()))


def _text_signature(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _table_page(table: dict[str, Any]) -> int | None:
    prov = table.get("prov") or []
    page = prov[0].get("page_no") if prov else None
    return int(page) if isinstance(page, (int, float)) else None


def _table_bbox(table: dict[str, Any]) -> dict[str, Any] | None:
    prov = table.get("prov") or []
    bbox = prov[0].get("bbox") if prov else None
    return bbox if isinstance(bbox, dict) else None


def _page_height(document_data: dict[str, Any], page_no: int | None) -> float | None:
    if page_no is None:
        return None
    page = (document_data.get("pages") or {}).get(str(page_no)) or {}
    height = (page.get("size") or {}).get("height")
    return float(height) if isinstance(height, (int, float)) else None


def _page_width(document_data: dict[str, Any], page_no: int | None) -> float | None:
    if page_no is None:
        return None
    page = (document_data.get("pages") or {}).get(str(page_no)) or {}
    width = (page.get("size") or {}).get("width")
    return float(width) if isinstance(width, (int, float)) else None


def _highest_status(statuses: list[str]) -> str:
    priority = {
        "review_required": 3,
        "repaired_high_confidence": 2,
        "preserved": 1,
        "verified": 0,
    }
    return max(statuses, key=lambda status: priority.get(status, -1))
