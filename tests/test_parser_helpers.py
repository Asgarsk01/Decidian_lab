from __future__ import annotations

from decidian_docling.postprocess import (
    clean_markdown_for_llm,
    inject_picture_ocr,
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
