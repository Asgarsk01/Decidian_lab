# Decidian Local

**Decidian Local** is a single-user Streamlit prototype that turns `.docx` SRS, RFC, and proposal documents into traceable, versioned **decisions** through a mandatory human-review gate. It parses documents entirely on your machine, extracts every embedded image, analyzes both text and images using Google Gemini, and enforces that no AI-generated output ever becomes a decision without an explicit human approval action.

> **Branch: `feature/docx-only`** — This branch accepts only `.docx` input. PDF and all other formats are rejected both at the UI layer (`StorageService.validate_docx_filename`) and the Streamlit uploader.

---

## How It Works — End to End

### 1. Upload
You upload a `.docx` file and select its document type (`srs`, `rfc`, `proposal`, `other`). The file extension is validated in two places — `StorageService.validate_docx_filename()` and the Streamlit uploader's `type=["docx"]` filter. The file is saved to `storage/uploads/<doc_id>/original.docx` and registered in the SQLite `documents` table with status `uploaded`.

### 2. DOCX Parsing (`src/services/docx_parser.py`)
Two passes run over the file:

- **Docling pass** — Converts the DOCX to Markdown (saved to `storage/parsed/<doc_id>/preview.md`) and captures Docling's internal JSON representation.
- **python-docx normalization pass** — Walks every `w:p` paragraph and `w:tbl` table in the Word XML body and produces stable, typed **blocks**:

| Block type | Source |
|---|---|
| `heading` | `Heading N` style, capped at level 9 |
| `paragraph` | Plain text paragraphs |
| `list` | Bullet or numbered items, resolved from Word's numbering XML |
| `table` | Rendered as GitHub-flavored Markdown |
| `caption` | Paragraphs with a `Caption` style |
| `image_reference` | Paragraphs containing an `a:blip` element (inline image anchor) |

Each block carries a `heading_path` (full heading hierarchy above it) and a sequential `position_index`. Blocks are persisted to `document_blocks` and to `storage/parsed/<doc_id>/parsed_document.json`.

### 3. Image Extraction (`src/services/image_extractor.py`)
Two-pass extraction:

**Pass A — Relationship-mapped images:** Walks every paragraph for `a:blip` elements. For each blip, resolves the `r:embed` relationship to the actual binary blob and captures the heading path, caption (if the next paragraph has a `Caption` style), and the nearest non-empty text before and after (used as Gemini context). These images get `mapping_quality = "exact"`.

**Pass B — Unreferenced media assets:** Opens the DOCX zip archive directly and collects any file under `word/media/` not already mapped in Pass A. These get `mapping_quality = "unknown"` and a warning is logged.

Every image is saved to `storage/images/<doc_id>/img_NNN.<ext>`. Identical binaries share a SHA-256 hash, but each occurrence gets its own `document_images` row and its own `image_id`. No image is silently skipped.

### 4. Gemini Image Analysis (`src/services/image_analyzer.py`)
Each **unique** image binary (deduplicated by SHA-256) is sent to Gemini Vision with a structured prompt. Gemini classifies:

- `importance` — `critical`, `relevant`, `unclear`, `decorative`, `duplicate`, or `unreadable`
- `image_type` — workflow diagram, architecture, UI mockup, table, etc.
- `is_decision_relevant` — boolean
- `extracted_logic` — structured rules visible in the image
- `unclear_parts` — things that could not be determined

Duplicate occurrences (same hash, later occurrence) receive an explicit `duplicate` record derived from the first analysis — nothing is silently skipped. If Gemini fails for a single image the error is logged, an `unreadable` audit record is created, and the remaining images continue. All analyses are stored in the `image_analyses` table and in `storage/image_analysis/<doc_id>/image_analysis.json`.

### 5. Packet Building (`src/services/packet_builder.py`)
Blocks and image analyses are grouped by `heading_path` into **section packets**. Each packet contains a `section_key` (e.g., `"Requirements > Authentication > Login"`), all blocks under that heading, and the image analyses for images anchored to that heading (full detail for `critical`/`relevant`/`unclear`; brief summary for others). The entire document is never sent as one prompt. Packets are stored in `extraction_packets` and written to `storage/extraction_packets/<doc_id>/packets.jsonl`.

