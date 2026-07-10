from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import time
from collections.abc import Callable
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
    ArtifactMode,
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
    artifact_mode: ArtifactMode,
) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "run_id": run_dir.name,
        "artifact_mode": artifact_mode.value,
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
        "stage_timings": {},
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


def _coerce_artifact_mode(value: ArtifactMode | str) -> ArtifactMode:
    return value if isinstance(value, ArtifactMode) else ArtifactMode(value)


def _mark_skipped(
    timings: dict[str, Any],
    name: str,
    reason: str,
) -> None:
    timings[name] = {"ran": False, "skipped_reason": reason}


def _run_stage(
    timings: dict[str, Any],
    name: str,
    operation: Callable[[], Any],
) -> Any:
    started = time.perf_counter()
    try:
        result = operation()
    except Exception:
        timings[name] = {
            "ran": True,
            "seconds": round(time.perf_counter() - started, 3),
            "failed": True,
        }
        raise
    timings[name] = {
        "ran": True,
        "seconds": round(time.perf_counter() - started, 3),
    }
    return result


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
) -> tuple[int, int, int, dict[int, Any]]:
    from docling_core.types.doc import PictureItem, TableItem

    pictures_dir.mkdir(parents=True, exist_ok=True)
    tables_dir.mkdir(parents=True, exist_ok=True)
    element_count = 0
    picture_count = 0
    table_count = 0
    table_items: dict[int, Any] = {}

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
            table_items[table_count] = element
            stem = f"table-{table_count:04d}"
            try:
                dataframe = element.export_to_dataframe(doc=document)
                dataframe.to_csv(tables_dir / f"{stem}.csv", index=False)
                dataframe.to_html(tables_dir / f"{stem}.html", index=False)
            except Exception as exc:
                warnings.append(
                    f"Could not export table data {table_count}: {exc}"
                )

    return element_count, table_count, picture_count, table_items


def _export_table_images(
    document: Any,
    table_items: dict[int, Any],
    tables_dir: Path,
    warnings: list[str],
    table_numbers: set[int] | None = None,
) -> int:
    count = 0
    selected = sorted(table_items) if table_numbers is None else sorted(table_numbers)
    for table_number in selected:
        element = table_items.get(table_number)
        if element is None:
            continue
        try:
            image = element.get_image(document)
            if image is not None:
                image.save(tables_dir / f"table-{table_number:04d}.png", "PNG")
                count += 1
        except Exception as exc:
            warnings.append(f"Could not export table image {table_number}: {exc}")
    return count


def _export_repaired_table_evidence(
    document: Any,
    table_items: dict[int, Any],
    repair_records: list[dict[str, Any]],
    evidence_dir: Path,
    warnings: list[str],
) -> int:
    if not repair_records:
        return 0

    from .artifacts import write_json

    exported = 0
    evidence_dir.mkdir(parents=True, exist_ok=True)
    for record in repair_records:
        repair_index = int(record.get("repair_index", exported + 1))
        repair_dir = evidence_dir / f"repair-{repair_index:04d}"
        repair_dir.mkdir(parents=True, exist_ok=True)
        fragments: list[dict[str, Any]] = []
        for table_number in record.get("table_numbers", []):
            element = table_items.get(int(table_number))
            if element is None:
                continue
            filename = f"table-fragment-{int(table_number):04d}.png"
            try:
                image = element.get_image(document)
                if image is not None:
                    image.save(repair_dir / filename, "PNG")
                    fragments.append(
                        {
                            "table_number": int(table_number),
                            "file": filename,
                        }
                    )
            except Exception as exc:
                warnings.append(
                    "Could not export repaired-table evidence for "
                    f"table {table_number}: {exc}"
                )
        metadata = dict(record)
        metadata["evidence_type"] = "pre_merge_table_fragments"
        metadata["fragments"] = fragments
        write_json(repair_dir / "metadata.json", metadata)
        if fragments:
            exported += 1
    return exported


