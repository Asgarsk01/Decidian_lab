from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import orjson

from src.config import Settings, get_settings


STORAGE_AREAS = (
    "uploads", "parsed", "images", "image_analysis", "extraction_packets", "decision_outputs"
)


class StorageService:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.root = self.settings.storage_dir
        self.ensure_layout()

    def ensure_layout(self) -> None:
        for area in STORAGE_AREAS:
            (self.root / area).mkdir(parents=True, exist_ok=True)
        (self.settings.project_root / "data").mkdir(parents=True, exist_ok=True)

    @staticmethod
    def validate_docx_filename(filename: str) -> None:
        if Path(filename).suffix.lower() != ".docx":
            raise ValueError("Unsupported file type. Decidian accepts DOCX files only.")

    @staticmethod
    def new_document_id() -> str:
        return f"doc_{uuid4().hex}"

    def document_dir(self, area: str, document_id: str) -> Path:
        if area not in STORAGE_AREAS:
            raise ValueError(f"Unknown storage area: {area}")
        target = self.root / area / document_id
        target.mkdir(parents=True, exist_ok=True)
        return target

    def save_upload(self, document_id: str, filename: str, payload: bytes) -> Path:
        self.validate_docx_filename(filename)
        target = self.document_dir("uploads", document_id) / "original.docx"
        target.write_bytes(payload)
        return target

    @staticmethod
    def write_json(path: Path, payload: object) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_bytes(orjson.dumps(payload, option=orjson.OPT_INDENT_2))
        temp.replace(path)

    @staticmethod
    def read_json(path: Path) -> object:
        return orjson.loads(path.read_bytes())

    @staticmethod
    def write_jsonl(path: Path, rows: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = b"\n".join(orjson.dumps(row) for row in rows)
        if body:
            body += b"\n"
        path.write_bytes(body)

