# Decidian Local

Decidian Local is a single-user Streamlit prototype for turning DOCX SRS, RFC, and proposal documents into traceable **decision candidates**. It parses documents locally, extracts every embedded image occurrence, analyzes images and document sections with Gemini, and gives a human reviewer the only path to approve a candidate as a versioned decision.

## Requirements

- Python 3.11 or newer
- A Google Gemini API key for image analysis, candidate extraction, and validation
- A DOCX input file (no PDF or other upload formats)

The prototype uses [Docling](https://docling-project.github.io/docling/) as its primary local document converter and the current official [Google Gen AI Python SDK](https://ai.google.dev/gemini-api/docs/quickstart) (`google-genai`) for Gemini requests. AI responses use Pydantic-backed structured output.

## Setup

From the `decidian-local` directory:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` and add your key:

```dotenv
GEMINI_API_KEY=your_real_key
GEMINI_TEXT_MODEL=gemini-3.5-flash
GEMINI_VISION_MODEL=gemini-3.5-flash
DATABASE_URL=sqlite:///data/decidian_local.db
STORAGE_DIR=storage
```

Model names are read only from `.env`. Model availability differs by account and can change. If Gemini reports that a configured model is unavailable, change the corresponding `GEMINI_TEXT_MODEL` or `GEMINI_VISION_MODEL` value and restart Streamlit.

## Run

```powershell
streamlit run app.py
```

Open the URL printed by Streamlit, upload a `.docx`, select its document type, and choose **Save and run full pipeline**. Unsupported extensions are rejected both by the UI and storage service.

## Pipeline and human-review boundary

1. The original DOCX is saved and registered in SQLite.
2. Docling creates the primary Markdown/document conversion; `python-docx` normalizes headings, paragraphs, lists, tables, captions, and image references into stable internal blocks.
3. Word package relationships and `word/media/` are inspected so every image occurrence is preserved. Identical bytes share a SHA-256 hash but remain separate UI rows.
4. Gemini Vision analyzes every unique binary image. Later duplicate occurrences receive an explicit duplicate analysis derived from the first hash analysis; nothing is silently skipped.
5. Blocks and image analyses are grouped into heading-based packets. The entire document is never sent as one prompt.
6. Gemini creates candidates only. Invalid source IDs are removed, and a candidate with no valid evidence is not inserted.
7. Gemini support validation and local normalized-text duplicate detection annotate candidates without deleting them.
8. A reviewer explicitly edits, approves, or rejects each candidate. Only the **Approve** action creates `decisions`, `decision_versions`, and `decision_sources` rows.

An AI request failure for one image or one packet is logged and sibling work continues. Image failures create an explicit `unreadable` audit record. Raw Gemini responses and errors are retained.

## Local outputs

For document ID `doc_xxx`, outputs are written under:

```text
storage/uploads/doc_xxx/original.docx
storage/parsed/doc_xxx/parsed_document.json
storage/parsed/doc_xxx/preview.md
storage/images/doc_xxx/img_001.png
storage/image_analysis/doc_xxx/image_analysis.json
storage/extraction_packets/doc_xxx/packets.jsonl
storage/decision_outputs/doc_xxx/decision_candidates.json
storage/decision_outputs/doc_xxx/validation_report.json
```

The SQLite database defaults to `data/decidian_local.db`. It stores documents, blocks, image occurrences, image analyses, extraction packets, decision candidates, validation annotations, review outcomes, approved decision versions/sources, and processing logs. The **Raw JSON Viewer** exposes both artifact files and database rows.

## Test

```powershell
python -m unittest discover -s tests -v
python scripts/create_sample_docx.py
```

The automated test builds a real DOCX fixture, verifies structured blocks and duplicate image occurrences, builds a packet, runs candidate extraction/validation through a deterministic fake Gemini client, and confirms that only a direct review-service approval creates a decision.

## Known limitations

- DOCX image-position mapping can be approximate; complex floating images may not map exactly to nearby text.
- Images stored in uncommon Word formats such as EMF/WMF may be preserved but not previewable or accepted by Gemini. Failures remain visible and auditable.
- Gemini output requires human review and can be wrong even when schema-valid.
- This prototype does not check source code or pull requests.
- PDF and other document formats are not supported.
- Multi-user/team authentication and authorization are not implemented.
- Re-running extraction for a document that already has approved decisions is intentionally not exposed in this prototype; upload a new copy instead.

