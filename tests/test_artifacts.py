from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from decidian_docling.artifacts import (
    QUALITY_FIELDS,
    artifact_inventory,
    build_archive,
    initialize_evaluation,
    read_json,
    save_evaluation,
    write_json,
)
from decidian_docling.models import EvaluationError


def test_json_and_archive_round_trip(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_json(run_dir / "manifest.json", {"status": "success"})
    (run_dir / "document.md").write_text("# Decision", encoding="utf-8")

    inventory = artifact_inventory(run_dir)
    assert {item["path"] for item in inventory} == {
        "document.md",
        "manifest.json",
    }

    archive = build_archive(run_dir)
    with zipfile.ZipFile(archive) as bundle:
        assert {"manifest.json", "document.md"} <= set(bundle.namelist())
        assert "result.zip" not in bundle.namelist()


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
    assert (run_dir / "result.zip").exists()

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
