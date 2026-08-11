from __future__ import annotations

from hashlib import sha256
from pathlib import Path, PurePosixPath
import mimetypes
import zipfile

import orjson
from docx import Document as WordDocument
from docx.oxml.ns import qn
from sqlalchemy import delete
from sqlalchemy.orm import Session

from src.models import DocumentImage
from src.services.storage_service import StorageService


CONTENT_TYPE_EXTENSIONS = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
    "image/tiff": ".tiff", "image/bmp": ".bmp", "image/svg+xml": ".svg",
    "image/x-emf": ".emf", "image/x-wmf": ".wmf",
}


def _paragraph_records(document) -> list[dict]:
    headings: list[str] = []
    records: list[dict] = []
    paragraph_position = 0
    for child in document.element.body.iterchildren():
        if child.tag != qn("w:p"):
            continue
        paragraph_position += 1
        from docx.text.paragraph import Paragraph
        paragraph = Paragraph(child, document)
        text = paragraph.text.strip()
        style_name = paragraph.style.name if paragraph.style else ""
        if style_name.lower().startswith("heading ") and text:
            try:
                level = int(style_name.rsplit(" ", 1)[1])
                headings = headings[: level - 1] + [text]
            except ValueError:
                pass
        records.append({
            "paragraph": paragraph,
            "position": paragraph_position,
            "text": text,
            "heading_path": headings.copy(),
            "is_caption": "caption" in style_name.lower(),
        })
    return records


def _nearby_text(records: list[dict], index: int, direction: int) -> str | None:
    cursor = index + direction
    while 0 <= cursor < len(records):
        text = records[cursor]["text"]
        if text:
            return text
        cursor += direction
    return None


def _extension(part_name: str, content_type: str | None) -> str:
    suffix = PurePosixPath(part_name).suffix.lower()
    if suffix:
        return suffix
    return CONTENT_TYPE_EXTENSIONS.get(content_type or "", mimetypes.guess_extension(content_type or "") or ".bin")


def extract_all_images(
    document_id: str,
    file_path: Path,
    storage: StorageService,
    session: Session,
) -> tuple[list[dict], list[str]]:
    document = WordDocument(str(file_path))
    output_dir = storage.document_dir("images", document_id)
    records = _paragraph_records(document)
    occurrences: list[dict] = []
    referenced_members: set[str] = set()
    warnings: list[str] = []

    for record_index, record in enumerate(records):
        blips = record["paragraph"]._p.xpath(".//a:blip")
        for blip in blips:
            relationship_id = blip.get(qn("r:embed")) or blip.get(qn("r:link"))
            if not relationship_id or relationship_id not in document.part.rels:
                warnings.append(f"Unresolvable image relationship at paragraph {record['position']}")
                continue
            relationship = document.part.rels[relationship_id]
            if relationship.is_external or not hasattr(relationship.target_part, "blob"):
                warnings.append(f"External/unsupported image relationship {relationship_id}")
                continue
            part = relationship.target_part
            member_name = str(part.partname).lstrip("/")
            referenced_members.add(member_name)
            occurrences.append({
                "blob": part.blob,
                "member_name": member_name,
                "content_type": getattr(part, "content_type", None),
                "heading_path": record["heading_path"],
                "caption": (
                    records[record_index + 1]["text"]
                    if record_index + 1 < len(records) and records[record_index + 1]["is_caption"]
                    else None
                ),
                "nearby_text_before": _nearby_text(records, record_index, -1),
                "nearby_text_after": _nearby_text(records, record_index, 1),
                "position_index": record["position"],
                "mapping_quality": "exact",
            })

    with zipfile.ZipFile(file_path) as archive:
        for member_name in archive.namelist():
            if not member_name.startswith("word/media/") or member_name.endswith("/"):
                continue
            if member_name in referenced_members:
                continue
            occurrences.append({
                "blob": archive.read(member_name),
                "member_name": member_name,
                "content_type": mimetypes.guess_type(member_name)[0],
                "heading_path": [],
                "caption": None,
                "nearby_text_before": None,
                "nearby_text_after": None,
                "position_index": None,
                "mapping_quality": "unknown",
            })
            warnings.append(f"Media asset {member_name} could not be mapped to a document paragraph")

    session.execute(delete(DocumentImage).where(DocumentImage.document_id == document_id))
    images: list[dict] = []
    for index, occurrence in enumerate(occurrences, start=1):
        image_id = f"img_{index:03d}"
        extension = _extension(occurrence["member_name"], occurrence["content_type"])
        target = output_dir / f"{image_id}{extension}"
        target.write_bytes(occurrence["blob"])
        digest = sha256(occurrence["blob"]).hexdigest()
        payload = {
            "image_id": image_id,
            "image_path": str(target),
            "image_hash": digest,
            "image_format": extension.lstrip(".") or "unknown",
            "heading_path": occurrence["heading_path"],
            "caption": occurrence["caption"],
            "nearby_text_before": occurrence["nearby_text_before"],
            "nearby_text_after": occurrence["nearby_text_after"],
            "position_index": occurrence["position_index"],
            "mapping_quality": occurrence["mapping_quality"],
        }
        images.append(payload)
        session.add(DocumentImage(
            document_id=document_id,
            image_id=image_id,
            image_path=str(target),
            image_hash=digest,
            image_format=payload["image_format"],
            heading_path=orjson.dumps(payload["heading_path"]).decode(),
            caption=payload["caption"],
            nearby_text_before=payload["nearby_text_before"],
            nearby_text_after=payload["nearby_text_after"],
            position_index=payload["position_index"],
            mapping_quality=payload["mapping_quality"],
        ))
    session.flush()
    return images, warnings

