from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


Importance = Literal["critical", "relevant", "not_relevant", "decorative", "duplicate", "unreadable", "unclear"]
ImageType = Literal[
    "workflow_diagram", "architecture_diagram", "table_screenshot", "state_machine",
    "permission_matrix", "sequence_diagram", "er_diagram", "ui_wireframe", "logo",
    "decorative", "screenshot", "unknown",
]


class ExtractedLogic(BaseModel):
    type: str
    rule: str
    actors: list[str] = Field(default_factory=list)
    conditions: list[str] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    source_visual_element: str | None = None


class ImageAnalysisPayload(BaseModel):
    image_id: str
    importance: Importance
    image_type: ImageType
    is_decision_relevant: bool
    comment: str = Field(min_length=1)
    summary: str | None = None
    extracted_logic: list[ExtractedLogic] = Field(default_factory=list)
    unclear_parts: list[str] = Field(default_factory=list)
    confidence_score: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def relevant_images_need_explanation(self):
        if self.is_decision_relevant and not (self.summary or self.extracted_logic):
            raise ValueError("Decision-relevant images require a summary or extracted logic")
        return self

