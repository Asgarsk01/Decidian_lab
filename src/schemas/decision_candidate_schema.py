from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator


DecisionType = Literal[
    "business_rule", "technical_constraint", "workflow_rule", "validation_rule",
    "permission_rule", "state_transition", "integration_rule", "security_rule",
    "data_rule", "audit_rule", "error_handling_rule", "other",
]


class StructuredConstraints(BaseModel):
    actor: str | None = None
    action: str | None = None
    condition: str | None = None
    required_behavior: str | None = None
    forbidden_behavior: str | None = None
    exception: str | None = None


class DecisionCandidatePayload(BaseModel):
    decision_type: DecisionType
    decision_statement: str = Field(min_length=5)
    structured_constraints: StructuredConstraints = Field(default_factory=StructuredConstraints)
    system_area_hint: str | None = None
    criticality_guess: Literal["low", "medium", "high", "critical"]
    confidence_score: float = Field(ge=0, le=1)
    confidence_reason: str = Field(min_length=1)
    source_block_ids: list[str] = Field(default_factory=list)
    source_image_ids: list[str] = Field(default_factory=list)
    source_excerpt: str = Field(min_length=1)
    from_source_type: Literal["text", "table", "image", "mixed"]
    requires_human_review: bool = True

    @field_validator("requires_human_review")
    @classmethod
    def human_review_is_mandatory(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("AI candidates must require human review")
        return value

    @field_validator("source_image_ids")
    @classmethod
    def evidence_is_checked_later(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def source_evidence_is_required(self):
        if not self.source_block_ids and not self.source_image_ids:
            raise ValueError("Every decision candidate requires a source block or source image")
        return self


class DecisionCandidateBatch(BaseModel):
    candidates: list[DecisionCandidatePayload] = Field(default_factory=list)


class CandidateValidationResult(BaseModel):
    validation_status: Literal["passed", "warning", "failed"]
    validation_notes: str
    is_supported: bool
    is_too_vague: bool = False
    appears_rejected_option: bool = False
    possible_conflict_notes: str | None = None
