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


def test_archive_handles_run_names_with_dots(tmp_path: Path) -> None:
    run_dir = tmp_path / "CP_SuperAdminFlow_SRS-V1.1__ef883d3a__20260710T064320008950Z"
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text('{"status":"success"}', encoding="utf-8")

    archive = build_archive(run_dir)

    assert archive == run_dir / "result.zip"
    assert archive.exists()
    assert not (tmp_path / ".CP_SuperAdminFlow_SRS-V1.zip").exists()


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


def test_save_evaluation_can_skip_archive_refresh(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    scores = {field: 1 for field in QUALITY_FIELDS}

    save_evaluation(run_dir, scores, refresh_archive=False)

    assert not (run_dir / "result.zip").exists()


def test_initialize_evaluation_marks_scores_pending(tmp_path: Path) -> None:
    path = initialize_evaluation(tmp_path)
    payload = read_json(path)
    assert payload["status"] == "pending"
    assert payload["total"] is None
    assert set(payload["scores"]) == set(QUALITY_FIELDS)
    assert all(score is None for score in payload["scores"].values())