def _export_document(
    conversion_result: Any,
    run_dir: Path,
    source_extension: str,
    warnings: list[str],
    artifact_mode: ArtifactMode,
    stage_timings: dict[str, Any],
) -> tuple[dict[str, int], dict[str, Any]]:
    from docling_core.types.doc import ImageRefMode

    document = conversion_result.document
    assets_dir = Path("assets")
    _run_stage(
        stage_timings,
        "document_json_export",
        lambda: document.save_as_json(
            run_dir / "document.json",
            artifacts_dir=assets_dir,
            image_mode=ImageRefMode.REFERENCED,
        ),
    )
    document_data = json.loads(
        (run_dir / "document.json").read_text(encoding="utf-8")
    )
    raw_markdown_path = run_dir / "document.raw.md"
    markdown_path = run_dir / "document.md"

    table_repair_records: list[dict[str, Any]] = []

    def export_markdown() -> None:
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
                table_repair_records,
            ),
            encoding="utf-8",
        )

    _run_stage(stage_timings, "markdown_export", export_markdown)

    if artifact_mode is ArtifactMode.FULL:
        _run_stage(
            stage_timings,
            "html_export",
            lambda: document.save_as_html(
                run_dir / "document.html",
                artifacts_dir=assets_dir,
                image_mode=ImageRefMode.REFERENCED,
            ),
        )
        _run_stage(
            stage_timings,
            "embedded_html_preview_export",
            lambda: document.save_as_html(
                run_dir / "document_preview.html",
                image_mode=ImageRefMode.EMBEDDED,
            ),
        )
    else:
        _mark_skipped(
            stage_timings,
            "html_export",
            "artifact_mode=extraction",
        )
        _mark_skipped(
            stage_timings,
            "embedded_html_preview_export",
            "artifact_mode=extraction",
        )

    _run_stage(
        stage_timings,
        "text_export",
        lambda: (run_dir / "document.txt").write_text(
            document.export_to_text(),
            encoding="utf-8",
        ),
    )

    if artifact_mode is ArtifactMode.FULL:
        page_count = _run_stage(
            stage_timings,
            "page_image_export",
            lambda: _export_pages(document, run_dir / "pages", warnings),
        )
    else:
        page_count = 0
        _mark_skipped(
            stage_timings,
            "page_image_export",
            "artifact_mode=extraction",
        )

    element_count, table_count, picture_count, table_items = _run_stage(
        stage_timings,
        "item_export",
        lambda: _export_items(
            document,
            run_dir / "pictures",
            run_dir / "tables",
            warnings,
        ),
    )

    repaired_table_numbers = {
        int(number)
        for record in table_repair_records
        for number in record.get("table_numbers", [])
    }
    if artifact_mode is ArtifactMode.FULL:
        exported_table_images = _run_stage(
            stage_timings,
            "table_image_export",
            lambda: _export_table_images(
                document,
                table_items,
                run_dir / "tables",
                warnings,
            ),
        )
    elif repaired_table_numbers:
        exported_table_images = _run_stage(
            stage_timings,
            "table_image_export",
            lambda: _export_table_images(
                document,
                table_items,
                run_dir / "tables",
                warnings,
                repaired_table_numbers,
            ),
        )
    else:
        exported_table_images = 0
        _mark_skipped(
            stage_timings,
            "table_image_export",
            "artifact_mode=extraction and no repaired tables",
        )

    if table_repair_records:
        repaired_evidence_count = _run_stage(
            stage_timings,
            "repaired_table_evidence_export",
            lambda: _export_repaired_table_evidence(
                document,
                table_items,
                table_repair_records,
                run_dir / "repaired_table_evidence",
                warnings,
            ),
        )
    else:
        repaired_evidence_count = 0
        _mark_skipped(
            stage_timings,
            "repaired_table_evidence_export",
            "no repaired tables",
        )

    picture_text_records: list[dict[str, Any]] = []
    picture_text_path = run_dir / "picture_text.jsonl"
    if source_extension == ".pdf" and picture_count:
        picture_text_records = _run_stage(
            stage_timings,
            "picture_text_extraction",
            lambda: extract_picture_text(
                run_dir / "pictures",
                run_dir / "document.json",
                picture_text_path,
                warnings,
            ),
        )
        _run_stage(
            stage_timings,
            "picture_text_markdown_injection",
            lambda: markdown_path.write_text(
                inject_picture_text(
                    markdown_path.read_text(encoding="utf-8"),
                    picture_text_records,
                ),
                encoding="utf-8",
            ),
        )
    else:
        picture_text_path.write_text("", encoding="utf-8")
        _mark_skipped(
            stage_timings,
            "picture_text_extraction",
            "not a PDF or no pictures detected",
        )
        _mark_skipped(
            stage_timings,
            "picture_text_markdown_injection",
            "not a PDF or no picture text records",
        )

    def export_chunks() -> tuple[list[dict[str, Any]], dict[str, Any]]:
        chunks, chunking_config = build_chunks(document)
        write_chunks_jsonl(run_dir / "chunks.jsonl", chunks)
        return chunks, chunking_config

    chunks, chunking_config = _run_stage(stage_timings, "chunking", export_chunks)

    counts = {
        "pages": len(document.pages),
        "exported_page_images": page_count,
        "elements": element_count,
        "tables": table_count,
        "exported_table_images": exported_table_images,
        "repaired_tables": len(table_repair_records),
        "repaired_table_evidence": repaired_evidence_count,
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
    artifact_mode: ArtifactMode | str = ArtifactMode.FULL,
) -> RunResult:
    source = validate_input(Path(input_path))
    settings = get_profile(profile)
    mode = _coerce_artifact_mode(artifact_mode)
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = _make_run_dir(output_root, source)
    manifest = _base_manifest(source, settings, run_dir, mode)
    manifest_path = run_dir / "manifest.json"
    write_json(manifest_path, manifest)
    initialize_evaluation(run_dir)
    started = time.perf_counter()
    stage_timings = manifest["stage_timings"]

    lock = FileLock(str(output_root.resolve() / ".parse.lock"))
    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        manifest["status"] = RunStatus.FAILED.value
        manifest["completed_at"] = _utc_now()
        manifest["warnings"].append("Another document is already being parsed")
        write_json(manifest_path, manifest)
        if mode is ArtifactMode.FULL:
            _run_stage(
                stage_timings,
                "archive_zip",
                lambda: build_archive(run_dir),
            )
        else:
            _mark_skipped(
                stage_timings,
                "archive_zip",
                "artifact_mode=extraction",
            )
        write_json(manifest_path, manifest)
        raise ParseBusyError(
            f"Another document is already being parsed. Diagnostics: {run_dir}"
        ) from exc

    try:
        from docling.datamodel.base_models import ConversionStatus, InputFormat
        from docling.datamodel.settings import settings as docling_settings
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

        previous_profile_timings = docling_settings.debug.profile_pipeline_timings
        docling_settings.debug.profile_pipeline_timings = True
        try:
            conversion_result = _run_stage(
                stage_timings,
                "docling_conversion",
                lambda: converter.convert(
                    source.path,
                    raises_on_error=False,
                    max_num_pages=MAX_NUM_PAGES,
                    max_file_size=MAX_FILE_SIZE,
                ),
            )
        finally:
            docling_settings.debug.profile_pipeline_timings = (
                previous_profile_timings
            )

        if conversion_result.timings:
            stage_timings["docling_conversion"]["native_timings_available"] = True
        else:
            stage_timings["docling_conversion"]["native_timings_available"] = False
            manifest["warnings"].append(
                "Docling native conversion timings were empty even with "
                "profile_pipeline_timings enabled"
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
                mode,
                stage_timings,
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
    if mode is ArtifactMode.FULL:
        _run_stage(
            stage_timings,
            "archive_zip",
            lambda: build_archive(run_dir),
        )
    else:
        _mark_skipped(
            stage_timings,
            "archive_zip",
            "artifact_mode=extraction",
        )
    manifest["artifacts"] = artifact_inventory(run_dir)
    write_json(manifest_path, manifest)
    return RunResult(
        run_dir=run_dir,
        status=RunStatus(manifest["status"]),
        manifest=manifest,
        warnings=list(manifest["warnings"]),
    )
