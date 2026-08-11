# Decidian — Docling Parsing Lab

> A Docker-first, local document parsing laboratory for inspecting how Docling processes documents before they enter the Decidian decision pipeline.

**Decidian** is a structured system for extracting and tracking important technical or business decisions made during software development — what was decided, why, who owns it, and which parts of the codebase it impacts. It acts as a single source of truth, helping developers and AI agents keep future code changes aligned with the intended design.

This repository (`main` branch) is the **parsing laboratory**: a Docker-based harness for running [Docling](https://docling-project.github.io/docling/) locally against any supported document, inspecting every artifact it produces, and optionally running a guarded Gemini review pass over diagram images — before any document enters the full Decidian decision extraction pipeline.

---

> **Repository branches**
>
> | Branch | Purpose | Stack |
> |---|---|---|
> | `main` ← you are here | Docling parsing lab — multi-format, Docker | Docker + uv + Streamlit + Typer CLI |
> | `feature/docx-only` | Full end-to-end decision extraction pipeline | Python venv + Streamlit + SQLite |
>
> The branches have significantly different architectures, dependency stacks, and purposes.
> The `feature/docx-only` branch implements the complete human-review decision pipeline scoped to `.docx` input only.

---

## Table of Contents

- [What This Lab Does](#what-this-lab-does)
- [Supported Formats](#supported-formats)
- [Parsing Profiles](#parsing-profiles)
- [Artifact Outputs](#artifact-outputs)
- [Gemini Guarded Review](#gemini-guarded-review)
- [Project Structure](#project-structure)
- [Requirements](#requirements)
- [Quick Start — Browser UI](#quick-start--browser-ui)
- [CLI Usage](#cli-usage)
- [Configuration Reference](#configuration-reference)
- [Running Tests](#running-tests)
- [Contributing](#contributing)
- [Known Limitations](#known-limitations)

---

## What This Lab Does

Given any supported document file, the lab:

1. **Validates** the input — extension, MIME type (via `python-magic`), size (max 100 MB), and internal structure for Office formats
2. **Converts** the document with Docling using a configurable parsing profile
3. **Exports** a rich set of immutable, timestamped artifacts — JSON, Markdown, text, chunks, tables, pictures, semantic integrity data, diagnostics, evaluation data
4. **Optionally reviews** qualifying diagram images with a guarded, two-pass Gemini Vision call (extraction + verification)
5. **Presents** all artifacts in a Streamlit browser UI with tabs for Summary, Markdown, JSON, chunks, visual OCR, tables, pictures, and evaluation
6. **Packages** every artifact into a downloadable ZIP on demand

Every run produces an **immutable output directory** named `<stem>__<sha256[:8]>__<utcTimestamp>/` so results are reproducible and never overwritten.

---

## Supported Formats

```
PDF    DOCX   PPTX   MD / Markdown   HTML / HTM   TXT
PNG    JPG    JPEG   TIF / TIFF      BMP          WEBP
```

Maximum input size: **100 MB**. PDF and DOCX are the primary acceptance-tested formats.

All inputs are validated by both extension and MIME signature. A file whose content does not match its extension is rejected before any parsing begins.

---

## Parsing Profiles

Select a profile based on your document type:

| Profile | When to use | Key behaviours |
|---|---|---|
| `standard` | Born-digital PDFs, DOCX, PPTX, text | OCR on, accurate table extraction, heading hierarchy |
| `scanned` | Scanned or photographically captured PDFs | Force full-page OCR across every page |
| `visual` | Diagrams, charts, architecture documents | Picture classification + chart extraction enabled |

Profiles are exposed in both the browser UI dropdown and the CLI `--profile` option.

---

## Artifact Outputs

Each run creates a timestamped directory under `output/`. Inside:

```
output/<stem>__<hash>__<timestamp>/
  manifest.json              ← run metadata, counts, timings, status
  document.json              ← full Docling JSON document model
  canonical.json             ← canonical structured representation
  canonical.md               ← clean Markdown for LLM consumption
  document.md                ← raw Docling Markdown export
  document.txt               ← plain text extraction
  chunks.jsonl               ← token-bounded chunks (for RAG/LLM input)
  clean_chunks.jsonl         ← filtered, higher-quality chunk set
  review_queue.jsonl         ← chunks flagged for human review
  semantic_integrity.json    ← semantic integrity findings and repairs
  visual_integrity.json      ← picture/diagram integrity findings
  evaluation.jsonl           ← structured evaluation records
  diagnostics.json           ← parser diagnostics and warnings
  tables/                    ← one JSON file per extracted table
  pictures/                  ← picture evidence images
  assets/                    ← Docling-generated image assets
  ai_evidence/               ← images actually submitted to Gemini (if AI review ran)
  gemini_review.json         ← structured Gemini extraction output
  gemini_events.jsonl        ← append-only Gemini audit log
```

`manifest.json` records the full provenance: source file SHA-256, Docling version, torch version, OCR engine, profile settings, stage timings, element and chunk counts, AI review status, and semantic/visual integrity summary.

---

## Gemini Guarded Review

When `DECIDIAN_AI_REVIEW=true` is set and a valid `GEMINI_API_KEY` is provided, the lab runs a **two-pass targeted Gemini Vision review** over qualifying diagram images (skipping decorative, logo, or simple raster images).

### How it works

**Pass 1 — Extraction:** Selected diagram images are sent to Gemini with a bounded extraction schema targeting the highest-value labels, components, relationships, and decision-relevant claims. Extremely wide diagrams are tiled; complex ERDs get the full image plus two readable evidence tiles.

**Pass 2 — Verification:** The verifier is invoked only when the extraction produced evidence-bearing claims and the verification allowance has not been exhausted.

### Guards

Every Gemini request is checked against multiple guards **before** it is sent:

| Guard | What it checks |
|---|---|
| Request count | `GEMINI_MAX_REQUESTS_PER_RUN` |
| Token budget | `GEMINI_MAX_TOTAL_TOKENS` |
| Runtime | `GEMINI_MAX_RUNTIME_SECONDS` |
| Cost estimate | `GEMINI_BUDGET_INR` with `GEMINI_REQUEST_RESERVE_INR` buffer |
| Concurrency | `GEMINI_MAX_CONCURRENCY` |

Authentication failures, model errors, billing errors, and schema validation failures open a **circuit breaker** so the same systemic failure is not repeated for every diagram.

### Cost tracking

Usage and spend are recorded as soon as Gemini returns response metadata — before JSON parsing or schema validation. Incomplete, truncated, or malformed paid responses still increase dashboard totals and retain a redacted raw-output audit entry. The rupee total is an operator estimate calculated from reported input, visible-output, and thought-token counts using configurable model prices and a USD-to-INR rate. Provider billing remains authoritative.

### Live dashboard

When AI review is enabled, the Streamlit UI shows a live dashboard with:
- Selected vs detected diagrams
- Current candidate and pass
- API attempts and verification calls
- Token count (including thought tokens)
- Elapsed time and estimated spend
- Remaining budget and guard utilization
- Event-by-event activity table

The same append-only audit is written to `gemini_events.jsonl` and can reconstruct the dashboard after a Streamlit rerun or server restart.

---

## Project Structure

```
Decidian_lab/                      ← repo root
├── Dockerfile                     ← single-stage runtime image (python:3.11-slim-bookworm)
├── compose.yaml                   ← Docker Compose service definition
├── pyproject.toml                 ← project metadata, pinned dependencies (uv)
├── uv.lock                        ← fully resolved lock file
├── .env.example                   ← all configurable environment variables
│
├── src/decidian_docling/
│   ├── __init__.py                ← package version
│   ├── models.py                  ← ParsingProfile, ProfileSettings, RunResult, RunStatus
│   ├── profiles.py                ← profile → Docling pipeline options
│   ├── validation.py              ← file validation: extension, MIME, size, Office zip check
│   ├── parser.py                  ← orchestrates conversion + all artifact stages
│   ├── postprocess.py             ← Markdown normalization, picture text injection
│   ├── canonical.py               ← canonical document model + canonical Markdown
│   ├── chunking.py                ← token-bounded chunking (RAG/LLM chunks)
│   ├── clean_chunking.py          ← filtered high-quality chunk set + review queue
│   ├── artifacts.py               ← artifact inventory helpers
│   ├── semantic_integrity.py      ← semantic + visual integrity analysis
│   ├── gemini_review.py           ← two-pass guarded Gemini Vision review
│   ├── config.py                  ← GeminiSettings from environment
│   ├── cli.py                     ← Typer CLI (parse one file or batch)
│   └── ui.py                      ← Streamlit browser UI
│
├── input/                         ← mount point for documents (Docker volume)
├── output/                        ← immutable run directories are written here
├── work/                          ← Docling working directory
├── Document/                      ← sample documents for testing
└── tests/                         ← pytest test suite
```

---

## Requirements

- [Docker Desktop](https://www.docker.com/products/docker-desktop/) with the Linux container engine
- Git (for cloning)

**Recommended Docker resources:**

| Resource | Minimum |
|---|---|
| CPUs | 6 |
| RAM | 24 GB |
| Free disk | 15 GB (image + model cache + outputs) |

The Docker Compose service has a 6-CPU and 24 GB memory limit configured. Docker Desktop must be allocated at least these resources.

> No local Python installation is required. All dependencies are baked into the Docker image using `uv` and a pinned lock file.

---

## Quick Start — Browser UI

### 1. Clone the repository

```bash
git clone https://github.com/Asgarsk01/Decidian_lab.git
cd Decidian_lab
```

All commands below must be run from the repository root where `compose.yaml` is located.

### 2. Configure (optional — required only for Gemini review)

```bash
cp .env.example .env
```

Edit `.env` and add your key:

```dotenv
GEMINI_API_KEY=your_real_key_here
DECIDIAN_AI_REVIEW=false          # set to true to enable Gemini review
GEMINI_MODEL=gemini-2.0-flash
```

`.env` is gitignored and never included in run ZIPs or manifests.

### 3. Confirm Docker is running

```powershell
docker version
docker compose version
```

`docker version` must show both Client and Server sections. If the Server section is missing, start Docker Desktop and wait for the Linux engine.

### 4. Build and start

```powershell
docker compose up --build -d docling-lab
```

The first build installs all pinned dependencies and bakes RapidOCR weights into the image. The first document conversion may download additional Docling and tokenizer model weights into the persistent `docling-models` Docker volume. Later runs reuse the cache.

### 5. Check health

```powershell
docker compose ps
docker compose logs --tail 50 docling-lab
```

The service should show `healthy`. Open: **[http://localhost:8501](http://localhost:8501)**

### 6. Parse a document

1. Select `standard`, `scanned`, or `visual`
2. Upload a document (see [Supported Formats](#supported-formats))
3. Click **Parse document**
4. Inspect the **Summary, Markdown, JSON, Core chunks, Visual OCR, Tables, Pictures**, and **Evaluation** tabs
5. Click **Prepare complete output ZIP** to download all artifacts for that run

### 7. Stop

```powershell
docker compose down
```

This retains the model cache. Use `docker compose down -v` only if you intentionally want to delete downloaded model weights.

---

## CLI Usage

The CLI is available inside the running container or as a standalone install:

```bash
# Parse a single file
docker compose exec docling-lab decidian-docling parse /data/input/my_document.pdf --profile standard

# Parse with Gemini review enabled
docker compose exec docling-lab decidian-docling parse /data/input/diagram.docx --profile visual --ai-review

# Parse a batch (all files in a directory)
docker compose exec docling-lab decidian-docling batch /data/input/ --profile standard --output /data/output/

# Show all options
docker compose exec docling-lab decidian-docling --help
```

Documents in `./input/` are mounted as `/data/input/` inside the container. Results appear in `./output/` on your host machine.

---

## Configuration Reference

All settings are read from environment variables (`.env` via Docker Compose):

### Core

| Variable | Default | Description |
|---|---|---|
| `DECIDIAN_INPUT_DIR` | `/data/input` | Input directory inside container |
| `DECIDIAN_OUTPUT_DIR` | `/data/output` | Root directory for run outputs |

### Gemini

| Variable | Default | Description |
|---|---|---|
| `GEMINI_API_KEY` | _(empty)_ | Google Gemini API key |
| `DECIDIAN_AI_REVIEW` | `false` | Enable/disable Gemini guarded review |
| `GEMINI_MODEL` | `gemini-3.5-flash` | Model for extraction and verification |
| `GEMINI_THINKING_LEVEL` | `medium` | Gemini thinking level |
| `GEMINI_BUDGET_INR` | `100` | Max estimated spend per run (₹) |
| `GEMINI_REQUEST_RESERVE_INR` | `20` | Reserve buffer before budget hard stop |
| `GEMINI_TIMEOUT_SECONDS` | `45` | Per-request timeout |
| `GEMINI_MAX_RETRIES` | `1` | Retries per failed request |
| `GEMINI_MAX_CONCURRENCY` | `1` | Concurrent Gemini requests |
| `GEMINI_MAX_CANDIDATES` | `5` | Max diagram images selected per run |
| `GEMINI_MAX_REQUESTS_PER_RUN` | `7` | Total API request attempts per run |
| `GEMINI_MAX_RUNTIME_SECONDS` | `300` | Max AI wall time per run |
| `GEMINI_MAX_TOTAL_TOKENS` | `60000` | Total token budget across all requests |
| `GEMINI_MAX_OUTPUT_TOKENS_PER_REQUEST` | `8192` | Per-request output token limit |
| `GEMINI_MAX_VERIFICATIONS` | `2` | Max verification calls per run |
| `GEMINI_USD_INR_RATE` | `100` | USD to INR conversion rate |
| `GEMINI_INPUT_PRICE_USD_PER_MILLION` | `1.5` | Input token price (USD per 1M) |
| `GEMINI_OUTPUT_PRICE_USD_PER_MILLION` | `9` | Output token price (USD per 1M) |

---

## Running Tests

```bash
docker compose exec docling-lab pytest tests/ -v
```

Or locally if you have `uv` installed:

```bash
uv sync
uv run pytest tests/ -v
```

---

## Contributing

Contributions are welcome. Here is how to get oriented:

### Where things live

| Want to change… | Look at… |
|---|---|
| Supported file types | `src/decidian_docling/validation.py` — `ALLOWED_EXTENSIONS` |
| Parsing profile settings | `src/decidian_docling/profiles.py` |
| The conversion and artifact pipeline | `src/decidian_docling/parser.py` |
| Docling post-processing and Markdown cleanup | `src/decidian_docling/postprocess.py` |
| Canonical document model | `src/decidian_docling/canonical.py` |
| Token-bounded chunking | `src/decidian_docling/chunking.py` |
| Clean chunk filtering | `src/decidian_docling/clean_chunking.py` |
| Gemini guarded review logic | `src/decidian_docling/gemini_review.py` |
| Gemini configuration and guards | `src/decidian_docling/config.py` |
| Semantic and visual integrity | `src/decidian_docling/semantic_integrity.py` |
| Streamlit browser UI | `src/decidian_docling/ui.py` |
| Typer CLI | `src/decidian_docling/cli.py` |
| Data models | `src/decidian_docling/models.py` |

### Guidelines

- **Immutable run directories.** Each run must produce a new timestamped directory. Never overwrite existing output.
- **Provenance is non-negotiable.** `manifest.json` must record the full source file SHA-256, all package versions, profile settings, stage timings, and artifact list.
- **Guard the Gemini spend.** Never remove or weaken the pre-request guards. The circuit breaker must remain active.
- **Raw Gemini audit stays.** Every Gemini response (text + metadata) must be written to `gemini_events.jsonl` before parsing, even if the response is malformed.
- **Observer failures must not affect parsing.** The `_notify_progress` callback wraps all UI update calls in a try/except so a Streamlit failure never corrupts artifact output.
- **Add tests for new artifact stages.** The test suite must not require a Gemini API key.

### Development workflow

```bash
# Install dependencies locally with uv
uv sync

# Run tests
uv run pytest tests/ -v

# Build the Docker image
docker compose build docling-lab

# Run a quick parse to validate changes
docker compose up -d docling-lab
docker compose exec docling-lab decidian-docling parse /data/input/sample.pdf
```

### Branch conventions

| Prefix | Purpose |
|---|---|
| `feature/` | New capability |
| `fix/` | Bug fix |
| `docs/` | Documentation only |
| `refactor/` | Internal restructuring without behavior change |

---

## Known Limitations

- Requires Docker Desktop with at least 24 GB RAM allocated — Docling's deep learning models are memory-intensive.
- The first run may take several minutes to download model weights (cached in a Docker volume after first use).
- Gemini cost estimates are calculated from reported token usage; provider billing is authoritative and may differ.
- One in-flight Gemini response can settle above the estimated budget; the `GEMINI_REQUEST_RESERVE_INR` buffer reduces this risk but does not eliminate it.
- HTML and page-preview tabs in the Streamlit UI are intentionally empty — those large debug artifacts are not produced by the extraction pipeline.
- This lab does not persist documents between sessions; each run is independent and immutable.
- Multi-user authentication and authorization are not implemented.
