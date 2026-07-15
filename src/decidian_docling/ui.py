from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Iterable

import streamlit as st

from decidian_docling.artifacts import (
    QUALITY_FIELDS,
    build_download_archive,
    read_json,
    save_evaluation,
)
from decidian_docling.models import HarnessError, ParsingProfile, RunResult
from decidian_docling.config import get_gemini_settings
from decidian_docling.parser import DEFAULT_OUTPUT_DIR, parse_document
from decidian_docling.validation import ALLOWED_EXTENSIONS, sanitize_stem

WORK_DIR = Path(os.getenv("DECIDIAN_WORK_DIR", "work"))
PREVIEW_LIMIT = 250_000
AI_DASHBOARD_STATE_KEY = "ai-live-dashboard-state"


def _read_preview(path: Path, limit: int = PREVIEW_LIMIT) -> tuple[str, bool]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return text[:limit], len(text) > limit


def _show_images(paths: Iterable[Path], empty_message: str) -> None:
    images = list(paths)
    if not images:
        st.info(empty_message)
        return
    columns = st.columns(3)
    for index, path in enumerate(images):
        columns[index % 3].image(str(path), caption=path.name, width="stretch")


def _show_file_preview(path: Path, language: str = "text") -> None:
    if not path.exists():
        st.info(f"{path.name} was not generated.")
        return
    preview, truncated = _read_preview(path)
    st.code(preview, language=language)
    if truncated:
        st.warning(
            f"Preview limited to {PREVIEW_LIMIT:,} characters. "
            "Prepare the download ZIP below for the complete file."
        )


def _page_metric(manifest: dict) -> tuple[str | int, str | None]:
    """Avoid presenting missing DOCX pagination as a zero-page document."""
    source = manifest.get("source") or {}
    counts = manifest.get("counts") or {}
    if (
        source.get("extension") == ".docx"
        and not counts.get("pages")
    ):
        return "Unavailable", "DOCX page provenance is unavailable in this conversion."
    return counts.get("pages", 0), None


