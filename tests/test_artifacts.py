from __future__ import annotations

from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import pytest

from decidian_docling.artifacts import (
    QUALITY_FIELDS,
    artifact_inventory,
    build_download_archive,
    initialize_evaluation,
    read_json,
    save_evaluation,
    write_json,
)
from decidian_docling.models import EvaluationError


def test_json_and_inventory_round_trip(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_json(run_dir / "manifest.json", {"status": "success"})
    (run_dir / "document.md").write_text("# Decision", encoding="utf-8")

    inventory = artifact_inventory(run_dir)
    assert {item["path"] for item in inventory} == {
        "document.md",
        "manifest.json",
    }


def test_download_archive_contains_generated_files_without_writing_zip(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "document.md").write_text("# Decision", encoding="utf-8")
    (run_dir / "pictures").mkdir()
    (run_dir / "pictures" / "picture-0001.png").write_bytes(b"image")

    archive = build_download_archive(run_dir)

    with ZipFile(BytesIO(archive)) as bundle:
        assert set(bundle.namelist()) == {
            "document.md",
            "pictures/picture-0001.png",
        }
    assert not (run_dir / "result.zip").exists()

def test_json_replaces_non_finite_numbers(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    write_json(path, {"score": float("nan"), "nested": [float("inf")]})
    assert read_json(path) == {"score": None, "nested": [None]}


def test_save_evaluation_validates_scores(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    scores = {field: 2 for field in QUALITY_FIELDS}
    path = save_evaluation(run_dir, scores, "Looks correct.")
    payload = read_json(path)
    assert payload["total"] == 14
    assert payload["maximum"] == 14

    invalid = dict(scores)
    invalid["ocr"] = 3
    with pytest.raises(EvaluationError):
        save_evaluation(run_dir, invalid)


def test_initialize_evaluation_marks_scores_pending(tmp_path: Path) -> None:
    path = initialize_evaluation(tmp_path)
    payload = read_json(path)
    assert payload["status"] == "pending"
    assert payload["total"] is None
    assert set(payload["scores"]) == set(QUALITY_FIELDS)
    assert all(score is None for score in payload["scores"].values())
