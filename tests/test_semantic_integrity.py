from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from decidian_docling.semantic_integrity import (
    IMAGE_INTEGRITY_WARNING,
    INTEGRITY_WARNING_PREFIX,
    OCR_INTEGRITY_WARNING,
    add_picture_integrity_findings,
    annotate_chunks_with_integrity,
    apply_pdf_semantic_integrity,
    empty_integrity_report,
)


@dataclass
class FakeCell:
    text: str
    start_row_offset_idx: int
    end_row_offset_idx: int
    start_col_offset_idx: int
    end_col_offset_idx: int
    column_header: bool = False
    row_header: bool = False
    row_section: bool = False
    row_span: int = 1
    col_span: int = 1
    fillable: bool = False
    bbox: dict[str, Any] | None = None


@dataclass
class FakeTableData:
    table_cells: list[FakeCell]
    num_rows: int
    num_cols: int


@dataclass
class FakeTable:
    self_ref: str
    data: FakeTableData


@dataclass
class FakeDocument:
    tables: list[FakeTable] = field(default_factory=list)

    def iterate_items(self):
        for table in self.tables:
            yield table, 0


def test_semantic_integrity_inherits_missing_continuation_headers() -> None:
    document_data = _document_data(
        [
            _json_table(
                0,
                1,
                {"t": 400, "b": 40, "l": 50, "r": 450},
                [
                    ["Key", "Description", "Payload", "Frontend Action"],
                    ["AUTH_REQUIRED", "Missing token", "401", "Show login"],
                ],
                header=True,
            ),
            _json_table(
                1,
                2,
                {"t": 760, "b": 500, "l": 50, "r": 450},
                [
                    ["MCP_SERVER_URL", "Server URL", "https://example.test", "Render field"],
                    ["THREAT_DETECTED", "Threat", "true", "Show alert"],
                ],
                header=False,
            ),
        ]
    )
    document = _fake_document(document_data)
    warnings: list[str] = []

    shadow, report = apply_pdf_semantic_integrity(document, document_data, warnings)

    assert report["llm_readiness"] == "ready"
    assert report["summary"]["repaired_high_confidence"] == 1
    assert report["findings"][0]["category"] == "missing_continuation_header"
    repaired_table = shadow.tables[1]
    assert repaired_table.data.num_rows == 3
    assert [cell.text for cell in repaired_table.data.table_cells[:4]] == [
        "Key",
        "Description",
        "Payload",
        "Frontend Action",
    ]
    assert all(cell.column_header for cell in repaired_table.data.table_cells[:4])
    assert [cell.text for cell in document.tables[1].data.table_cells[:4]] == [
        "MCP_SERVER_URL",
        "Server URL",
        "https://example.test",
        "Render field",
    ]
    assert any("repaired" in warning for warning in warnings)


def test_semantic_integrity_repairs_header_only_fragment_followed_by_rows() -> None:
    document_data = _document_data(
        [
            _json_table(
                0,
                1,
                {"t": 120, "b": 40, "l": 50, "r": 450},
                [["Name", "Value"]],
                header=True,
            ),
            _json_table(
                1,
                2,
                {"t": 760, "b": 600, "l": 50, "r": 450},
                [["MCP_SERVER_URL", "https://example.test"]],
                header=False,
            ),
        ]
    )
    shadow, report = apply_pdf_semantic_integrity(
        _fake_document(document_data),
        document_data,
        [],
    )

    assert report["findings"][0]["category"] == "header_only_fragment_continuation"
    assert [cell.text for cell in shadow.tables[1].data.table_cells[:2]] == [
        "Name",
        "Value",
    ]


def test_semantic_integrity_keeps_ambiguous_numeric_header_tables_review_required() -> None:
    document_data = _document_data(
        [
            _json_table(
                0,
                1,
                {"t": 500, "b": 300, "l": 50, "r": 450},
                [["MCP_SERVER_URL", "https://example.test"]],
                header=False,
            )
        ]
    )
    warnings: list[str] = []

    _shadow, report = apply_pdf_semantic_integrity(
        _fake_document(document_data),
        document_data,
        warnings,
    )

    assert report["llm_readiness"] == "review_required"
    assert report["findings"][0]["category"] == "synthetic_numeric_headers"
    assert any("requires review" in warning for warning in warnings)


def test_semantic_integrity_normalizes_wrapped_headers() -> None:
    document_data = _document_data(
        [
            _json_table(
                0,
                1,
                {"t": 500, "b": 300, "l": 50, "r": 450},
                [["Alert", "Threshol d"], ["RATE_LIMIT", "> 10"]],
                header=True,
            )
        ]
    )

    shadow, report = apply_pdf_semantic_integrity(
        _fake_document(document_data),
        document_data,
        [],
    )

    assert report["findings"][0]["category"] == "wrapped_header_fragments"
    assert shadow.tables[0].data.table_cells[1].text == "Threshold"


def test_chunk_annotations_warn_for_review_required_tables() -> None:
    chunks = [
        {
            "text": "MCP_SERVER_URL https://example.test",
            "contextualized_text": "MCP_SERVER_URL https://example.test",
            "source_refs": [{"self_ref": "#/tables/0"}],
        }
    ]
    report = {
        "findings": [
            {
                "id": "si-0001",
                "status": "review_required",
                "source_table_refs": ["#/tables/0"],
            }
        ]
    }

    annotate_chunks_with_integrity(chunks, report)

    assert chunks[0]["integrity_status"] == "review_required"
    assert chunks[0]["integrity_finding_ids"] == ["si-0001"]
    assert chunks[0]["contextualized_text"].startswith(INTEGRITY_WARNING_PREFIX)


