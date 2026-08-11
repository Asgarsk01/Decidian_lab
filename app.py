from __future__ import annotations

from datetime import datetime
from pathlib import Path

import orjson
import streamlit as st
from sqlalchemy import select

from src.config import get_settings
from src.db import init_db, session_scope
from src.models import (
    Decision,
    DecisionCandidate,
    DecisionSource,
    DecisionVersion,
    Document,
    DocumentBlock,
    DocumentImage,
    ExtractionPacket,
    ImageAnalysis,
    ProcessingLog,
)
from src.services.pipeline_service import register_upload, run_full_pipeline
from src.services.review_service import approve_candidate, edit_candidate, reject_candidate
from src.services.storage_service import StorageService


DECISION_TYPES = [
    "business_rule", "technical_constraint", "workflow_rule", "validation_rule",
    "permission_rule", "state_transition", "integration_rule", "security_rule",
    "data_rule", "audit_rule", "error_handling_rule", "other",
]
CRITICALITIES = ["low", "medium", "high", "critical"]


st.set_page_config(page_title="Decidian Local", page_icon="⚖️", layout="wide")
init_db()
settings = get_settings()
storage = StorageService(settings)


def _documents() -> list[Document]:
    with session_scope() as session:
        return list(session.scalars(select(Document).order_by(Document.created_at.desc())))


def _active_document() -> Document | None:
    document_id = st.session_state.get("document_id")
    if not document_id:
        return None
    with session_scope() as session:
        return session.get(Document, document_id)


def _row_dict(row) -> dict:
    result = {}
    for column in row.__table__.columns:
        value = getattr(row, column.name)
        result[column.name] = value.isoformat() if isinstance(value, datetime) else value
    return result


def _json_text(value: str, fallback):
    try:
        return orjson.loads(value)
    except (orjson.JSONDecodeError, TypeError):
        return fallback


st.title("Decidian Local")
st.caption("DOCX intelligence → decision candidates → mandatory human review")

documents = _documents()
if documents:
    labels = {item.id: f"{item.filename} · {item.doc_type} · {item.status} · {item.id[-8:]}" for item in documents}
    current = st.session_state.get("document_id")
    if current not in labels:
        current = documents[0].id
    selected_id = st.sidebar.selectbox(
        "Active document",
        options=list(labels),
        index=list(labels).index(current),
        format_func=labels.get,
    )
    st.session_state.document_id = selected_id
else:
    st.sidebar.info("Upload a DOCX to begin.")

st.sidebar.markdown("**Configured models**")
st.sidebar.code(f"Text: {settings.gemini_text_model}\nVision: {settings.gemini_vision_model}")
if not settings.gemini_api_key or settings.gemini_api_key == "your_key_here":
    st.sidebar.warning("GEMINI_API_KEY is not configured. Local parsing works, but AI stages require a key.")

tabs = st.tabs([
    "Upload DOCX",
    "Parsed Document Preview",
    "Extracted Images",
    "Gemini Image Analysis",
    "Extraction Packets",
    "Decision Candidates",
    "Raw JSON Viewer",
    "Logs / Errors",
])

with tabs[0]:
    st.subheader("Upload a requirements document")
    uploaded = st.file_uploader("DOCX only", type=["docx"], accept_multiple_files=False)
    doc_type = st.selectbox("Document type", ["other", "srs", "rfc", "proposal"])
    if uploaded is not None and Path(uploaded.name).suffix.lower() != ".docx":
        st.error("Unsupported file type. Decidian accepts DOCX files only.")
    if st.button("Save and run full pipeline", type="primary", disabled=uploaded is None):
        try:
            StorageService.validate_docx_filename(uploaded.name)
            document_id = register_upload(uploaded.name, doc_type, uploaded.getvalue())
            st.session_state.document_id = document_id
            with st.status("Running Decidian pipeline", expanded=True) as status:
                def stage_update(stage: str, message: str) -> None:
                    st.write(f"**{stage.replace('_', ' ').title()}** — {message}")

                result = run_full_pipeline(document_id, stage_update)
                status.update(label="Pipeline complete — candidates await review", state="complete")
            st.success(
                f"Created {result.get('candidate_count', 0)} candidates from "
                f"{result.get('packet_count', 0)} section packets. No candidate is approved automatically."
            )
            st.rerun()
        except ValueError as exc:
            st.error(str(exc))
        except Exception as exc:
            st.error(str(exc))
            st.info("The completed local stages and error details remain available in the other tabs.")

