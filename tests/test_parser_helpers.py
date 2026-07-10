from __future__ import annotations

import json

from decidian_docling.postprocess import (
    clean_markdown_for_llm,
    extract_picture_text,
    inject_picture_ocr,
    inject_picture_text,
    normalize_markdown_export,
)


def test_normalize_markdown_export_decodes_common_entities() -> None:
    markdown = "Login &amp; Core Administration\nKeep &lt;literal&gt; escaped."

    assert (
        normalize_markdown_export(markdown)
        == "Login & Core Administration\nKeep <literal> escaped."
    )


def test_clean_markdown_splits_fused_headings_and_demotes_field_labels() -> None:
    markdown = "\n".join(
        [
            "####### Width:",
            "#### 7.15.4 Credit Note from DMS 7.15.5 Functionality Scope",
            "# of Virtual Machines (VMs)",
        ]
    )

    cleaned = clean_markdown_for_llm(markdown)

    assert "Width:" in cleaned
    assert "####### Width:" not in cleaned
    assert "#### 7.15.4 Credit Note from DMS\n#### 7.15.5 Functionality Scope" in cleaned
    assert r"\# of Virtual Machines (VMs)" in cleaned


def test_clean_markdown_demotes_false_headings_and_infers_numbered_levels() -> None:
    markdown = "\n".join(
        [
            "#### 8-Nov-2025",
            "#### Automatically transitions workflow to Step 8 -Credit Note from DMS .",
            "#### Width",
            "#### Depth",
            "#### Width:",
            "#### · Date",
            "### 7.17.2 Width",
            "### 7.17.3 Depth",
            "## 10 Infra requirements & System Hygiene",
            "#### Backup and Recovery",
        ]
    )

    cleaned = clean_markdown_for_llm(markdown)

    assert "#### 8-Nov-2025" not in cleaned
    assert "\n8-Nov-2025\n" in f"\n{cleaned}\n"
    assert "#### Automatically transitions workflow" not in cleaned
    assert "Automatically transitions workflow to Step 8 - Credit Note from DMS." in cleaned
    assert "#### Width" in cleaned
    assert "#### Depth" in cleaned
    assert "#### Width:" not in cleaned
    assert "\nWidth:\n" in f"\n{cleaned}\n"
    assert "#### · Date" not in cleaned
    assert "\nDate\n" in f"\n{cleaned}\n"
    assert "#### 7.17.2 Width" in cleaned
    assert "#### 7.17.3 Depth" in cleaned
    assert "## 10 Infra requirements & System Hygiene" in cleaned
    assert "#### Backup and Recovery" in cleaned


def test_clean_markdown_repairs_borderless_two_column_tables() -> None:
    markdown = """## 8 Non-Functional Requirements

Category

Requirement

Performance

Average response time &lt; 3 seconds.

Backup &amp; Recovery Daily database backup.

Compliance

Follows privacy guidelines.

## 9 Technology Matrix
"""

    cleaned = clean_markdown_for_llm(markdown)

    assert "| Category | Requirement |" in cleaned
    assert "| Performance | Average response time < 3 seconds. |" in cleaned
    assert "| Backup & Recovery | Daily database backup. |" in cleaned
    assert "| Compliance | Follows privacy guidelines. |" in cleaned


def test_inject_picture_ocr_adds_text_after_matching_image() -> None:
    markdown = "Before\n\n![Image](assets/mockup.png)\n\nAfter"
    records = [
        {
            "asset_uri": "assets/mockup.png",
            "page_number": 7,
            "picture_file": "picture-0003.png",
            "text": "Claim ID\nApprove Step",
        }
    ]

    injected = inject_picture_ocr(markdown, records)

    assert "LOW-TRUST IMAGE OCR - page 7, picture-0003.png" in injected
    assert "not as authoritative requirements text" in injected
    assert "> Claim ID\n> Approve Step" in injected


def test_pdf_heading_cleanup_uses_numbering_provenance_and_ignores_code() -> None:
    markdown = """### 2. Architecture
#### 2.1 Scope
###### Supporting Analysis
###### ACME Platform | CONFIDENTIAL
```
# shell comment must stay code
```
"""
    document_data = {
        "pages": {"1": {"size": {"height": 800}}},
        "texts": [
            {"label": "page_header", "text": "ACME Platform | CONFIDENTIAL"},
            {
                "label": "section_header",
                "text": "Supporting Analysis",
                "level": 5,
                "prov": [{"page_no": 1, "bbox": {"t": 500, "b": 480}}],
            },
            {
                "label": "section_header",
                "text": "ACME Platform | CONFIDENTIAL",
                "level": 6,
                "prov": [{"page_no": 1, "bbox": {"t": 775, "b": 760}}],
            },
        ],
    }

    cleaned = clean_markdown_for_llm(markdown, document_data)

    assert "## 2. Architecture" in cleaned
    assert "### 2.1 Scope" in cleaned
    assert "##### Supporting Analysis" in cleaned
    assert "ACME Platform | CONFIDENTIAL" not in cleaned
    assert "# shell comment must stay code" in cleaned


