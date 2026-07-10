from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class ParsingProfile(str, Enum):
    STANDARD = "standard"
    SCANNED = "scanned"
    VISUAL = "visual"


class RunStatus(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    PARTIAL_SUCCESS = "partial_success"
    FAILED = "failed"


@dataclass(frozen=True)
class ProfileSettings:
    name: ParsingProfile
    do_ocr: bool = True
    force_full_page_ocr: bool = False
    do_table_structure: bool = True
    table_mode: str = "accurate"
    do_cell_matching: bool = True
    heading_hierarchy: bool = True
    generate_parsed_pages: bool = True
    generate_page_images: bool = True
    generate_picture_images: bool = True
    image_scale: float = 2.0
    do_picture_classification: bool = False
    do_chart_extraction: bool = False
    do_picture_description: bool = False
    do_code_enrichment: bool = False
    do_formula_enrichment: bool = False
    enable_remote_services: bool = False
    allow_external_plugins: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["name"] = self.name.value
        return data


@dataclass
class RunResult:
    run_dir: Path
    status: RunStatus
    manifest: dict[str, Any]
    warnings: list[str] = field(default_factory=list)

@dataclass(frozen=True)
class ValidatedInput:
    path: Path
    safe_stem: str
    sha256: str
    size_bytes: int
    extension: str
    detected_mime: str


class HarnessError(RuntimeError):
    """Base error shown consistently by the UI and CLI."""


class InputValidationError(HarnessError):
    """Raised when a local input file is unsafe or unsupported."""


class ParseBusyError(HarnessError):
    """Raised when another local conversion already owns the parser lock."""


class EvaluationError(HarnessError):
    """Raised when a manual evaluation payload is invalid."""