with tabs[1]:
    active = _active_document()
    if active is None:
        st.info("Upload a DOCX first.")
    else:
        parsed_dir = storage.document_dir("parsed", active.id)
        preview_path = parsed_dir / "preview.md"
        parsed_path = parsed_dir / "parsed_document.json"
        if not preview_path.exists():
            st.info("This document has not been parsed yet.")
        else:
            parsed = storage.read_json(parsed_path)
            blocks = parsed.get("blocks", [])
            headings = [item for item in blocks if item.get("type") == "heading"]
            col1, col2 = st.columns(2)
            col1.metric("Parsed blocks", len(blocks))
            col2.metric("Headings", len(headings))
            with st.expander("Heading outline", expanded=True):
                for heading in headings:
                    st.markdown(f"{' ' * 4 * (heading.get('level', 1) - 1)}- {heading.get('text', '')}")
            st.markdown(preview_path.read_text(encoding="utf-8"))
            with st.expander("Normalized block list"):
                st.dataframe(blocks, use_container_width=True, hide_index=True)

with tabs[2]:
    active = _active_document()
    if active is None:
        st.info("Upload a DOCX first.")
    else:
        with session_scope() as session:
            images = list(session.scalars(
                select(DocumentImage).where(DocumentImage.document_id == active.id).order_by(DocumentImage.id)
            ))
        if not images:
            st.info("No images were extracted, or image extraction has not run yet.")
        st.caption(f"Every occurrence is shown ({len(images)} total), including repeated assets.")
        for image_row in images:
            with st.expander(f"{image_row.image_id} · {image_row.mapping_quality} · {image_row.image_hash[:12]}", expanded=True):
                left, right = st.columns([1, 2])
                try:
                    left.image(image_row.image_path, caption=image_row.image_id, use_container_width=True)
                except Exception:
                    left.warning(f"Preview unavailable for .{image_row.image_format}; the original asset is preserved.")
                right.json({
                    "image_id": image_row.image_id,
                    "hash": image_row.image_hash,
                    "format": image_row.image_format,
                    "heading_path": _json_text(image_row.heading_path, []),
                    "caption": image_row.caption,
                    "nearby_text_before": image_row.nearby_text_before,
                    "nearby_text_after": image_row.nearby_text_after,
                    "position_index": image_row.position_index,
                    "mapping_quality": image_row.mapping_quality,
                })

with tabs[3]:
    active = _active_document()
    if active is None:
        st.info("Upload a DOCX first.")
    else:
        with session_scope() as session:
            analyses = list(session.scalars(
                select(ImageAnalysis).where(ImageAnalysis.document_id == active.id).order_by(ImageAnalysis.id)
            ))
        if not analyses:
            st.info("No Gemini image analyses are available yet.")
        for analysis in analyses:
            label = f"{analysis.image_id} · {analysis.importance} · {analysis.image_type} · {analysis.confidence_score:.0%}"
            with st.expander(label, expanded=analysis.importance in {"critical", "relevant", "unclear"}):
                st.write(analysis.comment)
                if analysis.summary:
                    st.markdown(f"**Summary:** {analysis.summary}")
                logic = _json_text(analysis.extracted_logic_json, [])
                if logic:
                    st.markdown("**Extracted visual logic**")
                    st.json(logic)
                unclear = _json_text(analysis.unclear_parts_json, [])
                if unclear:
                    st.markdown("**Unclear parts**")
                    st.json(unclear)
                with st.expander("Raw Gemini response"):
                    st.json(_json_text(analysis.raw_response_json, {"raw": analysis.raw_response_json}))

