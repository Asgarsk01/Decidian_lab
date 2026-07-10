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
from decidian_docling.parser import DEFAULT_OUTPUT_DIR, parse_document
from decidian_docling.validation import ALLOWED_EXTENSIONS, sanitize_stem

WORK_DIR = Path(os.getenv("DECIDIAN_WORK_DIR", "work"))
PREVIEW_LIMIT = 250_000


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
        columns[index % 3].image(str(path), caption=path.name, use_container_width=True)


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
    metrics = st.columns(5)
    for column, (label, key) in zip(
        metrics,
        [
            ("Pages", "pages"),
            ("Elements", "elements"),
            ("Tables", "tables"),
            ("Pictures", "pictures"),
            ("Chunks", "chunks"),
        ],
    ):
        column.metric(label, counts.get(key, 0))

    if result.warnings:
        with st.expander(f"Warnings ({len(result.warnings)})", expanded=True):
            for warning in result.warnings:
                st.warning(warning)

    tabs = st.tabs(
        [
            "Summary",
            "Markdown",
            "JSON",
            "Chunks",
            "Tables",
            "Pictures",
            "Evaluation",
        ]
    )

    with tabs[0]:
        st.json(manifest, expanded=2)
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
                with st.expander(
                    f"Chunk {index + 1} — {data.get('token_count', 0)} tokens"
                ):
                    st.write(data.get("contextualized_text", ""))
                    st.json(
                        {
                            "headings": data.get("headings"),
                            "page_numbers": data.get("page_numbers"),
                            "source_refs": data.get("source_refs"),
                        },
                        expanded=False,
                    )
        else:
            st.info("Chunks were not generated.")

    with tabs[4]:
        table_dir = run_dir / "tables"
        _show_images(
            sorted(table_dir.glob("*.png")) if table_dir.exists() else [],
            "No tables were detected.",
        )
        csv_files = sorted(table_dir.glob("*.csv")) if table_dir.exists() else []
        for path in csv_files:
            with st.expander(path.name):
                import pandas as pd

                st.dataframe(pd.read_csv(path), use_container_width=True)

    with tabs[5]:
        picture_dir = run_dir / "pictures"
        _show_images(
            sorted(picture_dir.glob("*.png")) if picture_dir.exists() else [],
            "No pictures were detected.",
        )

    with tabs[6]:
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

    archive_key = f"download-archive-{run_dir.name}"
    if st.button("Prepare complete output ZIP", use_container_width=True):
        with st.spinner("Preparing ZIP from this run's generated output..."):
            st.session_state[archive_key] = build_download_archive(run_dir)

    archive_data = st.session_state.get(archive_key)
    if isinstance(archive_data, bytes):
        st.download_button(
            "Download complete output ZIP",
            data=archive_data,
            file_name=f"{run_dir.name}.zip",
            mime="application/zip",
            use_container_width=True,
        )


def main() -> None:
    st.set_page_config(
        page_title="Decidian Docling Lab",
        page_icon="📄",
        layout="wide",
    )
    st.title("Decidian Docling Lab")
    st.caption(
        "Local-only document parsing. No files are sent to R2, a database, or an LLM."
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
        disabled=uploaded is None,
        use_container_width=True,
    ):
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
                )
            st.session_state["latest_result"] = result
        except HarnessError as exc:
            st.error(str(exc))
        finally:
            temp_path.unlink(missing_ok=True)

    latest = st.session_state.get("latest_result")
    if isinstance(latest, RunResult):
        _render_result(latest)


if __name__ == "__main__":
    main()
