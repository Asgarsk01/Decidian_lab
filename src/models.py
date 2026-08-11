from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    filename: Mapped[str] = mapped_column(Text)
    file_path: Mapped[str] = mapped_column(Text)
    doc_type: Mapped[str] = mapped_column(Text, default="other")
    status: Mapped[str] = mapped_column(Text, default="uploaded")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DocumentBlock(Base):
    __tablename__ = "document_blocks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    block_id: Mapped[str] = mapped_column(Text, index=True)
    block_type: Mapped[str] = mapped_column(Text)
    heading_path: Mapped[str] = mapped_column(Text, default="[]")
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    position_index: Mapped[int] = mapped_column(Integer)
    source_json: Mapped[str] = mapped_column(Text, default="{}")


class DocumentImage(Base):
    __tablename__ = "document_images"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    image_id: Mapped[str] = mapped_column(Text, index=True)
    image_path: Mapped[str] = mapped_column(Text)
    image_hash: Mapped[str] = mapped_column(Text, index=True)
    image_format: Mapped[str] = mapped_column(Text)
    heading_path: Mapped[str] = mapped_column(Text, default="[]")
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    nearby_text_before: Mapped[str | None] = mapped_column(Text, nullable=True)
    nearby_text_after: Mapped[str | None] = mapped_column(Text, nullable=True)
    position_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    mapping_quality: Mapped[str] = mapped_column(Text, default="unknown")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ImageAnalysis(Base):
    __tablename__ = "image_analyses"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    image_id: Mapped[str] = mapped_column(Text, index=True)
    image_hash: Mapped[str] = mapped_column(Text, index=True)
    vision_provider: Mapped[str] = mapped_column(Text, default="google_gemini")
    vision_model: Mapped[str] = mapped_column(Text)
    importance: Mapped[str] = mapped_column(Text)
    image_type: Mapped[str] = mapped_column(Text)
    is_decision_relevant: Mapped[bool] = mapped_column(Boolean)
    comment: Mapped[str] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_logic_json: Mapped[str] = mapped_column(Text, default="[]")
    unclear_parts_json: Mapped[str] = mapped_column(Text, default="[]")
    confidence_score: Mapped[float] = mapped_column(Float)
    raw_response_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ExtractionPacket(Base):
    __tablename__ = "extraction_packets"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    section_key: Mapped[str] = mapped_column(Text)
    heading_path: Mapped[str] = mapped_column(Text, default="[]")
    packet_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DecisionCandidate(Base):
    __tablename__ = "decision_candidates"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    extraction_packet_id: Mapped[int] = mapped_column(ForeignKey("extraction_packets.id"))
    decision_type: Mapped[str] = mapped_column(Text)
    decision_statement: Mapped[str] = mapped_column(Text)
    structured_constraints_json: Mapped[str] = mapped_column(Text, default="{}")
    system_area_hint: Mapped[str | None] = mapped_column(Text, nullable=True)
    criticality_guess: Mapped[str] = mapped_column(Text)
    confidence_score: Mapped[float] = mapped_column(Float)
    confidence_reason: Mapped[str] = mapped_column(Text)
    source_block_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    source_image_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    source_excerpt: Mapped[str] = mapped_column(Text)
    from_source_type: Mapped[str] = mapped_column(Text)
    requires_human_review: Mapped[bool] = mapped_column(Boolean, default=True)
    status: Mapped[str] = mapped_column(Text, default="pending_review")
    raw_response_json: Mapped[str] = mapped_column(Text)
    validation_status: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    possible_duplicate_candidate_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    possible_conflict_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Decision(Base):
    __tablename__ = "decisions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    current_version_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(Text, default="approved")
    owner: Mapped[str | None] = mapped_column(Text, nullable=True)
    criticality: Mapped[str] = mapped_column(Text)
    created_from_candidate_id: Mapped[int] = mapped_column(ForeignKey("decision_candidates.id"), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class DecisionVersion(Base):
    __tablename__ = "decision_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decision_id: Mapped[int] = mapped_column(ForeignKey("decisions.id"), index=True)
    version_number: Mapped[int] = mapped_column(Integer, default=1)
    rule_text: Mapped[str] = mapped_column(Text)
    structured_constraints_json: Mapped[str] = mapped_column(Text, default="{}")
    scope_json: Mapped[str] = mapped_column(Text, default="{}")
    edited_by: Mapped[str] = mapped_column(Text, default="local_reviewer")
    edit_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DecisionSource(Base):
    __tablename__ = "decision_sources"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decision_version_id: Mapped[int] = mapped_column(ForeignKey("decision_versions.id"), index=True)
    document_block_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    document_image_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    source_excerpt: Mapped[str] = mapped_column(Text)


class ProcessingLog(Base):
    __tablename__ = "processing_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    document_id: Mapped[str | None] = mapped_column(Text, index=True, nullable=True)
    level: Mapped[str] = mapped_column(Text, default="info")
    stage: Mapped[str] = mapped_column(Text)
    message: Mapped[str] = mapped_column(Text)
    details_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