with tabs[4]:
    active = _active_document()
    if active is None:
        st.info("Upload a DOCX first.")
    else:
        with session_scope() as session:
            packets = list(session.scalars(
                select(ExtractionPacket).where(ExtractionPacket.document_id == active.id).order_by(ExtractionPacket.id)
            ))
        st.metric("Section packets", len(packets))
        for packet in packets:
            data = _json_text(packet.packet_json, {})
            with st.expander(f"{packet.id} · {packet.section_key}"):
                st.write(
                    f"{len(data.get('source_blocks', []))} source blocks · "
                    f"{len(data.get('image_analyses', []))} accounted images"
                )
                st.json(data)

with tabs[5]:
    active = _active_document()
    if active is None:
        st.info("Upload a DOCX first.")
    else:
        with session_scope() as session:
            candidates = list(session.scalars(
                select(DecisionCandidate).where(DecisionCandidate.document_id == active.id).order_by(DecisionCandidate.id)
            ))
            image_rows = list(session.scalars(
                select(DocumentImage).where(DocumentImage.document_id == active.id)
            ))
        images_by_id = {row.image_id: row for row in image_rows}
        pending = sum(row.status in {"pending_review", "edited_candidate"} for row in candidates)
        approved = sum(row.status == "approved_candidate" for row in candidates)
        rejected = sum(row.status == "rejected_candidate" for row in candidates)
        metric_cols = st.columns(4)
        metric_cols[0].metric("Total", len(candidates))
        metric_cols[1].metric("Awaiting review", pending)
        metric_cols[2].metric("Approved", approved)
        metric_cols[3].metric("Rejected", rejected)
        st.warning("AI output is candidate material only. Approve creates a versioned decision through a human action.")

        if not candidates:
            st.info("No decision candidates are available yet.")
        for candidate in candidates:
            status_icon = {"approved_candidate": "✅", "rejected_candidate": "❌", "edited_candidate": "✏️"}.get(candidate.status, "⏳")
            with st.container(border=True):
                st.subheader(f"{status_icon} Candidate {candidate.id}: {candidate.decision_statement}")
                cols = st.columns(4)
                cols[0].write(f"**Type:** {candidate.decision_type}")
                cols[1].write(f"**Criticality:** {candidate.criticality_guess}")
                cols[2].write(f"**Confidence:** {candidate.confidence_score:.0%}")
                cols[3].write(f"**Validation:** {candidate.validation_status or 'not run'}")
                st.write(candidate.confidence_reason)
                if candidate.validation_notes:
                    st.info(candidate.validation_notes)
                st.markdown(f"**Source excerpt:** {candidate.source_excerpt}")
                source_images = _json_text(candidate.source_image_ids_json, [])
                st.json({
                    "source_block_ids": _json_text(candidate.source_block_ids_json, []),
                    "source_image_ids": source_images,
                    "from_source_type": candidate.from_source_type,
                    "system_area_hint": candidate.system_area_hint,
                    "structured_constraints": _json_text(candidate.structured_constraints_json, {}),
                })
                if source_images:
                    image_columns = st.columns(min(len(source_images), 4))
                    for idx, image_id in enumerate(source_images):
                        image_row = images_by_id.get(image_id)
                        if image_row:
                            try:
                                image_columns[idx % len(image_columns)].image(
                                    image_row.image_path, caption=image_id, use_container_width=True
                                )
                            except Exception:
                                image_columns[idx % len(image_columns)].caption(f"{image_id}: preview unavailable")
                with st.expander("Raw candidate response"):
                    st.json(_json_text(candidate.raw_response_json, {"raw": candidate.raw_response_json}))

                if candidate.status != "approved_candidate":
                    with st.expander("Edit candidate"):
                        with st.form(f"edit_{candidate.id}"):
                            statement = st.text_area("Decision statement", candidate.decision_statement)
                            dtype = st.selectbox(
                                "Decision type", DECISION_TYPES,
                                index=DECISION_TYPES.index(candidate.decision_type), key=f"dtype_{candidate.id}",
                            )
                            criticality = st.selectbox(
                                "Criticality", CRITICALITIES,
                                index=CRITICALITIES.index(candidate.criticality_guess), key=f"crit_{candidate.id}",
                            )
                            area = st.text_input("System area hint", candidate.system_area_hint or "")
                            constraints_text = st.text_area(
                                "Structured constraints (JSON)",
                                orjson.dumps(_json_text(candidate.structured_constraints_json, {}), option=orjson.OPT_INDENT_2).decode(),
                            )
                            if st.form_submit_button("Save edited candidate"):
                                try:
                                    constraints = orjson.loads(constraints_text)
                                    with session_scope() as session:
                                        edit_candidate(
                                            session, candidate.id, decision_statement=statement,
                                            decision_type=dtype, criticality=criticality,
                                            system_area_hint=area, structured_constraints=constraints,
                                        )
                                    st.success("Edited candidate saved. It is still not an approved decision.")
                                    st.rerun()
                                except Exception as exc:
                                    st.error(str(exc))
                    action_cols = st.columns([1, 2, 1])
                    if action_cols[0].button("Approve", key=f"approve_{candidate.id}", type="primary"):
                        try:
                            with session_scope() as session:
                                approve_candidate(session, candidate.id)
                            st.success("Approved and stored as decision version 1.")
                            st.rerun()
                        except Exception as exc:
                            st.error(str(exc))
                    reason = action_cols[1].text_input("Optional rejection reason", key=f"reason_{candidate.id}")
                    if action_cols[2].button("Reject", key=f"reject_{candidate.id}"):
                        try:
                            with session_scope() as session:
                                reject_candidate(session, candidate.id, reason)
                            st.rerun()
                        except Exception as exc:
                            st.error(str(exc))

