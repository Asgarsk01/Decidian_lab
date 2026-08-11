from __future__ import annotations

import orjson
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.models import Decision, DecisionCandidate, DecisionSource, DecisionVersion
from src.schemas.decision_candidate_schema import StructuredConstraints


def approve_candidate(session: Session, candidate_id: int, reviewer: str = "local_reviewer") -> Decision:
    candidate = session.get(DecisionCandidate, candidate_id)
    if candidate is None:
        raise ValueError("Candidate not found")
    if candidate.status == "rejected_candidate":
        raise ValueError("A rejected candidate must be edited before it can be approved")
    existing = session.scalar(select(Decision).where(Decision.created_from_candidate_id == candidate_id))
    if existing is not None:
        candidate.status = "approved_candidate"
        return existing

    # This is the only candidate-to-decision boundary and is invoked by an explicit human UI action.
    decision = Decision(
        document_id=candidate.document_id,
        current_version_id=None,
        status="approved",
        owner=reviewer,
        criticality=candidate.criticality_guess,
        created_from_candidate_id=candidate.id,
    )
    session.add(decision)
    session.flush()
    version = DecisionVersion(
        decision_id=decision.id,
        version_number=1,
        rule_text=candidate.decision_statement,
        structured_constraints_json=candidate.structured_constraints_json,
        scope_json=orjson.dumps({"system_area_hint": candidate.system_area_hint}).decode(),
        edited_by=reviewer,
        edit_note="Approved from human-reviewed AI candidate",
    )
    session.add(version)
    session.flush()
    session.add(DecisionSource(
        decision_version_id=version.id,
        document_block_ids_json=candidate.source_block_ids_json,
        document_image_ids_json=candidate.source_image_ids_json,
        source_excerpt=candidate.source_excerpt,
    ))
    decision.current_version_id = version.id
    candidate.status = "approved_candidate"
    session.flush()
    return decision


def edit_candidate(
    session: Session,
    candidate_id: int,
    *,
    decision_statement: str,
    decision_type: str,
    criticality: str,
    system_area_hint: str | None,
    structured_constraints: dict,
) -> DecisionCandidate:
    candidate = session.get(DecisionCandidate, candidate_id)
    if candidate is None:
        raise ValueError("Candidate not found")
    if candidate.status == "approved_candidate":
        raise ValueError("Approved decisions are versioned separately and cannot be edited as candidates")
    candidate.decision_statement = decision_statement.strip()
    candidate.decision_type = decision_type
    candidate.criticality_guess = criticality
    candidate.system_area_hint = system_area_hint.strip() if system_area_hint else None
    constraints = StructuredConstraints.model_validate(structured_constraints)
    candidate.structured_constraints_json = orjson.dumps(constraints.model_dump(mode="json")).decode()
    candidate.status = "edited_candidate"
    session.flush()
    return candidate


def reject_candidate(session: Session, candidate_id: int, reason: str | None = None) -> DecisionCandidate:
    candidate = session.get(DecisionCandidate, candidate_id)
    if candidate is None:
        raise ValueError("Candidate not found")
    if candidate.status == "approved_candidate":
        raise ValueError("An approved decision cannot be rejected through candidate review")
    candidate.status = "rejected_candidate"
    candidate.rejection_reason = reason.strip() if reason else None
    session.flush()
    return candidate

