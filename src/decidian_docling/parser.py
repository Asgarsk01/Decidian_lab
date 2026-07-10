from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from .artifacts import (
    artifact_inventory,
    build_archive,
    initialize_evaluation,
    write_json,
)
from .chunking import MAX_TOKENS, TOKENIZER_MODEL, build_chunks, write_chunks_jsonl
from .models import (
    ParseBusyError,
    ParsingProfile,
    RunResult,
    RunStatus,
    ValidatedInput,
)
from .postprocess import (
    clean_markdown_for_llm,
    extract_picture_text,
    inject_picture_text,
    normalize_markdown_export,
)
from .profiles import build_pdf_pipeline_options, get_profile
from .validation import MAX_FILE_SIZE, validate_input

DEFAULT_OUTPUT_DIR = Path(os.getenv("DECIDIAN_OUTPUT_DIR", "output"))
MAX_NUM_PAGES = 500


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_run_dir(output_root: Path, source: ValidatedInput) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir = output_root.resolve() / (
        f"{source.safe_stem}__{source.sha256[:8]}__{timestamp}"
    )
    run_dir.mkdir(parents=True, exist_ok=False)
    return run_dir


def _package_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _base_manifest(
    source: ValidatedInput,
    profile_settings: Any,
    run_dir: Path,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "run_id": run_dir.name,
        "status": RunStatus.RUNNING.value,
        "started_at": _utc_now(),
        "completed_at": None,
        "source": {
            "filename": source.path.name,
            "sha256": source.sha256,
            "size_bytes": source.size_bytes,
            "extension": source.extension,
            "detected_mime": source.detected_mime,
        },
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "docling": _package_version("docling"),
            "docling_core": _package_version("docling-core"),
            "docling_ibm_models": _package_version("docling-ibm-models"),
            "torch": _package_version("torch"),
            "transformers": _package_version("transformers"),
            "sentence_transformers": _package_version("sentence-transformers"),
            "rapidocr": _package_version("rapidocr"),
        },
        "models": {
            "chunk_tokenizer": TOKENIZER_MODEL,
            "chunk_token_limit": MAX_TOKENS,
            "docling_models": "resolved by pinned Docling model packages",
            "ocr": "Docling OcrAutoOptions (RapidOCR on the CPU image)",
        },
        "profile": profile_settings.to_dict(),
        "chunking": None,
        "conversion": {
            "status": None,
            "errors": [],
            "timings": {},
            "confidence": None,
        },
        "counts": {
            "pages": 0,
            "elements": 0,
            "tables": 0,
            "pictures": 0,
            "chunks": 0,
        },
        "duration_seconds": None,
        "warnings": [],
        "artifacts": [],
    }


