from __future__ import annotations

from pydantic import BaseModel, Field


class SourceBlock(BaseModel):
    block_id: str
    type: str
    text: str | None = None
    markdown: str | None = None


class PacketImageAnalysis(BaseModel):
    image_id: str
    importance: str
    image_type: str | None = None
    comment: str | None = None
    summary: str | None = None
    extracted_logic: list[dict] = Field(default_factory=list)
    unclear_parts: list[str] = Field(default_factory=list)
    confidence_score: float | None = None


class ExtractionPacketPayload(BaseModel):
    document_id: str
    document_type: str
    filename: str
    section_key: str
    heading_path: list[str] = Field(default_factory=list)
    source_blocks: list[SourceBlock] = Field(default_factory=list)
    image_analyses: list[PacketImageAnalysis] = Field(default_factory=list)

