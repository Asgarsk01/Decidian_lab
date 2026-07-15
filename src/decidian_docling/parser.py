from __future__ import annotations

import importlib.metadata
import json
import os
import platform
import re
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from .artifacts import (
    artifact_inventory,
    initialize_evaluation,
    write_json,
)
from .canonical import build_canonical_document, canonical_markdown
from .clean_chunking import build_clean_chunks, build_review_queue, write_jsonl
from .chunking import (
    MAX_TOKENS,
    TOKENIZER_MODEL,
    build_chunks,
    build_integrity_warning_supplement_chunks,
    build_picture_supplement_chunks,
    write_chunks_jsonl,
)
from .models import (
    ParseBusyError,
    ParsingProfile,
    RunResult,
    RunStatus,
    ValidatedInput,
)
from .config import GeminiSettings, get_gemini_settings
from .gemini_review import apply_verified_overlays, run_gemini_review
from .postprocess import (
    clean_markdown_for_llm,
    extract_picture_text_with_coverage,
    inject_picture_integrity_warnings,
    inject_picture_text,
    normalize_markdown_export,
)
from .profiles import build_pdf_pipeline_options, get_profile
from .semantic_integrity import (
    add_picture_integrity_findings,
    annotate_chunks_with_integrity,
    apply_pdf_semantic_integrity,
    empty_integrity_report,
)
from .validation import MAX_FILE_SIZE, validate_input

DEFAULT_OUTPUT_DIR = Path(os.getenv("DECIDIAN_OUTPUT_DIR", "output"))
MAX_NUM_PAGES = 500


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _notify_progress(
    callback: Callable[[dict[str, Any]], None] | None,
    event: dict[str, Any],
) -> None:
    if callback:
        try:
            callback(event)
        except Exception:
            # UI/observer failures must never affect parsing or artifact integrity.
            pass


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
        "artifact_mode": "extraction",
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
            "google_genai": _package_version("google-genai"),
            "python_docx": _package_version("python-docx"),
        },
        "models": {
            "chunk_tokenizer": TOKENIZER_MODEL,
            "chunk_token_limit": MAX_TOKENS,
            "docling_models": "resolved by pinned Docling model packages",
            "ocr": "Docling OcrAutoOptions (RapidOCR on the CPU image)",
        },
        "profile": profile_settings.to_dict(),
        "llm_readiness": "ready",
        "visual_readiness": "ready",
        "clean_readiness": "blocked",
        "ai_review": {
            "status": "not_configured",
            "provider": "google_gemini",
            "model": os.getenv("GEMINI_MODEL", "gemini-3.5-flash"),
            "mode": "targeted_two_pass",
            "candidate_count": 0,
            "verified_count": 0,
            "unresolved_count": 0,
            "estimated_cost_inr": 0.0,
            "budget_inr": 100.0,
            "artifact": "gemini_review.json",
            "events_artifact": "gemini_events.jsonl",
        },
        "provenance_scope": "unavailable",
        "semantic_integrity": {
            "summary": {
                "verified": 0,
                "repaired_high_confidence": 0,
                "review_required": 0,
                "preserved": 0,
            },
            "finding_count": 0,
            "artifact": None,
        },
        "visual_integrity": {
            "summary": {
                "verified": 0,
                "repaired_high_confidence": 0,
                "review_required": 0,
                "preserved": 0,
            },
            "finding_count": 0,
            "artifact": None,
        },
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
            "clean_chunks": 0,
            "review_queue": 0,
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
    if isinstance(value, (list, tuple, set)):
        return [_model_dump(item) for item in value]
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


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


def _remove_redundant_page_assets(
    assets_dir: Path,
    warnings: list[str],
) -> tuple[int, int]:
    """Remove generated page previews after extraction artifacts are complete.

    Docling stores page previews alongside picture assets when JSON uses
    referenced images. The extraction output does not expose or consume page previews;
    retained picture assets and the dedicated ``pictures/`` evidence directory
    cover picture-text verification. Keep this cleanup deliberately narrow so
    embedded document pictures (``image_*``) remain available to Markdown.
    """
    if not assets_dir.exists():
        return 0, 0

    removed_count = 0
    removed_bytes = 0
    for path in assets_dir.glob("page_*"):
        if not path.is_file():
            continue
        try:
            removed_bytes += path.stat().st_size
            path.unlink()
            removed_count += 1
        except OSError as exc:
            warnings.append(f"Could not remove extraction page asset {path.name}: {exc}")
    return removed_count, removed_bytes


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
                dataframe = _normalize_table_dataframe(dataframe)
                dataframe.to_csv(tables_dir / f"{stem}.csv", index=False)
                dataframe.to_html(tables_dir / f"{stem}.html", index=False)
            except Exception as exc:
                warnings.append(
                    f"Could not export table data {table_count}: {exc}"
                )

    return element_count, table_count, picture_count, table_items