def test_clean_markdown_repairs_explicit_continued_table() -> None:
    markdown = """| Alert | Metric | Threshold | Severity |
| --- | --- | --- | --- |
| AI_CONFIDENCE_ | RAG query | < 0.70 over | WARNIN |

| Alert | Metric | Threshold | Severity |
| --- | --- | --- | --- |
| LOW | confidence score avg | 30min | G |
| API_ERROR | API failures | > 5% | CRITICAL |
"""
    table_a = {
        "prov": [{"page_no": 10, "bbox": {"t": 300, "b": 50}}]
    }
    table_b = {
        "prov": [{"page_no": 11, "bbox": {"t": 760, "b": 500}}]
    }
    document_data = {
        "pages": {
            "10": {"size": {"height": 800}},
            "11": {"size": {"height": 800}},
        },
        "texts": [],
        "tables": [table_a, table_b],
    }
    warnings: list[str] = []
    repair_records: list[dict] = []

    cleaned = clean_markdown_for_llm(
        markdown,
        document_data,
        warnings,
        repair_records,
    )

    assert "| AI_CONFIDENCE_LOW | RAG query confidence score avg | < 0.70 over 30min | WARNING |" in cleaned
    assert cleaned.count("| Alert | Metric | Threshold | Severity |") == 1
    assert any("Repaired a continued table" in warning for warning in warnings)
    assert repair_records == [
        {
            "repair_index": 1,
            "table_indexes": [0, 1],
            "table_numbers": [1, 2],
            "pages": [10, 11],
            "headers": ["Alert", "Metric", "Threshold", "Severity"],
            "merged_row": [
                "AI_CONFIDENCE_LOW",
                "RAG query confidence score avg",
                "< 0.70 over 30min",
                "WARNING",
            ],
            "source": "native_table_continuation",
        }
    ]


def test_structured_picture_text_prevents_unnecessary_ocr(tmp_path) -> None:
    pictures = tmp_path / "pictures"
    pictures.mkdir()
    (pictures / "picture-0001.png").write_bytes(b"not-read-for-structured-text")
    document_json = tmp_path / "document.json"
    document_json.write_text(
        json.dumps(
            {
                "pages": {"4": {"size": {"height": 800}}},
                "texts": [
                    {
                        "self_ref": "#/texts/1",
                        "label": "section_header",
                        "text": "4.2 Recovery Sequence",
                        "level": 3,
                        "prov": [{"page_no": 4, "bbox": {"t": 700, "b": 680}}],
                    },
                    {
                        "self_ref": "#/texts/2",
                        "label": "text",
                        "text": "Promote the replica only after health checks pass.",
                        "prov": [{"page_no": 4, "bbox": {"t": 650, "b": 620}}],
                    },
                ],
                "pictures": [
                    {
                        "children": [
                            {"$ref": "#/texts/1"},
                            {"$ref": "#/texts/2"},
                        ],
                        "prov": [{"page_no": 4}],
                        "image": {
                            "uri": "assets/diagram.png",
                            "size": {"width": 900, "height": 600},
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    warnings: list[str] = []

    records = extract_picture_text(
        pictures,
        document_json,
        tmp_path / "picture_text.jsonl",
        warnings,
    )
    injected = inject_picture_text(
        "![Diagram](assets/diagram.png)\n",
        records,
    )

    assert records[0]["source"] == "docling_structured"
    assert "### 4.2 Recovery Sequence" in injected
    assert "MEDIUM-TRUST DOCLING PICTURE TEXT" in injected
    assert "Promote the replica only after health checks pass." in injected
    assert not warnings


def test_picture_text_enrichment_is_non_fatal(tmp_path) -> None:
    document_json = tmp_path / "document.json"
    document_json.write_text("not-json", encoding="utf-8")
    warnings: list[str] = []

    records = extract_picture_text(
        tmp_path,
        document_json,
        tmp_path / "picture_text.jsonl",
        warnings,
    )

    assert records == []
    assert (tmp_path / "picture_text.jsonl").read_text(encoding="utf-8") == ""
    assert any("unexpected error" in warning for warning in warnings)
