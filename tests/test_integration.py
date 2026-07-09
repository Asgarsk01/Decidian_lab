from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from decidian_docling.models import ParsingProfile, RunStatus
from decidian_docling.parser import parse_document

from .fixture_factory import create_all_fixtures

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    if os.getenv("RUN_DOCLING_INTEGRATION") != "1":
        pytest.skip("Set RUN_DOCLING_INTEGRATION=1 to run real Docling conversion")
    return create_all_fixtures(tmp_path_factory.mktemp("fixtures"))


@pytest.mark.parametrize(
    ("fixture_name", "profile"),
    [
        ("pdf", ParsingProfile.STANDARD),
        ("docx", ParsingProfile.STANDARD),
        ("scanned", ParsingProfile.SCANNED),
    ],
)
def test_real_docling_exports(
    tmp_path: Path,
    fixtures: dict[str, Path],
    fixture_name: str,
    profile: ParsingProfile,
) -> None:
    result = parse_document(
        fixtures[fixture_name],
        profile=profile,
        output_root=tmp_path / "output",
    )
    assert result.status in {RunStatus.SUCCESS, RunStatus.PARTIAL_SUCCESS}
    for required in [
        "manifest.json",
        "document.json",
        "document.md",
        "document.html",
        "document.txt",
        "chunks.jsonl",
        "evaluation.json",
        "result.zip",
    ]:
        assert (result.run_dir / required).is_file(), required

    chunks = [
        json.loads(line)
        for line in (result.run_dir / "chunks.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert chunks
    assert all(chunk["token_count"] <= 1200 for chunk in chunks)
    assert result.manifest["counts"]["elements"] > 0

    if fixture_name == "pdf":
        assert list((result.run_dir / "pages").glob("*.png"))
        assert list((result.run_dir / "tables").glob("*.csv"))
        assert list((result.run_dir / "pictures").glob("*.png"))
