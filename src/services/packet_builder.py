from __future__ import annotations

from collections import OrderedDict

import orjson
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.models import Document, DocumentBlock, DocumentImage, ExtractionPacket, ImageAnalysis
from src.schemas.extraction_packet_schema import ExtractionPacketPayload
from src.services.storage_service import StorageService


FULL_ANALYSIS_IMPORTANCE = {"critical", "relevant", "unclear"}


def _section_key(heading_path: list[str]) -> str:
    return " > ".join(heading_path) if heading_path else "Document root"


def build_packets(document_id: str, storage: StorageService, session: Session) -> list[dict]:
    document = session.get(Document, document_id)
    if document is None:
        raise ValueError(f"Unknown document: {document_id}")
    blocks = list(session.scalars(
        select(DocumentBlock).where(DocumentBlock.document_id == document_id).order_by(DocumentBlock.position_index, DocumentBlock.id)
    ))
    images = list(session.scalars(
        select(DocumentImage).where(DocumentImage.document_id == document_id).order_by(DocumentImage.id)
    ))
    analyses = list(session.scalars(
        select(ImageAnalysis).where(ImageAnalysis.document_id == document_id).order_by(ImageAnalysis.id)
    ))
    analysis_by_image = {item.image_id: item for item in analyses}
    sections: OrderedDict[tuple[str, ...], dict] = OrderedDict()

    for block in blocks:
        heading_path = orjson.loads(block.heading_path)
        key = tuple(heading_path)
        section = sections.setdefault(key, {"heading_path": heading_path, "source_blocks": [], "image_analyses": []})
        section["source_blocks"].append({
            "block_id": block.block_id,
            "type": block.block_type,
            "text": block.text,
            "markdown": block.markdown,
        })

    for image in images:
        heading_path = orjson.loads(image.heading_path)
        key = tuple(heading_path)
        section = sections.setdefault(key, {"heading_path": heading_path, "source_blocks": [], "image_analyses": []})
        analysis = analysis_by_image.get(image.image_id)
        if analysis is None:
            section["image_analyses"].append({
                "image_id": image.image_id,
                "importance": "unreadable",
                "comment": "No Gemini image analysis row exists; this image remains explicitly accounted for.",
            })
        elif analysis.importance in FULL_ANALYSIS_IMPORTANCE:
            section["image_analyses"].append({
                "image_id": analysis.image_id,
                "importance": analysis.importance,
                "image_type": analysis.image_type,
                "comment": analysis.comment,
                "summary": analysis.summary,
                "extracted_logic": orjson.loads(analysis.extracted_logic_json),
                "unclear_parts": orjson.loads(analysis.unclear_parts_json),
                "confidence_score": analysis.confidence_score,
            })
        else:
            section["image_analyses"].append({
                "image_id": analysis.image_id,
                "importance": analysis.importance,
                "comment": analysis.comment,
            })

    session.execute(delete(ExtractionPacket).where(ExtractionPacket.document_id == document_id))
    packets: list[dict] = []
    for section in sections.values():
        packet = ExtractionPacketPayload(
            document_id=document_id,
            document_type=document.doc_type,
            filename=document.filename,
            section_key=_section_key(section["heading_path"]),
            heading_path=section["heading_path"],
            source_blocks=section["source_blocks"],
            image_analyses=section["image_analyses"],
        ).model_dump(mode="json", exclude_none=True)
        packets.append(packet)
        session.add(ExtractionPacket(
            document_id=document_id,
            section_key=packet["section_key"],
            heading_path=orjson.dumps(packet["heading_path"]).decode(),
            packet_json=orjson.dumps(packet).decode(),
        ))
    session.flush()
    storage.write_jsonl(storage.document_dir("extraction_packets", document_id) / "packets.jsonl", packets)
    return packets

