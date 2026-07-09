from __future__ import annotations

import hashlib
import re
import zipfile
from pathlib import Path

from .models import InputValidationError, ValidatedInput

MAX_FILE_SIZE = 100 * 1024 * 1024
ALLOWED_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".pptx",
    ".md",
    ".markdown",
    ".html",
    ".htm",
    ".txt",
    ".png",
    ".jpg",
    ".jpeg",
    ".tif",
    ".tiff",
    ".bmp",
    ".webp",
}

_TEXT_EXTENSIONS = {".md", ".markdown", ".txt"}
_HTML_EXTENSIONS = {".html", ".htm"}
_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"}


def sanitize_stem(filename: str) -> str:
    stem = Path(filename).stem.strip()
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem)
    stem = re.sub(r"-{2,}", "-", stem).strip("._-")
    return (stem or "document")[:80]


def sha256_file(path: Path, block_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(block_size):
            digest.update(chunk)
    return digest.hexdigest()


def _detect_mime(path: Path) -> str:
    try:
        import magic

        return str(magic.from_file(str(path), mime=True))
    except (ImportError, OSError):
        return _detect_mime_by_signature(path)


def _detect_mime_by_signature(path: Path) -> str:
    prefix = path.read_bytes()[:16]
    if prefix.startswith(b"%PDF-"):
        return "application/pdf"
    if prefix.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if prefix.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if prefix.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if prefix.startswith(b"BM"):
        return "image/bmp"
    if prefix.startswith(b"RIFF") and prefix[8:12] == b"WEBP":
        return "image/webp"
    if prefix.startswith(b"PK\x03\x04"):
        return "application/zip"
    try:
        path.read_text(encoding="utf-8")
        return "text/plain"
    except UnicodeDecodeError:
        return "application/octet-stream"


def _validate_office_zip(path: Path, extension: str) -> None:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile as exc:
        raise InputValidationError(f"{extension} file is not a valid Office archive") from exc

    required_prefix = "word/" if extension == ".docx" else "ppt/"
    if not any(name.startswith(required_prefix) for name in names):
        raise InputValidationError(
            f"File content does not match the {extension} extension"
        )


def _is_utf8_text(path: Path) -> bool:
    try:
        content = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return False
    return "\x00" not in content


def _mime_matches(path: Path, extension: str, mime: str) -> bool:
    if extension == ".pdf":
        return mime == "application/pdf"
    if extension in {".docx", ".pptx"}:
        _validate_office_zip(path, extension)
        return mime in {
            "application/zip",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        }
    if extension in _TEXT_EXTENSIONS:
        return _is_utf8_text(path)
    if extension in _HTML_EXTENSIONS:
        return (
            mime in {"text/html", "text/plain", "application/xhtml+xml"}
            and _is_utf8_text(path)
        )
    if extension in _IMAGE_EXTENSIONS:
        return mime.startswith("image/")
    return False


def validate_input(path: Path, max_size: int = MAX_FILE_SIZE) -> ValidatedInput:
    path = path.expanduser().resolve()
    if not path.exists() or not path.is_file():
        raise InputValidationError(f"Input file does not exist: {path}")

    extension = path.suffix.lower()
    if extension not in ALLOWED_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
        raise InputValidationError(
            f"Unsupported extension '{extension or '(none)'}'. Allowed: {allowed}"
        )

    size = path.stat().st_size
    if size == 0:
        raise InputValidationError("Input file is empty")
    if size > max_size:
        raise InputValidationError(
            f"Input is {size / (1024 * 1024):.1f} MB; the limit is "
            f"{max_size / (1024 * 1024):.0f} MB"
        )

    detected_mime = _detect_mime(path)
    if not _mime_matches(path, extension, detected_mime):
        raise InputValidationError(
            f"File content ({detected_mime}) does not match extension {extension}"
        )

    return ValidatedInput(
        path=path,
        safe_stem=sanitize_stem(path.name),
        sha256=sha256_file(path),
        size_bytes=size,
        extension=extension,
        detected_mime=detected_mime,
    )
