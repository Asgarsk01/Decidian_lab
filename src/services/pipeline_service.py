from __future__ import annotations

from pathlib import Path
from typing import Callable

from sqlalchemy.orm import Session

from src.config import get_settings
from src.db import session_scope
from src.models import Document
from src.services.decision_extractor import extract_decision_candidates, validate_candidates
from src.services.docx_parser import parse_docx
from src.services.gemini_client import GeminiClient
from src.services.image_analyzer import analyze_images
from src.services.image_extractor import extract_all_images
from src.services.log_service import log_event
from src.services.packet_builder import build_packets
from src.services.storage_service import StorageService


StageCallback = Callable[[str, str], None]


def _notify(callback: StageCallback | None, stage: str, message: str) -> None:
    if callback:
        callback(stage, message)


def register_upload(filename: str, doc_type: str, payload: bytes) -> str:
    storage = StorageService()
    storage.validate_docx_filename(filename)
    document_id = storage.new_document_id()
    file_path = storage.save_upload(document_id, filename, payload)
    with session_scope() as session:
        session.add(Document(
            id=document_id,
            filename=Path(filename).name,
            file_path=str(file_path),
            doc_type=doc_type if doc_type in {"srs", "rfc", "proposal", "other"} else "other",
            status="uploaded",
        ))
        log_event(session, document_id, "upload", f"Uploaded {filename}")
    return document_id


def _log_failure(document_id: str, stage: str, exc: Exception) -> None:
    with session_scope() as session:
        log_event(session, document_id, stage, str(exc), level="error")
        document = session.get(Document, document_id)
        if document:
            document.status = f"{stage}_failed"


def run_full_pipeline(document_id: str, callback: StageCallback | None = None) -> dict:
    settings = get_settings()
    storage = StorageService(settings)
    results: dict = {"document_id": document_id}

    try:
        _notify(callback, "parse", "Parsing DOCX with Docling and normalizing blocks")
        with session_scope() as session:
            document = session.get(Document, document_id)
            if document is None:
                raise ValueError(f"Unknown document: {document_id}")
            parsed = parse_docx(
                document.id, document.filename, document.doc_type, Path(document.file_path), storage, session
            )
            document.status = "parsed"
            log_event(session, document_id, "parse", f"Parsed {len(parsed['blocks'])} document blocks")
            results["block_count"] = len(parsed["blocks"])
    except Exception as exc:
        _log_failure(document_id, "parse", exc)
        raise

    try:
        _notify(callback, "images", "Extracting every image occurrence from the DOCX package")
        with session_scope() as session:
            document = session.get(Document, document_id)
            images, warnings = extract_all_images(document_id, Path(document.file_path), storage, session)
            document.status = "images_extracted"
            log_event(session, document_id, "image_extraction", f"Extracted {len(images)} image occurrences")
            for warning in warnings:
                log_event(session, document_id, "image_extraction", warning, level="warning")
            results["image_count"] = len(images)
    except Exception as exc:
        _log_failure(document_id, "image_extraction", exc)
        raise

    try:
        client = GeminiClient(settings)
    except Exception as exc:
        _log_failure(document_id, "gemini_configuration", exc)
        raise

    try:
        _notify(callback, "vision", "Analyzing all unique images with Gemini Vision")
        with session_scope() as session:
            def image_error(image_id, exc):
                log_event(session, document_id, "image_analysis", f"{image_id}: {exc}", level="error")

            analyses = analyze_images(document_id, storage, session, client, settings, image_error)
            document = session.get(Document, document_id)
            document.status = "images_analyzed"
            log_event(session, document_id, "image_analysis", f"Stored {len(analyses)} image analyses")
            results["image_analysis_count"] = len(analyses)
    except Exception as exc:
        _log_failure(document_id, "image_analysis", exc)
        raise

    _notify(callback, "packets", "Building section-wise extraction packets")
    with session_scope() as session:
        packets = build_packets(document_id, storage, session)
        document = session.get(Document, document_id)
        document.status = "packets_built"
        log_event(session, document_id, "packet_builder", f"Built {len(packets)} section packets")
        results["packet_count"] = len(packets)

    try:
        _notify(callback, "decisions", "Extracting decision candidates section by section")
        with session_scope() as session:
            def packet_error(section_key, exc):
                log_event(session, document_id, "decision_extraction", f"{section_key}: {exc}", level="error")

            outputs = extract_decision_candidates(document_id, storage, session, client, settings, packet_error)
            candidate_count = sum(len(item["candidates"]) for item in outputs)
            document = session.get(Document, document_id)
            document.status = "candidates_extracted"
            log_event(session, document_id, "decision_extraction", f"Stored {candidate_count} candidates")
            results["candidate_count"] = candidate_count
    except Exception as exc:
        _log_failure(document_id, "decision_extraction", exc)
        raise

    try:
        _notify(callback, "validation", "Validating candidate support and checking duplicates")
        with session_scope() as session:
            def validation_error(candidate_id, exc):
                log_event(session, document_id, "validation", f"Candidate {candidate_id}: {exc}", level="warning")

            validation_report = validate_candidates(document_id, storage, session, client, settings, validation_error)
            document = session.get(Document, document_id)
            document.status = "pending_review"
            log_event(session, document_id, "validation", f"Validated {len(validation_report)} candidates")
            results["validation_count"] = len(validation_report)
    except Exception as exc:
        _log_failure(document_id, "validation", exc)
        raise

    _notify(callback, "complete", "Pipeline complete; candidates await human review")
    return results