with tabs[6]:
    active = _active_document()
    if active is None:
        st.info("Upload a DOCX first.")
    else:
        file_options = {
            "Parsed document": storage.document_dir("parsed", active.id) / "parsed_document.json",
            "Image analysis": storage.document_dir("image_analysis", active.id) / "image_analysis.json",
            "Extraction packets (JSONL)": storage.document_dir("extraction_packets", active.id) / "packets.jsonl",
            "Decision candidates": storage.document_dir("decision_outputs", active.id) / "decision_candidates.json",
            "Validation report": storage.document_dir("decision_outputs", active.id) / "validation_report.json",
        }
        source_kind = st.radio("Inspect", ["Output file", "Database rows"], horizontal=True)
        if source_kind == "Output file":
            name = st.selectbox("Output", list(file_options))
            path = file_options[name]
            st.code(str(path))
            if not path.exists():
                st.info("This output has not been created yet.")
            elif path.suffix == ".jsonl":
                rows = [orjson.loads(line) for line in path.read_bytes().splitlines() if line]
                st.json(rows)
            else:
                st.json(storage.read_json(path))
        else:
            table_models = {
                "documents": Document,
                "document_blocks": DocumentBlock,
                "document_images": DocumentImage,
                "image_analyses": ImageAnalysis,
                "extraction_packets": ExtractionPacket,
                "decision_candidates": DecisionCandidate,
                "decisions": Decision,
                "decision_versions": DecisionVersion,
                "decision_sources": DecisionSource,
                "processing_logs": ProcessingLog,
            }
            table_name = st.selectbox("Table", list(table_models))
            model = table_models[table_name]
            with session_scope() as session:
                query = select(model)
                if hasattr(model, "document_id"):
                    query = query.where(model.document_id == active.id)
                rows = list(session.scalars(query))
            st.json([_row_dict(row) for row in rows])

with tabs[7]:
    active = _active_document()
    if active is None:
        st.info("Upload a DOCX first.")
    else:
        with session_scope() as session:
            logs = list(session.scalars(
                select(ProcessingLog)
                .where(ProcessingLog.document_id == active.id)
                .order_by(ProcessingLog.created_at.desc(), ProcessingLog.id.desc())
            ))
        level = st.selectbox("Level", ["all", "error", "warning", "info"])
        filtered = [row for row in logs if level == "all" or row.level == level]
        for row in filtered:
            icon = {"error": "🔴", "warning": "🟠", "info": "🔵"}.get(row.level, "⚪")
            with st.expander(f"{icon} {row.created_at} · {row.stage} · {row.message}"):
                st.json(_json_text(row.details_json, {}))

