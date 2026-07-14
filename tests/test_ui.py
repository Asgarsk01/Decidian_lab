from __future__ import annotations

from decidian_docling.ui import _page_metric


def test_docx_without_page_provenance_is_not_rendered_as_zero_pages() -> None:
    value, help_text = _page_metric(
        {
            "source": {"extension": ".docx"},
            "counts": {"pages": 0},
            "provenance_scope": "unavailable",
        }
    )

    assert value == "Unavailable"
    assert help_text == "DOCX page provenance is unavailable in this conversion."


def test_docx_with_section_provenance_is_not_rendered_as_zero_pages() -> None:
    value, help_text = _page_metric(
        {
            "source": {"extension": ".docx"},
            "counts": {"pages": 0},
            "provenance_scope": "section_only",
        }
    )

    assert value == "Unavailable"
    assert help_text == "DOCX page provenance is unavailable in this conversion."


def test_page_metric_preserves_real_page_counts() -> None:
    assert _page_metric(
        {
            "source": {"extension": ".pdf"},
            "counts": {"pages": 48},
            "provenance_scope": "page",
        }
    ) == (48, None)
