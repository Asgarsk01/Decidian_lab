from __future__ import annotations

import os

import pytest

from decidian_docling.config import get_gemini_settings
from decidian_docling.gemini_review import run_gemini_review

from .fixture_factory import create_test_image

pytestmark = pytest.mark.integration


def test_live_gemini_two_pass_on_small_diagram(tmp_path) -> None:
    if os.getenv("RUN_GEMINI_INTEGRATION") != "1" or not os.getenv("GEMINI_API_KEY"):
        pytest.skip("Set RUN_GEMINI_INTEGRATION=1 and GEMINI_API_KEY for paid live review")
    pictures = tmp_path / "pictures"
    pictures.mkdir()
    create_test_image(pictures / "picture-0001.png")
    candidate = {
        "id": "ai-0001",
        "kind": "diagram",
        "block_id": "block-00001",
        "source_refs": ["#/pictures/0"],
        "page_numbers": [1],
        "section_path": ["Architecture"],
        "picture_file": "picture-0001.png",
        "ocr_hint": "API Service Order Queue",
        "ambiguity_reasons": ["content_bearing_picture"],
        "question": "Extract only visible components and directed relationships.",
    }

    report, results, _supplements = run_gemini_review(
        [candidate],
        tmp_path,
        get_gemini_settings(True),
        tmp_path / "cache",
    )

    assert report["status"] in {"success", "partial"}
    assert results["ai-0001"]["status"] in {"verified", "partial", "unresolved"}