### 6. Decision Candidate Extraction (`src/services/decision_extractor.py`)
Each packet is sent to Gemini with a structured prompt asking it to extract only real decisions — business rules, workflows, constraints, validation rules, permission rules, state transitions, security/integration/data/audit/error-handling rules. Gemini returns a `DecisionCandidateBatch` (Pydantic schema). Each candidate is then validated:

- `source_block_ids` and `source_image_ids` are cross-checked against the actual IDs in the packet — any invented IDs are removed
- A candidate with no valid evidence (no block ID, no image ID after filtering) is **not inserted**
- `requires_human_review` is a mandatory schema field — the Pydantic validator rejects any candidate that sets it to `False`

Accepted candidates are written to `decision_candidates` (status `pending_review`) and to `storage/decision_outputs/<doc_id>/decision_candidates.json`.

### 7. Validation (`src/services/decision_extractor.validate_candidates`)
Each candidate is validated two ways:

**Local fuzzy duplicate detection** — `rapidfuzz` normalized-text similarity. Any two candidates with ≥ 90% similarity are flagged; the later one gets `possible_duplicate_candidate_id` set and its validation status is at least `warning`.

**Gemini validation** — Each candidate is re-sent to Gemini with its source packet and all other candidate statements. Gemini checks source support, vague wording, rejected-option wording, and conflicts. Returns `CandidateValidationResult` (`passed`, `warning`, or `failed`) with notes. Candidates are annotated, never deleted.

### 8. Human Review — The Only Gate to a Decision
The **Decision Candidates** tab shows every candidate. A reviewer can:

| Action | Effect |
|---|---|
| **Edit** | Updates statement, type, criticality, system area hint, and structured constraints. Status → `edited_candidate`. Still not a decision. |
| **Approve** | Calls `review_service.approve_candidate()`. Creates a `Decision` row, a `DecisionVersion` (version 1), and a `DecisionSource` row. Candidate status → `approved_candidate`. **This is the only code path that writes to `decisions`, `decision_versions`, and `decision_sources`.** |
| **Reject** | Sets status to `rejected_candidate` with an optional reason. A rejected candidate must be edited before it can be re-approved. An approved decision cannot be rejected through candidate review. |

---

## Data Model

```
documents
  ├── document_blocks          (typed blocks parsed from the DOCX)
  ├── document_images          (every image occurrence, including duplicates)
  ├── image_analyses           (Gemini Vision output per image)
  ├── extraction_packets       (heading-grouped section packets)
  ├── decision_candidates      (AI-generated; all require human review)
  │    └── decisions           (created ONLY on explicit Approve action)
  │         └── decision_versions   (versioned rule text + constraints)
  │              └── decision_sources    (traceability: block IDs + image IDs)
  └── processing_logs          (every stage event, warning, and error)
```

---

## Streamlit UI Tabs

| Tab | Contents |
|---|---|
| **Upload DOCX** | File uploader (`.docx` only), document type selector, pipeline trigger |
| **Parsed Document Preview** | Block count, heading outline, Markdown preview, full normalized block list |
| **Extracted Images** | Every occurrence with preview, hash, caption, nearby text, mapping quality |
| **Gemini Image Analysis** | Importance, type, extracted logic, unclear parts, raw Gemini response |
| **Extraction Packets** | All section packets with block and image analysis counts |
| **Decision Candidates** | Full review UI — metrics, candidate details, source images, edit form, Approve/Reject |
| **Raw JSON Viewer** | Inspect output files (JSON/JSONL) or any database table directly |
| **Logs / Errors** | All processing log entries, filterable by level (`info` / `warning` / `error`) |

---

## File Outputs