def _model_dump(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return _model_dump(value.model_dump(mode="json", exclude_none=True))
    if isinstance(value, dict):
        return {str(key): _model_dump(item) for key, item in value.items()}
    return str(value)


def _export_pages(document: Any, directory: Path, warnings: list[str]) -> int:
    directory.mkdir(parents=True, exist_ok=True)
    count = 0
    for page_no, page in sorted(document.pages.items()):
        try:
            image = getattr(page, "image", None)
            pil_image = getattr(image, "pil_image", None)
            if pil_image is None:
                warnings.append(f"Page {page_no} has no generated preview image")
                continue
            pil_image.save(directory / f"page-{int(page_no):04d}.png", "PNG")
            count += 1
        except Exception as exc:
            warnings.append(f"Could not export page {page_no}: {exc}")
    return count


def _export_items(
    document: Any,
    pictures_dir: Path,
    tables_dir: Path,
    warnings: list[str],
) -> tuple[int, int, int]:
    from docling_core.types.doc import PictureItem, TableItem

    pictures_dir.mkdir(parents=True, exist_ok=True)
    tables_dir.mkdir(parents=True, exist_ok=True)
    element_count = 0
    picture_count = 0
    table_count = 0

    for element, _level in document.iterate_items():
        element_count += 1
        if isinstance(element, PictureItem):
            picture_count += 1
            try:
                image = element.get_image(document)
                if image is not None:
                    image.save(
                        pictures_dir / f"picture-{picture_count:04d}.png",
                        "PNG",
                    )
            except Exception as exc:
                warnings.append(
                    f"Could not export picture {picture_count}: {exc}"
                )
        elif isinstance(element, TableItem):
            table_count += 1
            stem = f"table-{table_count:04d}"
            try:
                image = element.get_image(document)
                if image is not None:
                    image.save(tables_dir / f"{stem}.png", "PNG")
            except Exception as exc:
                warnings.append(
                    f"Could not export table image {table_count}: {exc}"
                )
            try:
                dataframe = element.export_to_dataframe(doc=document)
                dataframe.to_csv(tables_dir / f"{stem}.csv", index=False)
                dataframe.to_html(tables_dir / f"{stem}.html", index=False)
            except Exception as exc:
                warnings.append(
                    f"Could not export table data {table_count}: {exc}"
                )

    return element_count, table_count, picture_count


def _export_document(
    conversion_result: Any,
    run_dir: Path,
    source_extension: str,
    warnings: list[str],
) -> tuple[dict[str, int], dict[str, Any]]:
    from docling_core.types.doc import ImageRefMode

    document = conversion_result.document
    assets_dir = Path("assets")
    document.save_as_json(
        run_dir / "document.json",
        artifacts_dir=assets_dir,
        image_mode=ImageRefMode.REFERENCED,
    )
    document_data = json.loads(
        (run_dir / "document.json").read_text(encoding="utf-8")
    )
    raw_markdown_path = run_dir / "document.raw.md"
    markdown_path = run_dir / "document.md"
    document.save_as_markdown(
        raw_markdown_path,
        artifacts_dir=assets_dir,
        image_mode=ImageRefMode.REFERENCED,
    )
    raw_markdown = normalize_markdown_export(
        raw_markdown_path.read_text(encoding="utf-8")
    )
    raw_markdown_path.write_text(raw_markdown, encoding="utf-8")
    markdown_path.write_text(
        clean_markdown_for_llm(
            raw_markdown,
            document_data if source_extension == ".pdf" else None,
            warnings,
        ),
        encoding="utf-8",
    )
    document.save_as_html(
        run_dir / "document.html",
        artifacts_dir=assets_dir,
        image_mode=ImageRefMode.REFERENCED,
    )
    document.save_as_html(
        run_dir / "document_preview.html",
        image_mode=ImageRefMode.EMBEDDED,
    )
    (run_dir / "document.txt").write_text(
        document.export_to_text(),
        encoding="utf-8",
    )

    page_count = _export_pages(document, run_dir / "pages", warnings)
    element_count, table_count, picture_count = _export_items(
        document,
        run_dir / "pictures",
        run_dir / "tables",
        warnings,
    )
    picture_text_records: list[dict[str, Any]] = []
    if source_extension == ".pdf" and picture_count:
        picture_text_records = extract_picture_text(
            run_dir / "pictures",
            run_dir / "document.json",
            run_dir / "picture_text.jsonl",
            warnings,
        )
        markdown_path.write_text(
            inject_picture_text(
                markdown_path.read_text(encoding="utf-8"),
                picture_text_records,
            ),
            encoding="utf-8",
        )
    chunks, chunking_config = build_chunks(document)
    write_chunks_jsonl(run_dir / "chunks.jsonl", chunks)

    counts = {
        "pages": len(document.pages),
        "exported_page_images": page_count,
        "elements": element_count,
        "tables": table_count,
        "pictures": picture_count,
        "picture_structured_items": sum(
            record.get("source") == "docling_structured"
            for record in picture_text_records
        ),
        "picture_ocr_items": sum(
            record.get("source") == "tesseract_ocr"
            for record in picture_text_records
        ),
        "chunks": len(chunks),
    }
    return counts, chunking_config


def parse_document(
    input_path: Path,
    profile: ParsingProfile | str = ParsingProfile.STANDARD,
    output_root: Path = DEFAULT_OUTPUT_DIR,
) -> RunResult:
    source = validate_input(Path(input_path))
    settings = get_profile(profile)
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = _make_run_dir(output_root, source)
    manifest = _base_manifest(source, settings, run_dir)
    manifest_path = run_dir / "manifest.json"
    write_json(manifest_path, manifest)
    initialize_evaluation(run_dir)
    started = time.perf_counter()

    lock = FileLock(str(output_root.resolve() / ".parse.lock"))
    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        manifest["status"] = RunStatus.FAILED.value
        manifest["completed_at"] = _utc_now()
        manifest["warnings"].append("Another document is already being parsed")
        write_json(manifest_path, manifest)
        build_archive(run_dir)
        raise ParseBusyError(
            f"Another document is already being parsed. Diagnostics: {run_dir}"
        ) from exc

    try:
        from docling.datamodel.base_models import ConversionStatus, InputFormat
        from docling.document_converter import DocumentConverter, PdfFormatOption

        converter = DocumentConverter(
            allowed_formats=[
                InputFormat.PDF,
                InputFormat.DOCX,
                InputFormat.PPTX,
                InputFormat.MD,
                InputFormat.HTML,
                InputFormat.IMAGE,
            ],
            format_options={
                InputFormat.PDF: PdfFormatOption(
                    pipeline_options=build_pdf_pipeline_options(settings)
                )
            },
        )
        conversion_result = converter.convert(
            source.path,
            raises_on_error=False,
            max_num_pages=MAX_NUM_PAGES,
            max_file_size=MAX_FILE_SIZE,
        )
        manifest["conversion"]["status"] = conversion_result.status.value
        manifest["conversion"]["errors"] = [
            _model_dump(error) for error in conversion_result.errors
        ]
        manifest["conversion"]["timings"] = _model_dump(
            conversion_result.timings
        )
        manifest["conversion"]["confidence"] = _model_dump(
            conversion_result.confidence
        )

        if conversion_result.status is ConversionStatus.FAILURE:
            manifest["status"] = RunStatus.FAILED.value
        else:
            counts, chunking_config = _export_document(
                conversion_result,
                run_dir,
                source.extension,
                manifest["warnings"],
            )
            manifest["counts"].update(counts)
            manifest["chunking"] = chunking_config
            manifest["status"] = (
                RunStatus.PARTIAL_SUCCESS.value
                if conversion_result.status is ConversionStatus.PARTIAL_SUCCESS
                else RunStatus.SUCCESS.value
            )
    except Exception as exc:
        manifest["status"] = RunStatus.FAILED.value
        manifest["conversion"]["errors"].append(
            {
                "category": "harness_exception",
                "type": type(exc).__name__,
                "message": str(exc),
            }
        )
    finally:
        lock.release()

    manifest["completed_at"] = _utc_now()
    manifest["duration_seconds"] = round(time.perf_counter() - started, 3)
    manifest["artifacts"] = artifact_inventory(run_dir)
    write_json(manifest_path, manifest)
    build_archive(run_dir)
    return RunResult(
        run_dir=run_dir,
        status=RunStatus(manifest["status"]),
        manifest=manifest,
        warnings=list(manifest["warnings"]),
    )
