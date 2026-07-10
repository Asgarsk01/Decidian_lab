from __future__ import annotations

from decidian_docling.parser import normalize_markdown_export


def test_normalize_markdown_export_decodes_ampersands_only() -> None:
    markdown = "Login &amp; Core Administration\nKeep &lt;literal&gt; escaped."

    assert (
        normalize_markdown_export(markdown)
        == "Login & Core Administration\nKeep &lt;literal&gt; escaped."
    )
