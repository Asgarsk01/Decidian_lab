from __future__ import annotations

import orjson
from rapidfuzz.fuzz import ratio
from rapidfuzz.utils import default_process
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.config import Settings, get_settings
from src.models import DecisionCandidate, ExtractionPacket
from src.schemas.decision_candidate_schema import (
    CandidateValidationResult,
    DecisionCandidateBatch,
)
from src.services.gemini_client import GeminiClient, GeminiModelUnavailableError
from src.services.storage_service import StorageService


DECISION_EXTRACTION_PROMPT = """You are extracting decision candidates from an SRS/RFC/proposal section.
Extract only actual decisions, business rules, workflows, constraints, validation rules, role permissions,
state transitions, security/integration/data/audit/error-handling rules, or technical implementation rules.

A valid candidate defines expected behavior, is useful for checking implementation, cites supplied source
block/image IDs, and is phrased as a clear mandatory/allowed/forbidden rule. Do not invent missing rules,
turn rejected options into rules, or treat a proposal as final unless explicitly accepted or required.
Lower confidence and explain uncertainty. Every candidate must require human review. Return strict JSON.

Section packet:
{packet}
"""


VALIDATION_PROMPT = """Validate one AI-generated decision candidate against its source section.
Check support, invention, vagueness, rejected-option wording, conflict with the other listed candidates,
and whether confidence is reasonable. Do not rewrite or approve it. Return strict JSON.

Candidate:
{candidate}

Source section:
{packet}

Other candidate statements in this document:
{others}
"""


def extract_decision_candidates(
    document_id: str,
    storage: StorageService,
    session: Session,
    client: GeminiClient,
    settings: Settings | None = None,
    on_packet_error=None,
) -> list[dict]:
    settings = settings or get_settings()
    packets = list(session.scalars(
        select(ExtractionPacket).where(ExtractionPacket.document_id == document_id).order_by(ExtractionPacket.id)
    ))
    session.execute(delete(DecisionCandidate).where(DecisionCandidate.document_id == document_id))
    output_packets: list[dict] = []

    for packet_row in packets:
        packet = orjson.loads(packet_row.packet_json)
        valid_block_ids = {item["block_id"] for item in packet.get("source_blocks", [])}
        valid_image_ids = {item["image_id"] for item in packet.get("image_analyses", [])}
        try:
            batch, raw = client.generate_structured(
                model=settings.gemini_text_model,
                contents=DECISION_EXTRACTION_PROMPT.format(packet=orjson.dumps(packet).decode()),
                schema=DecisionCandidateBatch,
            )
        except Exception as exc:
            if isinstance(exc, GeminiModelUnavailableError):
                raise
            if on_packet_error:
                on_packet_error(packet_row.section_key, exc)
            output_packets.append({
                "extraction_packet_id": packet_row.id,
                "section_key": packet_row.section_key,
                "candidates": [],
                "raw_response": {"error": str(exc)},
            })
            continue

        accepted_payloads: list[dict] = []
        for candidate in batch.candidates:
            candidate.source_block_ids = [item for item in candidate.source_block_ids if item in valid_block_ids]
            candidate.source_image_ids = [item for item in candidate.source_image_ids if item in valid_image_ids]
            if not candidate.source_block_ids and not candidate.source_image_ids:
                if on_packet_error:
                    on_packet_error(
                        packet_row.section_key,
                        ValueError(f"Skipped unsupported candidate with no valid source IDs: {candidate.decision_statement}"),
                    )
                continue
            payload = candidate.model_dump(mode="json")
            row = DecisionCandidate(
                document_id=document_id,
                extraction_packet_id=packet_row.id,
                decision_type=candidate.decision_type,
                decision_statement=candidate.decision_statement,
                structured_constraints_json=orjson.dumps(payload["structured_constraints"]).decode(),
                system_area_hint=candidate.system_area_hint,
                criticality_guess=candidate.criticality_guess,
                confidence_score=candidate.confidence_score,
                confidence_reason=candidate.confidence_reason,
                source_block_ids_json=orjson.dumps(candidate.source_block_ids).decode(),
                source_image_ids_json=orjson.dumps(candidate.source_image_ids).decode(),
                source_excerpt=candidate.source_excerpt,
                from_source_type=candidate.from_source_type,
                requires_human_review=True,
                status="pending_review",
                raw_response_json=orjson.dumps(raw).decode(),
            )
            session.add(row)
            session.flush()
            accepted_payloads.append({"id": row.id, **payload})
        output_packets.append({
            "extraction_packet_id": packet_row.id,
            "section_key": packet_row.section_key,
            "candidates": accepted_payloads,
            "raw_response": raw,
        })
    session.flush()
    storage.write_json(
        storage.document_dir("decision_outputs", document_id) / "decision_candidates.json",
        {"document_id": document_id, "packets": output_packets},
    )
    return output_packets


