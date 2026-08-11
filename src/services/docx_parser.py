from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import re

from docx import Document as WordDocument
from docx.document import Document as _Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph
from sqlalchemy import delete
from sqlalchemy.orm import Session

from src.models import DocumentBlock
from src.services.storage_service import StorageService


def _iter_body_items(parent: _Document) -> Iterator[Paragraph | Table]:
    for child in parent.element.body.iterchildren():
        if child.tag == qn("w:p"):
            yield Paragraph(child, parent)
        elif child.tag == qn("w:tbl"):
            yield Table(child, parent)


def _escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", "<br>").strip()


def _table_to_markdown(table: Table) -> str:
    rows = [[_escape_cell(cell.text) for cell in row.cells] for row in table.rows]
    if not rows:
        return ""
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    header = rows[0]
    separator = ["---"] * width
    return "\n".join("| " + " | ".join(row) + " |" for row in [header, separator, *rows[1:]])


def _heading_level(paragraph: Paragraph) -> int | None:
    style_name = paragraph.style.name if paragraph.style else ""
    match = re.match(r"Heading\s+(\d+)", style_name, re.IGNORECASE)
    if match:
        return max(1, min(int(match.group(1)), 9))
    return None


def _list_kind(paragraph: Paragraph, document: _Document) -> str | None:
    p_pr = paragraph._p.pPr
    num_pr = p_pr.numPr if p_pr is not None else None
    style_name = (paragraph.style.name if paragraph.style else "").lower()
    if num_pr is None:
        if "bullet" in style_name:
            return "bullet_list"
        if "number" in style_name:
            return "numbered_list"
        return None
    try:
        num_id = num_pr.numId.val
        ilvl = num_pr.ilvl.val if num_pr.ilvl is not None else 0
        numbering = document.part.numbering_part.element
        num_nodes = numbering.xpath(f"./w:num[@w:numId='{num_id}']")
        abstract_id = num_nodes[0].xpath("./w:abstractNumId/@w:val")[0]
        level_nodes = numbering.xpath(
            f"./w:abstractNum[@w:abstractNumId='{abstract_id}']/w:lvl[@w:ilvl='{ilvl}']/w:numFmt/@w:val"
        )
        return "bullet_list" if level_nodes and level_nodes[0] == "bullet" else "numbered_list"
    except (AttributeError, IndexError, KeyError, ValueError):
        return "numbered_list"


def _has_image_reference(paragraph: Paragraph) -> bool:
    return bool(paragraph._p.xpath(".//a:blip"))


def _normalize_blocks(document: _Document) -> list[dict]:
    blocks: list[dict] = []
    headings: list[str] = []
    for position, item in enumerate(_iter_body_items(document), start=1):
        block_id = f"b_{position:03d}"
        if isinstance(item, Table):
            markdown = _table_to_markdown(item)
            blocks.append({
                "block_id": block_id,
                "type": "table",
                "heading_path": headings.copy(),
                "text": item.cell(0, 0).text.strip() if item.rows and item.columns else "",
                "markdown": markdown,
                "position_index": position,
            })
            continue

        text = item.text.strip()
        level = _heading_level(item)
        if level is not None and text:
            headings = headings[: level - 1]
            headings.append(text)
            blocks.append({
                "block_id": block_id,
                "type": "heading",
                "level": level,
                "heading_path": headings.copy(),
                "text": text,
                "position_index": position,
            })
            continue

        style_name = (item.style.name if item.style else "").lower()
        if "caption" in style_name and text:
            block_type = "caption"
        else:
            list_kind = _list_kind(item, document)
            block_type = "list" if list_kind else "paragraph"
        if text:
            block = {
                "block_id": block_id,
                "type": block_type,
                "heading_path": headings.copy(),
                "text": text,
                "position_index": position,
            }
            if block_type == "list":
                block["list_type"] = list_kind
                block["markdown"] = ("- " if list_kind == "bullet_list" else "1. ") + text
            blocks.append(block)
        if _has_image_reference(item):
            blocks.append({
                "block_id": f"{block_id}_img",
                "type": "image_reference",
                "heading_path": headings.copy(),
                "text": text or None,
                "position_index": position,
            })
    return blocks


def parse_docx(
    document_id: str,
    filename: str,
    doc_type: str,
    file_path: Path,
    storage: StorageService,
    session: Session,
) -> dict:
    """Parse with Docling, then normalize Word structure for stable internal blocks."""
    try:
        from docling.datamodel.base_models import InputFormat
        from docling.document_converter import DocumentConverter
    except ImportError as exc:
        raise RuntimeError("Docling is not installed. Run: pip install -r requirements.txt") from exc

    converter = DocumentConverter(allowed_formats=[InputFormat.DOCX])
    conversion = converter.convert(file_path)
    docling_document = conversion.document
    preview_markdown = docling_document.export_to_markdown()
    if hasattr(docling_document, "export_to_dict"):
        docling_source = docling_document.export_to_dict()
    elif hasattr(docling_document, "model_dump"):
        docling_source = docling_document.model_dump(mode="json")
    else:
        docling_source = {"note": "Docling source serialization unavailable"}

    word_document = WordDocument(str(file_path))
    blocks = _normalize_blocks(word_document)
    payload = {
        "document_id": document_id,
        "filename": filename,
        "doc_type": doc_type,
        "blocks": blocks,
        "parser": {"primary": "docling", "normalizer": "python-docx", "docling_source": docling_source},
    }
    output_dir = storage.document_dir("parsed", document_id)
    storage.write_json(output_dir / "parsed_document.json", payload)
    (output_dir / "preview.md").write_text(preview_markdown, encoding="utf-8")

    session.execute(delete(DocumentBlock).where(DocumentBlock.document_id == document_id))
    for block in blocks:
        session.add(DocumentBlock(
            document_id=document_id,
            block_id=block["block_id"],
            block_type=block["type"],
            heading_path=__import__("orjson").dumps(block.get("heading_path", [])).decode(),
            text=block.get("text"),
            markdown=block.get("markdown"),
            position_index=block["position_index"],
            source_json=__import__("orjson").dumps(block).decode(),
        ))
    session.flush()
    return payload

