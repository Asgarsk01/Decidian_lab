from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

PARITY_FILES = ("document.md", "document.json", "picture_text.jsonl")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def compare_run_parity(
    first_run: Path,
    second_run: Path,
    filenames: tuple[str, ...] = PARITY_FILES,
) -> dict[str, Any]:
    first_run = Path(first_run)
    second_run = Path(second_run)
    files: list[dict[str, Any]] = []
    ok = True

    for filename in filenames:
        first = first_run / filename
        second = second_run / filename
        first_exists = first.is_file()
        second_exists = second.is_file()
        first_hash = file_sha256(first) if first_exists else None
        second_hash = file_sha256(second) if second_exists else None
        matched = (
            first_exists
            and second_exists
            and first_hash == second_hash
            and first.stat().st_size == second.stat().st_size
        )
        ok = ok and matched
        files.append(
            {
                "path": filename,
                "match": matched,
                "first_exists": first_exists,
                "second_exists": second_exists,
                "first_size_bytes": first.stat().st_size if first_exists else None,
                "second_size_bytes": second.stat().st_size if second_exists else None,
                "first_sha256": first_hash,
                "second_sha256": second_hash,
            }
        )

    return {
        "ok": ok,
        "first_run": str(first_run),
        "second_run": str(second_run),
        "files": files,
    }