def _render_result(result: RunResult) -> None:
    run_dir = result.run_dir
    manifest = result.manifest
    st.subheader("Parsing result")
    status_method = (
        st.success
        if result.status.value == "success"
        else st.warning
        if result.status.value == "partial_success"
        else st.error
    )
    status_method(
        f"{result.status.value.replace('_', ' ').title()} — "
        f"{manifest.get('duration_seconds', 0)} seconds"
    )

    counts = manifest.get("counts", {})
    metrics = st.columns(7)
    page_value, page_help = _page_metric(manifest)
    metrics[0].metric("Pages", page_value, help=page_help)
    for column, (label, key) in zip(
        metrics[1:],
        [
            ("Elements", "elements"),
            ("Tables", "tables"),
            ("Pictures", "pictures"),
            ("Core chunks", "chunks"),
            ("Visual chunks", "picture_chunks"),
            ("Integrity", "semantic_integrity_findings"),
        ],
    ):
        column.metric(label, counts.get(key, 0))

    readiness = manifest.get("llm_readiness", "ready")
    if readiness == "review_required":
        st.warning("Legacy core readiness: review required")
    else:
        st.success("Legacy core readiness: ready")
    st.caption(
        "Legacy core readiness describes chunks.jsonl only. New downstream ingestion "
        "must use the clean-feed readiness below and clean_chunks.jsonl."
    )
    if manifest.get("visual_readiness") == "review_required":
        st.info(
            "Visual OCR readiness: review required. Core text/table chunks remain "
            "available separately in chunks.jsonl."
        )
    clean_readiness = manifest.get("clean_readiness", "blocked")
    ai_summary = manifest.get("ai_review") or {}
    if clean_readiness == "ready":
        st.success("Clean feed readiness: ready")
    elif clean_readiness == "partial_ready":
        st.warning("Clean feed readiness: partial — unresolved evidence was excluded")
    else:
        st.error("Clean feed readiness: blocked")
    st.caption(
        "Gemini review: "
        f"{ai_summary.get('status', 'not_configured')} · "
        f"{ai_summary.get('selected_diagram_count', 0)} diagram(s) selected · "
        f"{ai_summary.get('diagram_request_count', 0)} diagram pass(es) · "
        f"{ai_summary.get('request_count', 0)} API call(s) · "
        f"{ai_summary.get('verified_count', 0)} candidate(s) fully verified · "
        f"{ai_summary.get('unresolved_count', 0)} unresolved · "
        f"₹{float(ai_summary.get('estimated_cost_inr', 0)):.2f} estimated"
    )

    if result.warnings:
        with st.expander(f"Warnings ({len(result.warnings)})", expanded=True):
            for warning in result.warnings:
                st.warning(warning)

    tabs = st.tabs(
        [
            "Summary",
            "Markdown",
            "JSON",
            "Core chunks",
            "Visual OCR",
            "Tables",
            "Pictures",
            "Evaluation",
            "Canonical",
            "Verified clean",
            "Review queue",
            "Gemini audit",
            "AI events",
        ]
    )

    with tabs[0]:
        st.json(manifest, expanded=2)
        integrity_path = run_dir / "semantic_integrity.json"
        if integrity_path.exists():
            with st.expander("Semantic integrity", expanded=readiness == "review_required"):
                st.json(read_json(integrity_path), expanded=2)
        st.caption(f"Run directory: {run_dir}")

    with tabs[1]:
        markdown_path = run_dir / "document.md"
        if markdown_path.exists():
            markdown, truncated = _read_preview(markdown_path)
            st.markdown(markdown)
            if truncated:
                st.warning("Rendered Markdown preview is truncated.")
        else:
            st.info("Markdown was not generated.")

    with tabs[2]:
        _show_file_preview(run_dir / "document.json", language="json")

    with tabs[3]:
        chunks_path = run_dir / "chunks.jsonl"
        if chunks_path.exists():
            lines = chunks_path.read_text(encoding="utf-8").splitlines()
            st.caption(f"{len(lines)} chunks")
            for index, line in enumerate(lines):
                data = json.loads(line)
                origin = data.get("origin")
                label = (
                    f"Picture text — {data.get('trust', 'low')} trust"
                    if origin == "picture_text"
                    else f"Chunk {index + 1}"
                )
                with st.expander(
                    f"{label} — {data.get('token_count', 0)} tokens"
                ):
                    st.write(data.get("contextualized_text", ""))
                    st.json(
                        {
                            "headings": data.get("headings"),
                            "page_numbers": data.get("page_numbers"),
                            "source_refs": data.get("source_refs"),
                            "integrity_status": data.get("integrity_status"),
                            "integrity_finding_ids": data.get("integrity_finding_ids"),
                            "origin": origin or "document",
                            "trust": data.get("trust"),
                            "picture_file": data.get("picture_file"),
                        },
                        expanded=False,
                    )
        else:
            st.info("Core chunks were not generated.")

    with tabs[4]:
        visual_chunks_path = run_dir / "picture_chunks.jsonl"
        if visual_chunks_path.exists():
            lines = visual_chunks_path.read_text(encoding="utf-8").splitlines()
            st.caption(f"{len(lines)} visual-only chunk(s); excluded from the core feed")
            for index, line in enumerate(lines):
                data = json.loads(line)
                origin = data.get("origin", "picture_text")
                label = (
                    "Visual integrity warning"
                    if origin == "semantic_integrity_warning"
                    else f"Picture text — {data.get('trust', 'low')} trust"
                )
                with st.expander(
                    f"{label} — {data.get('token_count', 0)} tokens"
                ):
                    st.write(data.get("contextualized_text", ""))
                    st.json(
                        {
                            "page_numbers": data.get("page_numbers"),
                            "provenance_scope": data.get("provenance_scope"),
                            "source_refs": data.get("source_refs"),
                            "integrity_status": data.get("integrity_status"),
                            "integrity_finding_ids": data.get("integrity_finding_ids"),
                            "trust": data.get("trust"),
                            "picture_file": data.get("picture_file"),
                        },
                        expanded=False,
                    )
        else:
            st.info("No visual OCR chunks were generated.")

    with tabs[5]:
        table_dir = run_dir / "tables"
        _show_images(
            sorted(table_dir.glob("*.png")) if table_dir.exists() else [],
            "No tables were detected.",
        )
        csv_files = sorted(table_dir.glob("*.csv")) if table_dir.exists() else []
        for path in csv_files:
            with st.expander(path.name):
                import pandas as pd

                st.dataframe(pd.read_csv(path), width="stretch")

    with tabs[6]:
        picture_dir = run_dir / "pictures"
        _show_images(
            sorted(picture_dir.glob("*.png")) if picture_dir.exists() else [],
            "No pictures were detected.",
        )

    with tabs[7]:
        existing_path = run_dir / "evaluation.json"
        existing = read_json(existing_path) if existing_path.exists() else {}
        existing_scores = existing.get("scores", {})
        labels = {
            "reading_order": "Reading order",
            "headings": "Headings",
            "tables": "Tables",
            "ocr": "OCR",
            "images": "Images",
            "provenance": "Source provenance",
            "chunk_quality": "Chunk quality",
        }
        with st.form(f"evaluation-{run_dir.name}"):
            status = existing.get("status", "pending")
            if status != "completed":
                st.info(
                    "Evaluation is not saved yet. Set every field explicitly, "
                    "then click Save evaluation before downloading the ZIP."
                )
            scores: dict[str, int | None] = {}
            for field in QUALITY_FIELDS:
                stored_score = existing_scores.get(field)
                scores[field] = st.selectbox(
                    labels[field],
                    options=[None, 0, 1, 2],
                    index=(
                        [None, 0, 1, 2].index(stored_score)
                        if stored_score in {0, 1, 2}
                        else 0
                    ),
                    format_func=lambda value: (
                        "Unscored"
                        if value is None
                        else f"{value} — "
                        + {0: "broken", 1: "partial", 2: "correct"}[value]
                    ),
                    help="0 = broken, 1 = partial, 2 = correct",
                )
            notes = st.text_area(
                "Notes",
                value=existing.get("notes", ""),
                placeholder="Record missing text, broken tables, OCR issues, etc.",
            )
            if st.form_submit_button("Save evaluation"):
                if any(score is None for score in scores.values()):
                    st.error("Score every evaluation field before saving.")
                else:
                    save_evaluation(
                        run_dir,
                        {field: int(score) for field, score in scores.items()},
                        notes,
                    )
                    st.session_state.pop(f"download-archive-{run_dir.name}", None)
                    st.success("Evaluation saved.")
                    st.rerun()

    with tabs[8]:
        _show_file_preview(run_dir / "canonical_document.json", language="json")

    with tabs[9]:
        clean_path = run_dir / "clean_chunks.jsonl"
        if clean_path.exists():
            lines = clean_path.read_text(encoding="utf-8").splitlines()
            st.caption(f"{len(lines)} verified clean chunk(s); this is the downstream LLM feed")
            for index, line in enumerate(lines):
                data = json.loads(line)
                with st.expander(
                    f"Clean chunk {index + 1} — {data.get('content_type')} — "
                    f"{data.get('token_count', 0)} tokens"
                ):
                    st.write(data.get("contextualized_text", ""))
                    st.json(data, expanded=False)
        else:
            st.info("Verified clean chunks were not generated.")

    with tabs[10]:
        review_path = run_dir / "review_queue.jsonl"
        if review_path.exists():
            lines = review_path.read_text(encoding="utf-8").splitlines()
            st.caption(f"{len(lines)} unresolved item(s), excluded from clean_chunks.jsonl")
            for line in lines:
                data = json.loads(line)
                with st.expander(f"{data.get('candidate_id')} — {data.get('kind')}"):
                    st.json(data, expanded=2)
                    evidence = data.get("evidence_path")
                    if evidence and (run_dir / evidence).exists():
                        st.image(str(run_dir / evidence), width="stretch")
        else:
            st.info("No review queue was generated.")

    with tabs[11]:
        _show_file_preview(run_dir / "gemini_review.json", language="json")

    with tabs[12]:
        events_path = run_dir / "gemini_events.jsonl"
        if events_path.exists():
            events = [
                json.loads(line)
                for line in events_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            st.caption(f"{len(events)} append-only AI progress and cost event(s)")
            st.dataframe(_event_rows(events), width="stretch", hide_index=True)
            with st.expander("Complete event JSON"):
                st.code(
                    events_path.read_text(encoding="utf-8"),
                    language="json",
                )
        else:
            st.info("No AI events were recorded for this run.")

    archive_key = f"download-archive-{run_dir.name}"
    if st.button("Prepare complete output ZIP", width="stretch"):
        with st.spinner("Preparing ZIP from this run's generated output..."):
            st.session_state[archive_key] = build_download_archive(run_dir)

    archive_data = st.session_state.get(archive_key)
    if isinstance(archive_data, bytes):
        st.download_button(
            "Download complete output ZIP",
            data=archive_data,
            file_name=f"{run_dir.name}.zip",
            mime="application/zip",
            width="stretch",
        )


def _event_rows(events: list[dict]) -> list[dict]:
    rows = []
    for event in events:
        request_usage = event.get("request_usage") or {}
        rows.append({
            "#": event.get("sequence"),
            "Time": str(event.get("timestamp", ""))[11:19],
            "Event": event.get("event"),
            "Candidate": event.get("candidate_id"),
            "Stage": event.get("stage"),
            "Attempt": event.get("attempt"),
            "Calls": f"{event.get('requests_used', 0)}/{event.get('requests_limit', 0)}",
            "Diagram passes": event.get("diagram_requests", 0),
            "Evidence files": event.get("evidence_count"),
            "Request tokens": request_usage.get("total_tokens"),
            "Response": event.get("response_status"),
            "Error": event.get("error_type"),
            "Total tokens": event.get("total_tokens", 0),
            "Thinking": event.get("thought_tokens", 0),
            "Spend": f"₹{float(event.get('estimated_cost_inr', 0)):.2f}",
            "Remaining": f"₹{float(event.get('budget_remaining_inr', 0)):.2f}",
            "Elapsed": f"{float(event.get('elapsed_seconds', 0)):.1f}s",
            "Message": event.get("message"),
        })
    return rows


def _render_live_ai_dashboard(
    placeholder,
    state: dict,
    events: list[dict],
) -> None:
    with placeholder.container():
        st.subheader("Live AI dashboard")
        phase = state.get("phase", "waiting")
        message = state.get("message", "Waiting for parsing to start.")
        if state.get("event") == "review_completed" and state.get("review_status") == "success":
            st.success(message)
        elif state.get("event") == "review_completed" and state.get("review_status") == "failed":
            st.error(message)
        elif state.get("event") in {"request_failed", "response_incomplete", "response_validation_failed"}:
            st.error(message)
        elif state.get("stop_reason"):
            st.warning(message)
        else:
            st.info(message)

        metrics = st.columns(4)
        metrics[0].metric("Phase", str(phase).replace("_", " ").title())
        metrics[1].metric(
            "Diagrams selected",
            f"{state.get('selected_diagrams', 0)}/{state.get('detected_diagrams', 0)}",
        )
        candidate_position = state.get("candidate_position", 0)
        selected_candidates = state.get("selected_candidates", 0)
        metrics[2].metric(
            "Current candidate",
            f"{candidate_position}/{selected_candidates}" if selected_candidates else "—",
        )
        metrics[3].metric(
            "Diagram API passes",
            state.get("diagram_requests", 0),
            help="Actual Gemini request attempts carrying diagram evidence, including retries and verification.",
        )
        metrics = st.columns(4)
        metrics[0].metric(
            "API calls",
            f"{state.get('requests_used', 0)}/{state.get('requests_limit', 0)}",
        )
        metrics[1].metric(
            "Verifications",
            f"{state.get('verifications_used', 0)}/{state.get('verifications_limit', 0)}",
        )
        metrics[2].metric("Total tokens", f"{int(state.get('total_tokens', 0)):,}")
        metrics[3].metric(
            "Estimated spend",
            f"₹{float(state.get('estimated_cost_inr', 0)):.2f} / ₹{float(state.get('budget_inr', 0)):.0f}",
        )

        budget = max(1.0, float(state.get("budget_inr", 100)))
        spend = max(0.0, float(state.get("estimated_cost_inr", 0)))
        st.progress(
            min(1.0, spend / budget),
            text=(
                f"Budget: ₹{spend:.2f} spent · "
                f"₹{max(0.0, budget - spend):.2f} remaining"
            ),
        )
        request_limit = max(1, int(state.get("requests_limit", 1)))
        st.progress(
            min(1.0, int(state.get("requests_used", 0)) / request_limit),
            text=f"Requests: {state.get('requests_used', 0)} of {request_limit}",
        )
        runtime_limit = max(1.0, float(state.get("runtime_limit_seconds", 300)))
        elapsed = max(0.0, float(state.get("elapsed_seconds", 0)))
        st.progress(
            min(1.0, elapsed / runtime_limit),
            text=f"AI time: {elapsed:.1f}s of {runtime_limit:.0f}s",
        )

        detail = st.columns(4)
        detail[0].caption(f"Model: {state.get('model', '—')}")
        detail[1].caption(f"Thinking: {state.get('thinking_level', '—')}")
        detail[2].caption(f"Candidate: {state.get('candidate_id', '—')}")
        detail[3].caption(f"Action: {state.get('stage', state.get('event', '—'))}")
        if events:
            st.dataframe(
                _event_rows(events[-50:]),
                width="stretch",
                hide_index=True,
                height=300,
            )


def _dashboard_defaults(settings) -> dict:
    return {
        "phase": "waiting",
        "model": settings.model,
        "thinking_level": settings.thinking_level,
        "budget_inr": settings.budget_inr,
        "requests_limit": settings.max_requests,
        "verifications_limit": settings.max_verifications,
        "runtime_limit_seconds": settings.max_runtime_seconds,
    }


def _read_ai_events(path: Path | None) -> list[dict]:
    if path is None or not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            if line.strip():
                events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _live_progress_callback(
    placeholder,
    settings,
    *,
    reset: bool = False,
    replay_path: Path | None = None,
):
    replay_key = str(replay_path.parent) if replay_path is not None else None
    stored = st.session_state.get(AI_DASHBOARD_STATE_KEY)
    if reset:
        stored = {"state": _dashboard_defaults(settings), "events": [], "run_key": None}
    elif not isinstance(stored, dict) or (replay_key and stored.get("run_key") != replay_key):
        replayed = _read_ai_events(replay_path)
        state = _dashboard_defaults(settings)
        for event in replayed:
            state.update(event)
        stored = {
            "state": state,
            "events": replayed,
            "run_key": replay_key,
        }
    state = stored["state"]
    events = stored["events"]
    st.session_state[AI_DASHBOARD_STATE_KEY] = stored
    _render_live_ai_dashboard(placeholder, state, events)

    def update(event: dict) -> None:
        state.update(event)
        events.append(dict(event))
        st.session_state[AI_DASHBOARD_STATE_KEY] = {
            "state": state,
            "events": events,
            "run_key": stored.get("run_key"),
        }
        _render_live_ai_dashboard(placeholder, state, events)

    return update


def main() -> None:
    st.set_page_config(
        page_title="Decidian Docling Lab",
        page_icon="📄",
        layout="wide",
    )
    st.title("Decidian Docling Lab")
    default_gemini_settings = get_gemini_settings()
    st.caption("Local Docling parsing with optional targeted Gemini verification.")
    ai_review = st.toggle(
        "Gemini defence",
        value=default_gemini_settings.enabled,
        help="Sends qualifying diagrams and ambiguous DOCX/PDF evidence to Gemini for two-pass verification.",
    )
    # Re-read settings with the operator's current toggle selection. In
    # particular, `configured` must become true when an API key exists and a
    # previously disabled default is enabled in the UI.
    gemini_settings = get_gemini_settings(ai_review)
    if ai_review and gemini_settings.configured:
        st.warning(
            "External processing enabled: selected diagram and ambiguity evidence will be sent to Gemini. "
            f"Model: {gemini_settings.model}; thinking: {gemini_settings.thinking_level}; "
            f"run budget: ₹{gemini_settings.budget_inr:.0f}."
        )
    elif ai_review:
        st.info("Gemini defence is enabled but GEMINI_API_KEY is not configured; unresolved evidence will be excluded and queued.")

    ai_approved = True
    if ai_review and gemini_settings.configured:
        with st.expander("AI limits and approval", expanded=True):
            st.write({
                "model": gemini_settings.model,
                "thinking_level": gemini_settings.thinking_level,
                "maximum_spend_inr": gemini_settings.budget_inr,
                "maximum_candidates": gemini_settings.max_candidates,
                "maximum_api_requests": gemini_settings.max_requests,
                "maximum_verifications": gemini_settings.max_verifications,
                "maximum_ai_runtime_seconds": gemini_settings.max_runtime_seconds,
                "maximum_total_tokens": gemini_settings.max_total_tokens,
                "maximum_output_tokens_per_request": gemini_settings.max_output_tokens,
                "per_request_timeout_seconds": gemini_settings.timeout_seconds,
                "retries": gemini_settings.max_retries,
            })
            st.caption(
                "The ₹ amount is a conservative live estimate based on configured token "
                "rates. The job stops scheduling calls when any guardrail is reached."
            )
            ai_approved = st.checkbox(
                f"I approve this run with a maximum estimated AI budget of ₹{gemini_settings.budget_inr:.0f}",
                key="ai-cost-approval",
            )

    latest = st.session_state.get("latest_result")
    replay_path = (
        latest.run_dir / "gemini_events.jsonl"
        if isinstance(latest, RunResult)
        else None
    )
    live_dashboard = st.empty() if ai_review else None
    progress_callback = (
        _live_progress_callback(
            live_dashboard,
            gemini_settings,
            replay_path=replay_path,
        )
        if live_dashboard is not None
        else None
    )

    uploaded = st.file_uploader(
        "Choose a document",
        type=sorted(extension.lstrip(".") for extension in ALLOWED_EXTENSIONS),
        accept_multiple_files=False,
        help="Maximum file size: 100 MB.",
    )
    profile = st.selectbox(
        "Parsing profile",
        options=list(ParsingProfile),
        format_func=lambda item: item.value.title(),
        help=(
            "Standard uses automatic OCR. Scanned forces full-page OCR. "
            "Visual also enables picture classification and chart extraction."
        ),
    )
    if uploaded is not None:
        st.write(
            {
                "filename": uploaded.name,
                "size_mb": round(uploaded.size / (1024 * 1024), 2),
                "profile": profile.value,
                "artifact_mode": "extraction",
            }
        )

    if st.button(
        "Parse document",
        type="primary",
        disabled=uploaded is None or not ai_approved,
        width="stretch",
    ):
        if live_dashboard is not None:
            progress_callback = _live_progress_callback(
                live_dashboard,
                gemini_settings,
                reset=True,
            )
        WORK_DIR.mkdir(parents=True, exist_ok=True)
        suffix = Path(uploaded.name).suffix.lower()
        temp_name = f"{sanitize_stem(uploaded.name)}-{uuid.uuid4().hex}{suffix}"
        temp_path = WORK_DIR / temp_name
        try:
            temp_path.write_bytes(uploaded.getbuffer())
            with st.spinner(
                "Docling is parsing the document. The first run may download models."
            ):
                result = parse_document(
                    temp_path,
                    profile=profile,
                    output_root=DEFAULT_OUTPUT_DIR,
                    ai_review=ai_review,
                    ai_progress_callback=progress_callback,
                )
            st.session_state["latest_result"] = result
            dashboard_state = st.session_state.get(AI_DASHBOARD_STATE_KEY)
            if isinstance(dashboard_state, dict):
                dashboard_state["run_key"] = str(result.run_dir)
                st.session_state[AI_DASHBOARD_STATE_KEY] = dashboard_state
        except HarnessError as exc:
            st.error(str(exc))
        finally:
            temp_path.unlink(missing_ok=True)

    latest = st.session_state.get("latest_result")
    if isinstance(latest, RunResult):
        _render_result(latest)


if __name__ == "__main__":
    main()