def _normalize_table_dataframe(dataframe: Any) -> Any:
    """Remove Markdown presentation syntax from LLM-facing table exports."""
    def normalize(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        value = re.sub(r"(?<!\\)\*\*(.+?)(?<!\\)\*\*", r"\1", value)
        value = re.sub(r"(?<!\\)__(.+?)(?<!\\)__", r"\1", value)
        return value.replace(r"\*", "*").replace(r"\_", "_").strip()

    dataframe = dataframe.copy()
    dataframe.columns = [normalize(str(column)) for column in dataframe.columns]
    return dataframe.apply(lambda column: column.map(normalize))


def _document_provenance_scope(chunks: list[dict[str, Any]]) -> str:
    scopes = {str(chunk.get("provenance_scope", "unavailable")) for chunk in chunks}
    if "section_only" in scopes:
        return "section_only"
    if "page" in scopes:
        return "page"
    return "unavailable"


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


def _export_semantic_integrity_evidence(
    document: Any,
    table_items: dict[int, Any],
    integrity_report: dict[str, Any],
    evidence_dir: Path,
    warnings: list[str],
) -> int:
    findings = [
        finding
        for finding in integrity_report.get("findings", []) or []
        if finding.get("status") in {"repaired_high_confidence", "review_required"}
        and finding.get("table_numbers")
    ]
    if not findings:
        return 0

    from .artifacts import write_json

    exported = 0
    evidence_dir.mkdir(parents=True, exist_ok=True)
    for index, finding in enumerate(findings, start=1):
        finding_dir = evidence_dir / f"{finding.get('id') or f'finding-{index:04d}'}"
        finding_dir.mkdir(parents=True, exist_ok=True)
        fragments: list[dict[str, Any]] = []
        for table_number in finding.get("table_numbers", []):
            element = table_items.get(int(table_number))
            if element is None:
                continue
            filename = f"table-fragment-{int(table_number):04d}.png"
            try:
                image = element.get_image(document)
                if image is not None:
                    image.save(finding_dir / filename, "PNG")
                    fragments.append(
                        {
                            "table_number": int(table_number),
                            "file": filename,
                        }
                    )
            except Exception as exc:
                warnings.append(
                    "Could not export semantic-integrity evidence for "
                    f"table {table_number}: {exc}"
                )
        metadata = dict(finding)
        metadata["evidence_type"] = "semantic_integrity_table_fragments"
        metadata["fragments"] = fragments
        write_json(finding_dir / "metadata.json", metadata)
        if fragments:
            exported += 1
    return exported


def _export_document(
    conversion_result: Any,
    run_dir: Path,
    source: ValidatedInput,
    gemini_settings: GeminiSettings,
    warnings: list[str],
    stage_timings: dict[str, Any],
    ai_progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> tuple[
    dict[str, int],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    str,
]:
    from docling_core.types.doc import ImageRefMode

    document = conversion_result.document
    source_extension = source.extension
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
    table_header_repair_records: list[dict[str, str]] = []
    semantic_document = document
    semantic_integrity_report = empty_integrity_report(
        "pdf" if source_extension == ".pdf" else source_extension.lstrip(".") or "document"
    )
    visual_integrity_report = empty_integrity_report("visual")

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

        nonlocal semantic_document, semantic_integrity_report
        repaired_raw_markdown = raw_markdown
        if source_extension == ".pdf":
            semantic_markdown_path = run_dir / ".document.semantic.raw.md"
            try:
                semantic_document, semantic_integrity_report = (
                    apply_pdf_semantic_integrity(document, document_data, warnings)
                )
                semantic_document.save_as_markdown(
                    semantic_markdown_path,
                    artifacts_dir=assets_dir,
                    image_mode=ImageRefMode.REFERENCED,
                )
                repaired_raw_markdown = normalize_markdown_export(
                    semantic_markdown_path.read_text(encoding="utf-8")
                )
            except Exception as exc:
                warnings.append(
                    "PDF semantic integrity Markdown regeneration skipped after "
                    f"unexpected error: {exc}"
                )
                semantic_document = document
                semantic_integrity_report = {
                    "schema_version": "1.0",
                    "scope": "pdf",
                    "llm_readiness": "review_required",
                    "summary": {
                        "verified": 0,
                        "repaired_high_confidence": 0,
                        "review_required": 1,
                        "preserved": 0,
                    },
                    "findings": [
                        {
                            "id": "si-0001",
                            "category": "integrity_markdown_regeneration_exception",
                            "status": "review_required",
                            "message": "Semantic integrity Markdown regeneration failed open; baseline Docling Markdown was preserved.",
                            "rationale": [str(exc)],
                            "table_indexes": [],
                            "table_numbers": [],
                            "source_table_refs": [],
                            "pages": [],
                            "affected_artifacts": ["document.md", "chunks.jsonl"],
                            "source_refs": [],
                            "provenance_scope": "unavailable",
                            "llm_warning": (
                                "SEMANTIC INTEGRITY WARNING: Integrity analysis failed. "
                                "Do not use this parse for unattended decision extraction."
                            ),
                            "blocks_llm_readiness": True,
                        }
                    ],
                }
            finally:
                semantic_markdown_path.unlink(missing_ok=True)

        markdown_path.write_text(
            clean_markdown_for_llm(
                repaired_raw_markdown,
                document_data if source_extension == ".pdf" else None,
                warnings,
                table_repair_records,
                table_header_repair_records,
            ),
            encoding="utf-8",
        )

    _run_stage(stage_timings, "markdown_export", export_markdown)

    _mark_skipped(
        stage_timings,
        "html_export",
        "not part of the extraction artifact set",
    )
    _mark_skipped(
        stage_timings,
        "embedded_html_preview_export",
        "not part of the extraction artifact set",
    )

    _run_stage(
        stage_timings,
        "text_export",
        lambda: (run_dir / "document.txt").write_text(
            document.export_to_text(),
            encoding="utf-8",
        ),
    )

    page_count = 0
    _mark_skipped(
        stage_timings,
        "page_image_export",
        "not part of the extraction artifact set",
    )

    element_count, table_count, picture_count, table_items = _run_stage(
        stage_timings,
        "item_export",
        lambda: _export_items(
            semantic_document,
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
    if repaired_table_numbers:
        exported_table_images = _run_stage(
            stage_timings,
            "table_image_export",
            lambda: _export_table_images(
                semantic_document,
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
            "no repaired tables",
        )

    if table_repair_records:
        repaired_evidence_count = _run_stage(
            stage_timings,
            "repaired_table_evidence_export",
            lambda: _export_repaired_table_evidence(
                semantic_document,
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
    picture_coverage: list[dict[str, Any]] = []
    picture_text_path = run_dir / "picture_text.jsonl"
    if picture_count:
        picture_text_records, picture_coverage = _run_stage(
            stage_timings,
            "picture_text_extraction",
            lambda: extract_picture_text_with_coverage(
                run_dir / "pictures",
                run_dir / "document.json",
                picture_text_path,
                warnings,
            ),
        )
        visual_integrity_report = add_picture_integrity_findings(
            visual_integrity_report,
            picture_coverage,
        )
        _run_stage(
            stage_timings,
            "picture_text_markdown_injection",
            lambda: markdown_path.write_text(
                inject_picture_integrity_warnings(
                    inject_picture_text(
                        markdown_path.read_text(encoding="utf-8"),
                        picture_text_records,
                    ),
                    picture_coverage,
                ),
                encoding="utf-8",
            ),
        )
    else:
        picture_text_path.write_text("", encoding="utf-8")
        _mark_skipped(
            stage_timings,
            "picture_text_extraction",
            "no pictures detected",
        )
        _mark_skipped(
            stage_timings,
            "picture_text_markdown_injection",
            "no pictures detected",
        )

    _run_stage(
        stage_timings,
        "semantic_integrity_export",
        lambda: write_json(run_dir / "semantic_integrity.json", semantic_integrity_report),
    )
    _run_stage(
        stage_timings,
        "visual_integrity_export",
        lambda: write_json(run_dir / "visual_integrity.json", visual_integrity_report),
    )
    semantic_evidence_count = _run_stage(
        stage_timings,
        "semantic_integrity_evidence_export",
        lambda: _export_semantic_integrity_evidence(
            semantic_document,
            table_items,
            semantic_integrity_report,
            run_dir / "semantic_integrity_evidence",
            warnings,
        ),
    )

    canonical_result = _run_stage(
        stage_timings,
        "canonical_reconciliation",
        lambda: build_canonical_document(
            source.path,
            document_data,
            semantic_integrity_report,
            visual_integrity_report,
            picture_text_records,
        ),
    )
    if source_extension not in {".docx", ".pdf"}:
        canonical_result.candidates.clear()
        canonical_result.document["summary"]["ai_candidates"] = 0
    write_json(run_dir / "canonical_document.json", canonical_result.document)

    # Persist a deterministic safe feed before external review starts. If the AI
    # job is cancelled or the container exits, core DOCX/PDF content is still usable.
    preliminary_clean_chunks, _ = build_clean_chunks(
        canonical_result.document,
        [],
    )
    preliminary_review_queue = build_review_queue(
        canonical_result.document,
        canonical_result.candidates,
        {},
    )
    write_jsonl(run_dir / "clean_chunks.jsonl", preliminary_clean_chunks)
    write_jsonl(run_dir / "review_queue.jsonl", preliminary_review_queue)
    _notify_progress(
        ai_progress_callback,
        {
            "event": "local_processing_completed",
            "phase": "local_processing",
            "message": (
                f"Local parsing completed. {len(canonical_result.candidates)} AI "
                "candidate(s) detected; deterministic clean chunks are already saved."
            ),
            "detected_candidates": len(canonical_result.candidates),
            "detected_diagrams": sum(
                item.get("kind") == "diagram"
                for item in canonical_result.candidates
            ),
            "preliminary_clean_chunks": len(preliminary_clean_chunks),
        },
    )

    ai_report, ai_results, verified_supplements = _run_stage(
        stage_timings,
        "gemini_review",
        lambda: run_gemini_review(
            canonical_result.candidates,
            run_dir,
            gemini_settings,
            run_dir.parent / ".ai_cache",
            ai_progress_callback,
        ),
    )
    apply_verified_overlays(
        canonical_result.document,
        canonical_result.candidates,
        ai_results,
    )
    write_json(run_dir / "canonical_document.json", canonical_result.document)
    write_json(run_dir / "gemini_review.json", ai_report)

    canonical_review_markdown = canonical_markdown(canonical_result.document)
    markdown_path.write_text(
        inject_picture_integrity_warnings(
            inject_picture_text(canonical_review_markdown, picture_text_records),
            picture_coverage,
        ),
        encoding="utf-8",
    )

    clean_chunks, clean_chunking_config = _run_stage(
        stage_timings,
        "clean_chunking",
        lambda: build_clean_chunks(
            canonical_result.document,
            verified_supplements,
        ),
    )
    review_queue = build_review_queue(
        canonical_result.document,
        canonical_result.candidates,
        ai_results,
    )
    write_jsonl(run_dir / "clean_chunks.jsonl", clean_chunks)
    write_jsonl(run_dir / "review_queue.jsonl", review_queue)
    clean_readiness = (
        "blocked"
        if not clean_chunks
        else "partial_ready"
        if review_queue
        else "ready"
    )

    def export_chunks() -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        core_chunks, chunking_config = build_chunks(semantic_document)
        picture_supplements = build_picture_supplement_chunks(picture_text_records)
        integrity_supplements = build_integrity_warning_supplement_chunks(
            visual_integrity_report.get("findings", []) or []
        )
        annotate_chunks_with_integrity(core_chunks, semantic_integrity_report)
        visual_chunks = [*picture_supplements, *integrity_supplements]
        annotate_chunks_with_integrity(visual_chunks, visual_integrity_report)
        for index, chunk in enumerate(core_chunks):
            chunk["index"] = index
        for index, chunk in enumerate(visual_chunks):
            chunk["index"] = index
        chunking_config.update(
            {
                "base_document_chunks": len(core_chunks),
                "picture_supplement_chunks": len(picture_supplements),
                "visual_integrity_warning_supplement_chunks": len(integrity_supplements),
                "visual_chunks_artifact": "picture_chunks.jsonl",
                "provenance_scope": _document_provenance_scope(core_chunks),
                "semantic_integrity": {
                    "llm_readiness": semantic_integrity_report.get("llm_readiness"),
                    "finding_count": len(
                        semantic_integrity_report.get("findings", []) or []
                    ),
                },
                "visual_integrity": {
                    "llm_readiness": visual_integrity_report.get("llm_readiness"),
                    "finding_count": len(
                        visual_integrity_report.get("findings", []) or []
                    ),
                },
            }
        )
        write_chunks_jsonl(run_dir / "chunks.jsonl", core_chunks)
        write_chunks_jsonl(run_dir / "picture_chunks.jsonl", visual_chunks)
        return core_chunks, visual_chunks, chunking_config

    chunks, visual_chunks, chunking_config = _run_stage(
        stage_timings,
        "chunking",
        export_chunks,
    )

    removed_page_assets, removed_page_asset_bytes = _run_stage(
        stage_timings,
        "page_asset_cleanup",
        lambda: _remove_redundant_page_assets(run_dir / assets_dir, warnings),
    )

    counts = {
        "pages": len(document.pages),
        "exported_page_images": page_count,
        "removed_page_preview_assets": removed_page_assets,
        "removed_page_preview_asset_bytes": removed_page_asset_bytes,
        "elements": element_count,
        "tables": table_count,
        "exported_table_images": exported_table_images,
        "repaired_tables": len(table_repair_records),
        "table_header_fragments_repaired": len(table_header_repair_records),
        "repaired_table_evidence": repaired_evidence_count,
        "semantic_integrity_findings": len(
            semantic_integrity_report.get("findings", []) or []
        ),
        "semantic_integrity_evidence": semantic_evidence_count,
        "visual_integrity_findings": len(
            visual_integrity_report.get("findings", []) or []
        ),
        "pictures": picture_count,
        "picture_structured_items": sum(
            record.get("source") == "docling_structured"
            for record in picture_text_records
        ),
        "picture_ocr_items": sum(
            record.get("source") == "tesseract_ocr"
            for record in picture_text_records
        ),
        "picture_unverified_items": sum(
            item.get("qualifying")
            and item.get("coverage_status") not in {"structured_text", "ocr_text"}
            for item in picture_coverage
        ),
        "chunks": len(chunks),
        "picture_chunks": len(visual_chunks),
        "clean_chunks": len(clean_chunks),
        "review_queue": len(review_queue),
        "canonical_blocks": len(canonical_result.document.get("blocks", []) or []),
        "ai_candidates": len(canonical_result.candidates),
        "ai_verified_claims": len(verified_supplements),
    }
    chunking_config["semantic_integrity_report"] = {
        "llm_readiness": semantic_integrity_report.get("llm_readiness"),
        "summary": semantic_integrity_report.get("summary", {}),
    }
    chunking_config["visual_integrity_report"] = {
        "llm_readiness": visual_integrity_report.get("llm_readiness"),
        "summary": visual_integrity_report.get("summary", {}),
    }
    chunking_config["clean"] = clean_chunking_config
    return (
        counts,
        chunking_config,
        semantic_integrity_report,
        visual_integrity_report,
        ai_report,
        clean_readiness,
    )


def parse_document(
    input_path: Path,
    profile: ParsingProfile | str = ParsingProfile.STANDARD,
    output_root: Path = DEFAULT_OUTPUT_DIR,
    ai_review: bool | None = None,
    ai_progress_callback: Callable[[dict[str, Any]], None] | None = None,
) -> RunResult:
    source = validate_input(Path(input_path))
    settings = get_profile(profile)
    gemini_settings = get_gemini_settings(ai_review)
    output_root = Path(output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    run_dir = _make_run_dir(output_root, source)
    manifest = _base_manifest(source, settings, run_dir)
    manifest_path = run_dir / "manifest.json"
    write_json(manifest_path, manifest)
    initialize_evaluation(run_dir)
    started = time.perf_counter()
    stage_timings = manifest["stage_timings"]
    _notify_progress(
        ai_progress_callback,
        {
            "event": "parse_started",
            "phase": "local_processing",
            "message": "Running local Docling conversion and deterministic reconciliation.",
            "model": gemini_settings.model,
            "thinking_level": gemini_settings.thinking_level,
            "budget_inr": gemini_settings.budget_inr,
        },
    )

    lock = FileLock(str(output_root.resolve() / ".parse.lock"))
    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        manifest["status"] = RunStatus.FAILED.value
        manifest["completed_at"] = _utc_now()
        manifest["warnings"].append("Another document is already being parsed")
        write_json(manifest_path, manifest)
        _mark_skipped(
            stage_timings,
            "archive_zip",
            "not part of the extraction artifact set",
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
            (
                counts,
                chunking_config,
                semantic_integrity_report,
                visual_integrity_report,
                ai_report,
                clean_readiness,
            ) = _export_document(
                conversion_result,
                run_dir,
                source,
                gemini_settings,
                manifest["warnings"],
                stage_timings,
                ai_progress_callback,
            )
            manifest["counts"].update(counts)
            manifest["chunking"] = chunking_config
            manifest["provenance_scope"] = chunking_config.get(
                "provenance_scope",
                "unavailable",
            )
            manifest["llm_readiness"] = semantic_integrity_report.get(
                "llm_readiness",
                "ready",
            )
            manifest["visual_readiness"] = visual_integrity_report.get(
                "llm_readiness",
                "ready",
            )
            manifest["clean_readiness"] = clean_readiness
            manifest["ai_review"] = {
                "status": ai_report.get("status"),
                "provider": ai_report.get("provider", "google_gemini"),
                "model": ai_report.get("model", gemini_settings.model),
                "mode": ai_report.get("mode", "targeted_two_pass"),
                "candidate_count": ai_report.get("candidate_count", 0),
                "verified_count": ai_report.get("verified_count", 0),
                "unresolved_count": ai_report.get("unresolved_count", 0),
                "verified_claim_count": ai_report.get("verified_claim_count", 0),
                "duration_seconds": ai_report.get("duration_seconds", 0.0),
                "selected_candidate_count": ai_report.get("selected_candidate_count", 0),
                "selected_diagram_count": ai_report.get("selected_diagram_count", 0),
                "deferred_candidate_count": ai_report.get("deferred_candidate_count", 0),
                "prepared_candidate_count": ai_report.get("prepared_candidate_count", 0),
                "prepared_evidence_count": ai_report.get("prepared_evidence_count", 0),
                "attempted_candidate_count": ai_report.get("attempted_candidate_count", 0),
                "request_count": ai_report.get("request_count", 0),
                "extraction_request_count": ai_report.get("extraction_request_count", 0),
                "verification_request_count": ai_report.get("verification_request_count", 0),
                "diagram_request_count": ai_report.get("diagram_request_count", 0),
                "verification_count": ai_report.get("verification_count", 0),
                "submitted_candidate_count": ai_report.get("submitted_candidate_count", 0),
                "submitted_candidate_ids": ai_report.get("submitted_candidate_ids", []),
                "submitted_evidence_count": ai_report.get("submitted_evidence_count", 0),
                "submitted_evidence_paths": ai_report.get("submitted_evidence_paths", []),
                "usage": ai_report.get("usage", {}),
                "estimated_cost_inr": ai_report.get("estimated_cost_inr", 0.0),
                "budget_inr": ai_report.get("budget_inr", gemini_settings.budget_inr),
                "budget_remaining_inr": ai_report.get("budget_remaining_inr", gemini_settings.budget_inr),
                "stop_reason": ai_report.get("stop_reason"),
                "artifact": "gemini_review.json",
                "events_artifact": "gemini_events.jsonl",
            }
            manifest["semantic_integrity"] = {
                "summary": semantic_integrity_report.get("summary", {}),
                "finding_count": len(
                    semantic_integrity_report.get("findings", []) or []
                ),
                "artifact": "semantic_integrity.json",
            }
            manifest["visual_integrity"] = {
                "summary": visual_integrity_report.get("summary", {}),
                "finding_count": len(
                    visual_integrity_report.get("findings", []) or []
                ),
                "artifact": "visual_integrity.json",
            }
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
    _mark_skipped(
        stage_timings,
        "archive_zip",
        "not part of the extraction artifact set",
    )
    manifest["artifacts"] = artifact_inventory(run_dir)
    write_json(manifest_path, manifest)
    return RunResult(
        run_dir=run_dir,
        status=RunStatus(manifest["status"]),
        manifest=manifest,
        warnings=list(manifest["warnings"]),
    )
