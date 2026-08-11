# Decidian

> Turn `.docx` specification documents into traceable, versioned decisions — with a mandatory human-review gate between every AI suggestion and every approved rule.

**Decidian** is an AI-assisted decision extraction pipeline that processes Word documents (SRS, RFC, proposals) locally on your machine. It parses the document structure, extracts every embedded image, runs Google Gemini analysis over both text and visuals, and then puts a human in the loop before anything becomes a versioned decision record.

**No AI output is ever promoted to a decision automatically.** Every candidate requires an explicit Approve action from a reviewer.

---

> **You are on the `feature/docx-only` branch.**
> This branch implements the full end-to-end Decidian pipeline scoped exclusively to `.docx` input. It uses a lightweight Python + Streamlit stack with SQLite — no Docker required.
>
> The `main` branch is a separate Docling parsing laboratory that supports multiple document formats (PDF, DOCX, PPTX, images, etc.) in a Docker environment. The architecture, purpose, and dependency stack differ significantly between the two branches.

---

## Table of Contents

- [What Decidian Does](#what-decidian-does)
- [How the Pipeline Works](#how-the-pipeline-works)
- [Project Structure](#project-structure)
- [Data Model](#data-model)
- [Streamlit UI Tabs](#streamlit-ui-tabs)
- [File Outputs](#file-outputs)
- [Quick Start](#quick-start)
- [Configuration](#configuration)
- [Running Tests](#running-tests)
- [Contributing](#contributing)
- [Known Limitations](#known-limitations)

---

## What Decidian Does

Given a `.docx` requirements or design document, Decidian:

1. **Parses** the document into typed, heading-grouped blocks using [Docling](https://docling-project.github.io/docling/) and `python-docx`
2. **Extracts** every embedded image, preserving context (caption, nearby text, heading position)
3. **Analyzes** each unique image with Gemini Vision to classify decision-relevant visual content
4. **Groups** blocks and image analyses into section packets (one per heading group)
5. **Extracts** decision candidates from each packet using Gemini with a structured Pydantic schema
6. **Validates** candidates — fuzzy duplicate detection + Gemini cross-check
7. **Presents** candidates in a review UI where a human can Edit, Approve, or Reject
8. **Records** only approved candidates as versioned, source-traced decisions in SQLite

---

## How the Pipeline Works

### Stage 1 — Upload & Register
You upload a `.docx` and select a document type (`srs`, `rfc`, `proposal`, `other`). The file extension is enforced at two layers:
- `StorageService.validate_docx_filename()` — rejects anything not `.docx`
- Streamlit uploader `type=["docx"]` filter

The file is stored at `storage/uploads/<doc_id>/original.docx` and registered in SQLite with status `uploaded`.

---

### Stage 2 — DOCX Parsing (`src/services/docx_parser.py`)

Two passes run in sequence:

**Docling pass** — Primary conversion to Markdown. Saved to `storage/parsed/<doc_id>/preview.md`.

**python-docx normalization pass** — Walks every `w:p` and `w:tbl` element in the Word XML body and produces stable, typed blocks:

| Block Type | How It's Detected |
|---|---|
| `heading` | `Heading N` paragraph style (capped at level 9) |
| `paragraph` | Plain text paragraph |
| `list` | Bullet/numbered list, resolved from Word's numbering XML |
| `table` | Rendered as GitHub-flavored Markdown |
| `caption` | Paragraph with a `Caption` style |
| `image_reference` | Paragraph containing an `a:blip` element |

Each block carries a `heading_path` (the full heading stack above it) and a `position_index`. All blocks are written to the `document_blocks` table and `storage/parsed/<doc_id>/parsed_document.json`.

---

### Stage 3 — Image Extraction (`src/services/image_extractor.py`)

**Pass A — Relationship-mapped images:** Walks every paragraph for `a:blip` elements. Resolves `r:embed` relationships to binary blobs. Captures heading path, caption (if next paragraph has `Caption` style), and nearest text before/after for Gemini context. `mapping_quality = "exact"`.

**Pass B — Unreferenced media:** Scans `word/media/` in the DOCX zip for any image not captured in Pass A. `mapping_quality = "unknown"`, logged as a warning.

All images are saved to `storage/images/<doc_id>/img_NNN.<ext>`. Each occurrence gets its own `document_images` row and `image_id`. Identical binaries share a SHA-256 hash but are **not deduplicated at storage level** — every occurrence is preserved.

---

### Stage 4 — Gemini Image Analysis (`src/services/image_analyzer.py`)

Each **unique binary** (by SHA-256) is sent to Gemini Vision. The structured prompt asks Gemini to classify:

| Field | Values |
|---|---|
| `importance` | `critical`, `relevant`, `unclear`, `decorative`, `duplicate`, `unreadable` |
| `image_type` | workflow diagram, architecture, UI mockup, table, etc. |
| `is_decision_relevant` | boolean |
| `extracted_logic` | structured rules visible in the image |
| `unclear_parts` | anything that could not be determined |

Later duplicate occurrences (same hash) receive an explicit `duplicate` record — no image is silently skipped. If Gemini fails for a single image, an `unreadable` audit record is created and analysis continues for remaining images.

---

### Stage 5 — Packet Building (`src/services/packet_builder.py`)

Blocks and image analyses are grouped by `heading_path` into **section packets**. Each packet includes:
- A `section_key` string (e.g., `"Authentication > Login Flow"`)
- All blocks under that heading
- Image analyses anchored to that heading (full detail for `critical`/`relevant`/`unclear`, brief for others)

The entire document is **never sent as one prompt**. Packets are stored in `extraction_packets` and `storage/extraction_packets/<doc_id>/packets.jsonl`.

---

### Stage 6 — Decision Candidate Extraction (`src/services/decision_extractor.py`)

Each packet is sent to Gemini with a structured extraction prompt. Gemini must return a `DecisionCandidateBatch` — a Pydantic-validated list of candidates covering business rules, constraints, workflows, validation rules, permission rules, state transitions, and security/integration/data/audit/error-handling rules.

Two hard guards are enforced per candidate **before** insertion:
- `source_block_ids` and `source_image_ids` are cross-checked against the packet — invented IDs are stripped
- A candidate with **no valid evidence** (no block ID, no image ID after stripping) is **not inserted**

A third guard is enforced by the Pydantic schema itself:
- `requires_human_review` must be `True` — the validator raises an error if an AI response sets it to `False`

---

### Stage 7 — Validation

**Local fuzzy deduplication** — `rapidfuzz` normalized-text similarity. Candidates with ≥ 90% similarity are flagged; the later candidate gets `possible_duplicate_candidate_id` set and at least a `warning` status.

**Gemini validation** — Each candidate is re-sent with its source packet and all other candidate statements. Gemini checks source support, vagueness, rejected-option wording, and conflicts. Returns `CandidateValidationResult` (`passed`, `warning`, or `failed`). Candidates are annotated, never deleted.

---

### Stage 8 — Human Review

The **Decision Candidates** tab is the only path from an AI suggestion to an approved decision.

| Action | What Happens |
|---|---|
| **Edit** | Updates the candidate's statement, type, criticality, system area, and constraints. Status → `edited_candidate`. Still not a decision. |
| **Approve** | Calls `review_service.approve_candidate()`. Creates a `Decision`, a `DecisionVersion` (v1), and a `DecisionSource` tracing back to the source blocks and images. **This is the only code path that writes to `decisions`, `decision_versions`, or `decision_sources`.** |
| **Reject** | Sets status to `rejected_candidate` with an optional reason. A rejected candidate must be edited before it can be re-approved. An approved decision cannot be rejected through candidate review. |

---

## Project Structure

```
decidian/                        ← repo root (this branch)
├── app.py                       ← Streamlit UI (8 tabs)
├── requirements.txt
├── .env.example
├── sample_srs.docx              ← example input document
│
├── src/
│   ├── config.py                ← Settings via pydantic + python-dotenv
│   ├── db.py                    ← SQLAlchemy engine + session helpers
│   ├── models.py                ← 8 SQLAlchemy ORM table models
│   │
│   ├── schemas/
│   │   ├── decision_candidate_schema.py   ← Pydantic models for AI output
│   │   ├── extraction_packet_schema.py
│   │   └── image_analysis_schema.py
│   │
│   └── services/
│       ├── pipeline_service.py        ← Orchestrates all stages
│       ├── docx_parser.py             ← Stage 2: Docling + python-docx
│       ├── image_extractor.py         ← Stage 3: Image extraction
│       ├── image_analyzer.py          ← Stage 4: Gemini Vision
│       ├── packet_builder.py          ← Stage 5: Section packet grouping
│       ├── decision_extractor.py      ← Stages 6 & 7: Extraction + validation
│       ├── review_service.py          ← Stage 8: Approve / Edit / Reject
│       ├── gemini_client.py           ← Gemini API wrapper
│       ├── storage_service.py         ← File I/O
│       └── log_service.py             ← DB event logging
│
├── tests/
│   └── test_local_pipeline.py        ← Unit tests with a fake Gemini client
│
├── scripts/
│   └── create_sample_docx.py         ← Generates a test fixture DOCX
│
├── data/                             ← SQLite database (gitignored)
└── storage/                          ← All file outputs (gitignored except .gitkeep)
```

---

## Data Model

```
documents
  ├── document_blocks         parsed typed blocks
  ├── document_images         every image occurrence (including duplicates)
  ├── image_analyses          Gemini Vision output per image
  ├── extraction_packets      heading-grouped section packets
  ├── decision_candidates     AI-generated candidates (all pending human review)
  │    └── decisions          ONLY created on explicit Approve
  │         └── decision_versions    versioned rule text + structured constraints
  │              └── decision_sources    source block IDs + image IDs
  └── processing_logs         all stage events, warnings, and errors
```

---

## Streamlit UI Tabs

| Tab | What It Shows |
|---|---|
| **Upload DOCX** | File uploader (`.docx` only), document type selector, pipeline trigger button |
| **Parsed Document Preview** | Block count, heading outline, Markdown preview, full normalized block list |
| **Extracted Images** | Every occurrence: preview image, hash, caption, nearby text, mapping quality |
| **Gemini Image Analysis** | Importance, image type, extracted logic rules, unclear parts, raw Gemini JSON |
| **Extraction Packets** | All section packets with their block and image analysis counts |
| **Decision Candidates** | Full review UI — metrics, candidate detail, source image previews, Edit form, Approve / Reject |
| **Raw JSON Viewer** | Inspect output files (JSON / JSONL) or query any database table directly |
| **Logs / Errors** | All processing log entries, filterable by level: `info` / `warning` / `error` |

---

## File Outputs

```
storage/
  uploads/<doc_id>/original.docx
  parsed/<doc_id>/parsed_document.json
  parsed/<doc_id>/preview.md
  images/<doc_id>/img_001.png  img_002.png  ...
  image_analysis/<doc_id>/image_analysis.json
  extraction_packets/<doc_id>/packets.jsonl
  decision_outputs/<doc_id>/decision_candidates.json
  decision_outputs/<doc_id>/validation_report.json

data/decidian_local.db    ← SQLite database
```

---

## Quick Start

### Prerequisites

- Python 3.11 or newer
- A [Google Gemini API key](https://ai.google.dev/gemini-api/docs/quickstart)
- A `.docx` file to process

### 1. Clone and set up

```bash
git clone https://github.com/Asgarsk01/Decidian_lab.git
cd Decidian_lab
git checkout feature/docx-only
```

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1        # Windows
# source .venv/bin/activate         # macOS / Linux

python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 2. Configure

```powershell
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

> **Model names are read only from `.env`.** If Gemini reports a model is unavailable for your account, update the corresponding value and restart Streamlit.

### 3. Run

```powershell
streamlit run app.py
```

Open the URL printed by Streamlit. Upload a `.docx`, select the document type, and click **Save and run full pipeline**.

---

## Configuration

All settings are loaded from `.env` via `src/config.py` and exposed as a cached Pydantic `Settings` object:

| Variable | Default | Description |
|---|---|---|
| `GEMINI_API_KEY` | _(required)_ | Google Gemini API key |
| `GEMINI_TEXT_MODEL` | `gemini-3.5-flash` | Model used for extraction and validation |
| `GEMINI_VISION_MODEL` | `gemini-3.5-flash` | Model used for image analysis |
| `DATABASE_URL` | `sqlite:///data/decidian_local.db` | SQLAlchemy database URL |
| `STORAGE_DIR` | `storage` | Root directory for all file outputs |

---

## Running Tests

```powershell
# Run the full test suite
python -m unittest discover -s tests -v

# Generate a sample DOCX fixture for manual testing
python scripts/create_sample_docx.py
```

The test suite:
- Builds a real `.docx` fixture in memory
- Verifies block parsing and duplicate image detection
- Builds a section packet
- Runs extraction and validation through a **deterministic fake Gemini client** (no API key needed)
- Confirms that only an explicit `approve_candidate()` call through `review_service` creates a decision row

---

## Contributing

Contributions are welcome. Here is how to get oriented quickly:

### Where things live

| Want to change… | Look at… |
|---|---|
| How documents are parsed | `src/services/docx_parser.py` |
| How images are extracted | `src/services/image_extractor.py` |
| What Gemini is asked about images | `src/services/image_analyzer.py` — `IMAGE_ANALYSIS_PROMPT` |
| How packets are built | `src/services/packet_builder.py` |
| What Gemini is asked to extract | `src/services/decision_extractor.py` — `DECISION_EXTRACTION_PROMPT` |
| The validation logic | `src/services/decision_extractor.py` — `validate_candidates()` |
| The approve / edit / reject flow | `src/services/review_service.py` |
| The Streamlit UI layout | `app.py` |
| Database tables | `src/models.py` |
| AI output schemas | `src/schemas/` |

### Guidelines

- **Keep the human-review gate intact.** `requires_human_review = True` is a schema-level invariant. Do not weaken it.
- **No silent skips.** Every image, packet, or candidate failure must produce an audit record.
- **Raw responses stay.** Every Gemini response (text + parsed + token usage) must be stored alongside the processed record.
- **Add tests for new pipeline stages.** Use the fake Gemini client pattern in `tests/test_local_pipeline.py`.
- **Don't break the `.docx`-only guard.** `StorageService.validate_docx_filename()` and the Streamlit uploader filter are both intentional. If you want to add a new format, that belongs on a separate branch.

### Development workflow

```bash
# Install in editable mode with dev dependencies if you add any
pip install -r requirements.txt

# Run tests before opening a PR
python -m unittest discover -s tests -v
```

### Branch conventions

| Branch prefix | Purpose |
|---|---|
| `feature/` | New pipeline capability |
| `fix/` | Bug fixes |
| `docs/` | Documentation-only changes |
| `refactor/` | Internal restructuring without behavior changes |

---

## Error Handling and Auditability

- A Gemini failure for a **single image** is caught, logged, and an `unreadable` record is created. Analysis continues for all other images.
- A Gemini failure for a **single packet** is caught, logged, and an empty candidate list is stored. Extraction continues for all other packets.
- `GeminiModelUnavailableError` (HTTP 404 / "model not found") is **re-raised immediately** with a message directing you to update `.env`.
- Every raw Gemini response (text + parsed output + token usage metadata) is stored in the database row it produced.
- All events, warnings, and errors are visible in the **Logs / Errors** tab.
- Re-running the pipeline for a document that already has approved decisions is not exposed — upload a new copy instead.

---

## Known Limitations

- DOCX image-position mapping can be approximate; complex floating-layout images may not map exactly to their nearest text block.
- Images in EMF/WMF format are preserved and audited but may not be previewable in the browser or accepted by Gemini Vision.
- Gemini output requires human review and can be wrong even when schema-valid.
- This pipeline does not analyze source code or pull requests.
- Only `.docx` is supported on this branch — PDF and all other formats are rejected by design.
- Multi-user authentication and authorization are not implemented.
- There is no built-in concept of decision ownership, expiry, or downstream propagation to a codebase in this branch.


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

