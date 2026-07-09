from __future__ import annotations

from pathlib import Path

import pytest

from decidian_docling.models import InputValidationError
from decidian_docling.validation import (
    MAX_FILE_SIZE,
    sanitize_stem,
    sha256_file,
    validate_input,
)


def test_sanitize_stem_removes_unsafe_characters() -> None:
    assert sanitize_stem("../../Payment RFC (final)!.pdf") == "Payment-RFC-final"
    assert sanitize_stem("...") == "document"


def test_hash_is_stable(tmp_path: Path) -> None:
    path = tmp_path / "sample.md"
    path.write_text("# Decision\nUse the queue.", encoding="utf-8")
    assert sha256_file(path) == sha256_file(path)
    assert len(sha256_file(path)) == 64


def test_valid_markdown(tmp_path: Path) -> None:
    path = tmp_path / "sample.md"
    path.write_text("# Approved decision\nUse optimistic locking.", encoding="utf-8")
    validated = validate_input(path)
    assert validated.extension == ".md"
    assert validated.detected_mime.startswith("text/")


def test_rejects_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.pdf"
    path.touch()
    with pytest.raises(InputValidationError, match="empty"):
        validate_input(path)


def test_rejects_content_extension_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "fake.pdf"
    path.write_text("This is not a PDF.", encoding="utf-8")
    with pytest.raises(InputValidationError, match="does not match"):
        validate_input(path)


def test_txt_uses_content_validation_when_mime_is_misclassified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "notes.txt"
    path.write_text("const is a word that can confuse MIME detection.", encoding="utf-8")
    monkeypatch.setattr(
        "decidian_docling.validation._detect_mime",
        lambda _path: "application/javascript",
    )
    assert validate_input(path).detected_mime == "application/javascript"


def test_txt_rejects_binary_nul_content(tmp_path: Path) -> None:
    path = tmp_path / "binary.txt"
    path.write_bytes(b"prefix\x00suffix")
    with pytest.raises(InputValidationError, match="does not match"):
        validate_input(path)


def test_rejects_oversized_input_without_allocating_large_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "large.md"
    path.write_text("small", encoding="utf-8")
    with pytest.raises(InputValidationError, match="limit"):
        validate_input(path, max_size=1)
