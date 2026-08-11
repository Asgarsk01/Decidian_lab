from __future__ import annotations

import mimetypes
from pathlib import Path

import orjson
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from src.config import Settings, get_settings
from src.models import Document, DocumentImage, ImageAnalysis
from src.schemas.image_analysis_schema import ImageAnalysisPayload
from src.services.gemini_client import GeminiClient, GeminiModelUnavailableError
from src.services.storage_service import StorageService


IMAGE_ANALYSIS_PROMPT = """You are analyzing an image extracted from a DOCX SRS/RFC/proposal document.

Your job is to decide whether this image contains decision-relevant information.
Decision-relevant means the image contains or supports a business rule, workflow, state transition,
approval flow, role/permission matrix, architecture logic, integration or validation rule, exception
path, compliance/security constraint, data lifecycle rule, business-relevant UI behavior, or API/data flow.

Classify every image. Return strict JSON matching the supplied schema. If it is not useful, explain why.
Do not invent rules not visible in the image or strongly supported by nearby text. If unreadable, say so.
If decorative/logo only, mark it appropriately. Extract workflow/architecture logic as structured rules.

Image/document context:
{context}
"""


def _mime_type(path: Path) -> str:
    mime_type = mimetypes.guess_type(path.name)[0]
    if mime_type == "image/jpg":
        return "image/jpeg"
    return mime_type or "application/octet-stream"


def _analysis_row(document_id: str, image: DocumentImage, model: str, payload: ImageAnalysisPayload, raw: dict):
    return ImageAnalysis(
        document_id=document_id,
        image_id=image.image_id,
        image_hash=image.image_hash,
        vision_provider="google_gemini",
        vision_model=model,
        importance=payload.importance,
        image_type=payload.image_type,
        is_decision_relevant=payload.is_decision_relevant,
        comment=payload.comment,
        summary=payload.summary,
        extracted_logic_json=orjson.dumps([item.model_dump(mode="json") for item in payload.extracted_logic]).decode(),
        unclear_parts_json=orjson.dumps(payload.unclear_parts).decode(),
        confidence_score=payload.confidence_score,
        raw_response_json=orjson.dumps(raw).decode(),
    )


def analyze_images(
    document_id: str,
    storage: StorageService,
    session: Session,
    client: GeminiClient,
    settings: Settings | None = None,
    on_item_error=None,
) -> list[dict]:
    settings = settings or get_settings()
    document = session.get(Document, document_id)
    if document is None:
        raise ValueError(f"Unknown document: {document_id}")
    images = list(session.scalars(
        select(DocumentImage).where(DocumentImage.document_id == document_id).order_by(DocumentImage.id)
    ))
    session.execute(delete(ImageAnalysis).where(ImageAnalysis.document_id == document_id))
    analyses: list[dict] = []
    first_by_hash: dict[str, ImageAnalysisPayload] = {}

    for image in images:
        raw: dict
        if image.image_hash in first_by_hash:
            original = first_by_hash[image.image_hash]
            payload = ImageAnalysisPayload(
                image_id=image.image_id,
                importance="duplicate",
                image_type=original.image_type,
                is_decision_relevant=False,
                comment=f"Duplicate binary image; analysis reused from the first occurrence with hash {image.image_hash[:12]}.",
                summary=original.summary,
                extracted_logic=[],
                unclear_parts=[],
                confidence_score=original.confidence_score,
            )
            raw = {"reused_duplicate_hash": image.image_hash, "original_analysis": original.model_dump(mode="json")}
        else:
            context = {
                "image_id": image.image_id,
                "document_title": document.filename,
                "document_type": document.doc_type,
                "heading_path": orjson.loads(image.heading_path),
                "caption": image.caption,
                "nearby_text_before": image.nearby_text_before,
                "nearby_text_after": image.nearby_text_after,
            }
            try:
                image_path = Path(image.image_path)
                prompt = IMAGE_ANALYSIS_PROMPT.format(context=orjson.dumps(context).decode())
                payload, raw = client.generate_structured(
                    model=settings.gemini_vision_model,
                    contents=[prompt, client.image_part(image_path.read_bytes(), _mime_type(image_path))],
                    schema=ImageAnalysisPayload,
                )
                payload.image_id = image.image_id
                first_by_hash[image.image_hash] = payload
            except Exception as exc:
                if isinstance(exc, GeminiModelUnavailableError):
                    raise
                if on_item_error:
                    on_item_error(image.image_id, exc)
                payload = ImageAnalysisPayload(
                    image_id=image.image_id,
                    importance="unreadable",
                    image_type="unknown",
                    is_decision_relevant=False,
                    comment=f"Image analysis failed; the image was not silently skipped: {exc}",
                    summary=None,
                    extracted_logic=[],
                    unclear_parts=[str(exc)],
                    confidence_score=0.0,
                )
                raw = {"error": str(exc)}
        session.add(_analysis_row(document_id, image, settings.gemini_vision_model, payload, raw))
        analyses.append(payload.model_dump(mode="json") | {"raw_response": raw, "image_hash": image.image_hash})
    session.flush()
    storage.write_json(
        storage.document_dir("image_analysis", document_id) / "image_analysis.json",
        {"document_id": document_id, "analyses": analyses},
    )
    return analyses