def test_unverified_picture_blocks_readiness_and_annotates_related_chunk() -> None:
    report = add_picture_integrity_findings(
        empty_integrity_report("docx"),
        [
            {
                "picture_file": "picture-0001.png",
                "asset_uri": "assets/diagram.png",
                "source_ref": "#/pictures/0",
                "qualifying": True,
                "coverage_status": "ocr_unavailable",
                "page_number": None,
            },
            {
                "picture_file": "picture-0002.png",
                "source_ref": "#/pictures/1",
                "qualifying": False,
                "coverage_status": "below_threshold",
            },
        ],
    )
    chunks = [
        {
            "text": "Architecture diagram",
            "contextualized_text": "Architecture diagram",
            "source_refs": [{"self_ref": "#/pictures/0"}],
        }
    ]

    annotate_chunks_with_integrity(chunks, report)

    assert report["llm_readiness"] == "review_required"
    finding = report["findings"][0]
    assert finding["blocks_llm_readiness"] is True
    assert finding["provenance_scope"] == "unavailable"
    assert chunks[0]["integrity_status"] == "review_required"
    assert chunks[0]["text"].startswith(IMAGE_INTEGRITY_WARNING)


def test_preserved_blocking_finding_blocks_readiness() -> None:
    report = add_picture_integrity_findings(
        {
            **empty_integrity_report("docx"),
            "findings": [
                {
                    "id": "si-0001",
                    "status": "preserved",
                    "blocks_llm_readiness": True,
                }
            ],
        },
        [],
    )

    assert report["llm_readiness"] == "review_required"


def test_low_trust_ocr_blocks_readiness_without_claiming_text_is_missing() -> None:
    report = add_picture_integrity_findings(
        empty_integrity_report("docx"),
        [
            {
                "picture_file": "picture-0001.png",
                "source_ref": "#/pictures/0",
                "qualifying": True,
                "coverage_status": "ocr_text",
            }
        ],
    )
    chunks = [
        {
            "text": "Recovered OCR text",
            "contextualized_text": "Recovered OCR text",
            "source_refs": [{"self_ref": "#/pictures/0"}],
        }
    ]

    annotate_chunks_with_integrity(chunks, report)

    finding = report["findings"][0]
    assert finding["category"] == "low_trust_picture_ocr"
    assert finding["requires_standalone_warning"] is False
    assert report["llm_readiness"] == "review_required"
    assert chunks[0]["integrity_status"] == "review_required"
    assert chunks[0]["text"].startswith(OCR_INTEGRITY_WARNING)


def _document_data(tables: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "pages": {
            "1": {"size": {"height": 800, "width": 500}},
            "2": {"size": {"height": 800, "width": 500}},
        },
        "texts": [],
        "body": {"children": [{"$ref": table["self_ref"]} for table in tables]},
        "tables": tables,
    }


def _json_table(
    index: int,
    page: int,
    bbox: dict[str, float],
    rows: list[list[str]],
    header: bool,
) -> dict[str, Any]:
    cells = []
    col_width = (bbox["r"] - bbox["l"]) / len(rows[0])
    row_height = max((bbox["t"] - bbox["b"]) / max(len(rows), 1), 1)
    for row_index, row in enumerate(rows):
        for col_index, text in enumerate(row):
            left = bbox["l"] + col_width * col_index
            right = left + col_width
            top = bbox["t"] - row_height * row_index
            bottom = top - row_height
            cells.append(
                {
                    "text": text,
                    "start_row_offset_idx": row_index,
                    "end_row_offset_idx": row_index + 1,
                    "start_col_offset_idx": col_index,
                    "end_col_offset_idx": col_index + 1,
                    "column_header": header and row_index == 0,
                    "row_span": 1,
                    "col_span": 1,
                    "bbox": {"l": left, "r": right, "t": top, "b": bottom},
                }
            )
    return {
        "self_ref": f"#/tables/{index}",
        "prov": [{"page_no": page, "bbox": bbox}],
        "data": {
            "num_rows": len(rows),
            "num_cols": len(rows[0]),
            "table_cells": cells,
        },
    }


def _fake_document(document_data: dict[str, Any]) -> FakeDocument:
    tables = []
    for table in document_data["tables"]:
        cells = [
            FakeCell(
                text=cell["text"],
                start_row_offset_idx=cell["start_row_offset_idx"],
                end_row_offset_idx=cell["end_row_offset_idx"],
                start_col_offset_idx=cell["start_col_offset_idx"],
                end_col_offset_idx=cell["end_col_offset_idx"],
                column_header=cell["column_header"],
                bbox=cell["bbox"],
            )
            for cell in table["data"]["table_cells"]
        ]
        tables.append(
            FakeTable(
                self_ref=table["self_ref"],
                data=FakeTableData(
                    table_cells=cells,
                    num_rows=table["data"]["num_rows"],
                    num_cols=table["data"]["num_cols"],
                ),
            )
        )
    return FakeDocument(tables)
