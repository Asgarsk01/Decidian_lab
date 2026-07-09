from __future__ import annotations

from decidian_docling.models import ParsingProfile
from decidian_docling.profiles import get_profile


def test_standard_profile_defaults() -> None:
    profile = get_profile(ParsingProfile.STANDARD)
    assert profile.do_ocr is True
    assert profile.force_full_page_ocr is False
    assert profile.table_mode == "accurate"
    assert profile.heading_hierarchy is True
    assert profile.do_picture_description is False
    assert profile.enable_remote_services is False


def test_scanned_profile_forces_ocr() -> None:
    profile = get_profile("scanned")
    assert profile.force_full_page_ocr is True
    assert profile.do_chart_extraction is False


def test_visual_profile_enables_local_visual_enrichment() -> None:
    profile = get_profile("visual")
    assert profile.do_picture_classification is True
    assert profile.do_chart_extraction is True
    assert profile.do_picture_description is False