def validate_candidates(
    document_id: str,
    storage: StorageService,
    session: Session,
    client: GeminiClient,
    settings: Settings | None = None,
    on_candidate_error=None,
) -> list[dict]:
    settings = settings or get_settings()
    candidates = list(session.scalars(
        select(DecisionCandidate).where(DecisionCandidate.document_id == document_id).order_by(DecisionCandidate.id)
    ))
    packets = {row.id: orjson.loads(row.packet_json) for row in session.scalars(
        select(ExtractionPacket).where(ExtractionPacket.document_id == document_id)
    )}
    all_statements = [{"id": row.id, "statement": row.decision_statement} for row in candidates]
    report: list[dict] = []

    for index, candidate in enumerate(candidates):
        duplicate_id: int | None = None
        duplicate_score = 0
        for earlier in candidates[:index]:
            score = ratio(
                default_process(candidate.decision_statement),
                default_process(earlier.decision_statement),
            )
            if score >= 90 and score > duplicate_score:
                duplicate_id = earlier.id
                duplicate_score = score

        candidate_payload = {
            "id": candidate.id,
            "decision_statement": candidate.decision_statement,
            "decision_type": candidate.decision_type,
            "confidence_score": candidate.confidence_score,
            "confidence_reason": candidate.confidence_reason,
            "source_block_ids": orjson.loads(candidate.source_block_ids_json),
            "source_image_ids": orjson.loads(candidate.source_image_ids_json),
            "source_excerpt": candidate.source_excerpt,
        }
        try:
            validation, raw = client.generate_structured(
                model=settings.gemini_text_model,
                contents=VALIDATION_PROMPT.format(
                    candidate=orjson.dumps(candidate_payload).decode(),
                    packet=orjson.dumps(packets[candidate.extraction_packet_id]).decode(),
                    others=orjson.dumps([item for item in all_statements if item["id"] != candidate.id]).decode(),
                ),
                schema=CandidateValidationResult,
            )
        except Exception as exc:
            if isinstance(exc, GeminiModelUnavailableError):
                raise
            if on_candidate_error:
                on_candidate_error(candidate.id, exc)
            validation = CandidateValidationResult(
                validation_status="warning",
                validation_notes=f"Gemini validation failed; local checks only: {exc}",
                is_supported=True,
            )
            raw = {"error": str(exc)}

        notes = validation.validation_notes
        status = validation.validation_status
        if duplicate_id is not None:
            status = "warning" if status == "passed" else status
            notes += f" Possible local duplicate of candidate {duplicate_id} ({duplicate_score}% similarity)."
        candidate.validation_status = status
        candidate.validation_notes = notes
        candidate.possible_duplicate_candidate_id = duplicate_id
        candidate.possible_conflict_notes = validation.possible_conflict_notes
        report.append({
            "candidate_id": candidate.id,
            **validation.model_dump(mode="json"),
            "final_validation_status": status,
            "final_validation_notes": notes,
            "possible_duplicate_candidate_id": duplicate_id,
            "duplicate_similarity": duplicate_score or None,
            "raw_response": raw,
        })
    session.flush()
    storage.write_json(
        storage.document_dir("decision_outputs", document_id) / "validation_report.json",
        {"document_id": document_id, "validations": report},
    )
    return report
