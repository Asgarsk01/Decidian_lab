from __future__ import annotations

from io import BytesIO
import json
import math
from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

from .models import EvaluationError

QUALITY_FIELDS = (
    "reading_order",
    "headings",
    "tables",
    "ocr",
    "images",
    "provenance",
    "chunk_quality",
)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            _json_safe(payload),
            indent=2,
            ensure_ascii=False,
            default=str,
            allow_nan=False,
        ),
        encoding="utf-8",
    )


def _json_safe(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def artifact_inventory(run_dir: Path) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for path in sorted(run_dir.rglob("*")):
        if not path.is_file():
            continue
        inventory.append(
            {
                "path": path.relative_to(run_dir).as_posix(),
                "size_bytes": path.stat().st_size,
            }
        )
    return inventory


def build_download_archive(run_dir: Path) -> bytes:
    """Create an on-demand ZIP of the generated run without writing a ZIP file."""
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(run_dir.rglob("*")):
            if path.is_file():
                bundle.write(path, arcname=path.relative_to(run_dir).as_posix())
    return buffer.getvalue()


def initialize_evaluation(run_dir: Path) -> Path:
    output = run_dir / "evaluation.json"
    write_json(
        output,
        {
            "status": "pending",
            "scores": {field: None for field in QUALITY_FIELDS},
            "total": None,
            "maximum": len(QUALITY_FIELDS) * 2,
            "notes": "",
        },
    )
    return output


def save_evaluation(
    run_dir: Path,
    scores: dict[str, int],
    notes: str = "",
) -> Path:
    missing = set(QUALITY_FIELDS) - set(scores)
    unexpected = set(scores) - set(QUALITY_FIELDS)
    if missing or unexpected:
        raise EvaluationError(
            f"Evaluation fields mismatch; missing={sorted(missing)}, "
            f"unexpected={sorted(unexpected)}"
        )
    if any(not isinstance(score, int) or score not in {0, 1, 2} for score in scores.values()):
        raise EvaluationError("Every quality score must be the integer 0, 1, or 2")

    payload = {
        "status": "completed",
        "scores": scores,
        "total": sum(scores.values()),
        "maximum": len(QUALITY_FIELDS) * 2,
        "notes": notes.strip(),
    }
    output = run_dir / "evaluation.json"
    write_json(output, payload)
    return output