```
storage/
  uploads/<doc_id>/original.docx
  parsed/<doc_id>/parsed_document.json
  parsed/<doc_id>/preview.md
  images/<doc_id>/img_001.png  ...
  image_analysis/<doc_id>/image_analysis.json
  extraction_packets/<doc_id>/packets.jsonl
  decision_outputs/<doc_id>/decision_candidates.json
  decision_outputs/<doc_id>/validation_report.json

data/decidian_local.db          (SQLite database)
```

---

## Requirements

- Python 3.11 or newer
- A Google Gemini API key
- A `.docx` input file — no other formats are accepted on this branch

### Python Dependencies

| Package | Version | Purpose |
|---|---|---|
| `streamlit` | ≥1.40, <2 | Web UI |
| `python-dotenv` | ≥1.0, <2 | `.env` loading |
| `SQLAlchemy` | ≥2.0, <3 | SQLite ORM |
| `pydantic` | ≥2.8, <3 | Structured AI output schemas |
| `docling` | ≥2.50, <3 | Primary DOCX → Markdown conversion |
| `python-docx` | ≥1.1, <2 | Block normalization and image extraction |
| `lxml` | ≥5.0, <7 | XML processing for Word internals |
| `Pillow` | ≥10, <13 | Image handling |
| `google-genai` | ≥1.0, <2 | Official Google Gemini SDK |
| `rapidfuzz` | ≥3.9, <4 | Fuzzy duplicate detection |
| `orjson` | ≥3.10, <4 | Fast JSON serialization |

---

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env`:

```dotenv
GEMINI_API_KEY=your_real_key_here
GEMINI_TEXT_MODEL=gemini-2.0-flash
GEMINI_VISION_MODEL=gemini-2.0-flash
DATABASE_URL=sqlite:///data/decidian_local.db
STORAGE_DIR=storage
```

> Model names are read only from `.env`. If Gemini reports a model is unavailable for your account, change the corresponding value and restart Streamlit.

---

## Run

```powershell
streamlit run app.py
```

---

## Test

```powershell
python -m unittest discover -s tests -v
python scripts/create_sample_docx.py
```

The test suite builds a real DOCX fixture, verifies structured block parsing and duplicate image detection, builds a section packet, runs candidate extraction and validation through a deterministic fake Gemini client, and confirms that only an explicit `approve_candidate()` call creates a decision record.

---

## Configuration Reference

All settings are loaded from `.env` via `src/config.py` and exposed as a cached Pydantic `Settings` object:

| Variable | Default | Description |
|---|---|---|
| `GEMINI_API_KEY` | _(required)_ | Google Gemini API key |
| `GEMINI_TEXT_MODEL` | `gemini-3.5-flash` | Model for candidate extraction and validation |
| `GEMINI_VISION_MODEL` | `gemini-3.5-flash` | Model for image analysis |
| `DATABASE_URL` | `sqlite:///data/decidian_local.db` | SQLAlchemy database URL |
| `STORAGE_DIR` | `storage` | Root directory for all file outputs |

---

## Error Handling and Auditability

- A Gemini failure for a **single image** is caught, logged, and an `unreadable` analysis record is created; other images continue.
- A Gemini failure for a **single packet** is caught, logged, and an empty candidate list is stored for that packet; other packets continue.
- A `GeminiModelUnavailableError` (HTTP 404 / "model not found") is re-raised immediately with a clear message directing you to update `.env`.
- Every raw Gemini response (text + parsed output + token usage) is stored alongside the processed record.
- All pipeline events, warnings, and errors are visible in the **Logs / Errors** tab.
- Re-running extraction for a document that already has approved decisions is intentionally not exposed; upload a new copy instead.

---

## Known Limitations

- DOCX image-position mapping can be approximate; complex floating-layout images may not map exactly to their nearest text.
- Images in EMF/WMF format are preserved and audited but may not be previewable in the browser or accepted by Gemini.
- Gemini output requires human review and can be wrong even when schema-valid.
- This prototype does not check source code or pull requests.
- PDF and all other document formats are not supported (by design on this branch).
- Multi-user authentication and authorization are not implemented.

