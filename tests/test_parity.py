from __future__ import annotations

from pathlib import Path

from decidian_docling.parity import compare_run_parity


def test_compare_run_parity_matches_feed_files(tmp_path: Path) -> None:
    first = tmp_path / "full"
    second = tmp_path / "extraction"
    first.mkdir()
    second.mkdir()
    for filename in ("document.md", "document.json", "picture_text.jsonl"):
        (first / filename).write_text(f"{filename}\n", encoding="utf-8")
        (second / filename).write_text(f"{filename}\n", encoding="utf-8")

    result = compare_run_parity(first, second)

    assert result["ok"] is True
    assert all(item["match"] for item in result["files"])


def test_compare_run_parity_reports_differences(tmp_path: Path) -> None:
    first = tmp_path / "full"
    second = tmp_path / "extraction"
    first.mkdir()
    second.mkdir()
    for filename in ("document.md", "document.json", "picture_text.jsonl"):
        (first / filename).write_text("same", encoding="utf-8")
        (second / filename).write_text("same", encoding="utf-8")
    (second / "document.md").write_text("changed", encoding="utf-8")

    result = compare_run_parity(first, second)

    assert result["ok"] is False
    assert result["files"][0]["path"] == "document.md"
    assert result["files"][0]["match"] is False
