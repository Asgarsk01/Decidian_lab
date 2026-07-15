# Decidian Docling Lab

A Docker-first, CPU-only laboratory for testing how Docling parses documents
before Decidian adds cloud storage, queues, a database, or final LLM decision extraction.

The project provides:

- A Streamlit browser interface for uploading and inspecting one document.
- A Typer CLI for repeatable single-file and batch parsing.
- Shared validation and parsing logic used by both interfaces.
- Standard, scanned-document, and visual-document parsing profiles.
- One lean extraction artifact set containing only the files needed for
  inspection and future LLM decision extraction.
- Immutable local output folders with JSON, Markdown, text, chunks, picture
  evidence, table data, diagnostics, and evaluation data.

Docling conversion remains local. When Gemini defence is enabled and
`GEMINI_API_KEY` is configured, only qualifying diagram images and targeted
DOCX/PDF ambiguity evidence are sent to the Google Gemini API for guarded,
targeted review. Raw documents are not uploaded wholesale. No content is sent
to R2, a database, or another storage service.

## Gemini defence configuration

Copy `.env.example` to `.env` (a blank local `.env` is created for development)
and add the API key locally:

```dotenv
GEMINI_API_KEY=
GEMINI_MODEL=gemini-3.5-flash
DECIDIAN_AI_REVIEW=false
GEMINI_THINKING_LEVEL=medium
GEMINI_BUDGET_INR=100
GEMINI_TIMEOUT_SECONDS=45
GEMINI_MAX_RETRIES=1
GEMINI_MAX_CONCURRENCY=1
GEMINI_MAX_CANDIDATES=5
GEMINI_MAX_REQUESTS_PER_RUN=7
GEMINI_MAX_RUNTIME_SECONDS=300
GEMINI_MAX_TOTAL_TOKENS=60000
GEMINI_MAX_OUTPUT_TOKENS_PER_REQUEST=8192
GEMINI_MAX_VERIFICATIONS=2
GEMINI_REQUEST_RESERVE_INR=20
GEMINI_USD_INR_RATE=100
GEMINI_INPUT_PRICE_USD_PER_MILLION=1.5
GEMINI_OUTPUT_PRICE_USD_PER_MILLION=9
```

`.env` is ignored by Git and never included in run ZIPs or manifests. The
Streamlit UI and CLI allow Gemini review to be disabled for a parse. If the key
is absent or the API fails, local Docling artifacts still complete and all
unresolved evidence is excluded from the clean feed.

Gemini model interactions use the Developer API `v1beta` Interactions endpoint.
The default guarded profile uses Gemini 3.5 Flash with medium thinking, one
request at a time, at most five selected candidates, seven total request
attempts, two verification calls, 60,000 total reported tokens, an 8,192-token
response allowance, five minutes of AI wall time, and an estimated ₹100 run budget. Before each request the client
checks the request, token, runtime, budget, and ₹20 reserve guards. The verifier
is invoked only when extraction produced evidence-bearing claims and the
verification allowance remains. Authentication, model, billing, quota, or
shared response-schema failures open a circuit breaker so the same systemic
failure is not repeated for every diagram.

The rupee total is an operator estimate calculated from reported input,
visible-output, and thought-token usage with configurable model prices and USD
to INR rate. Provider billing remains authoritative, and one in-flight response
can settle above the estimate; the request reserve reduces that risk.

Usage and spend are recorded as soon as Gemini returns response metadata,
before JSON parsing or schema validation. Consequently, incomplete, truncated,
or malformed paid responses still increase the dashboard totals and retain a
redacted raw-output audit. Provider `incomplete` status, schema rejection, API
failure, retries, and guard stops are separate journal events.

When AI is enabled, the UI requires explicit approval of the displayed run
budget and shows a live dashboard with selected versus detected diagrams,
current candidate and pass, API attempts, verification calls, tokens, thought
tokens, diagram API passes (including retries and verification), elapsed time,
estimated spend, remaining budget, guard utilization, and an event-by-event
activity table. The same append-only audit is written to
`gemini_events.jsonl`.

Dashboard state is retained in the Streamlit session and can be reconstructed
from `gemini_events.jsonl` after a rerun or server restart. Evidence is labelled
separately as source, selected/prepared, and actually submitted. Only selected
evidence is copied into `ai_evidence/`; request events record the exact files
sent. Extremely wide diagrams are tiled, while complex ERDs retain the full
image plus two readable evidence tiles. The extraction schema is deliberately
bounded to the highest-value labels, components, relationships, and claims.

## Requirements

Install and start:

1. [Docker Desktop](https://www.docker.com/products/docker-desktop/).
2. Docker Desktop's Linux container engine.
3. Git, if you are cloning the project.

Recommended Docker resources:

- 6 CPUs
- 24 GB RAM
- At least 15 GB of free disk space for the image, model cache, and outputs

The Compose service has a six-CPU and 24 GB memory limit. Docker Desktop must
also be configured with enough resources for those limits to be available.

## Clone the repository

```powershell
git clone https://github.com/Asgarsk01/Decidian_lab.git
cd Decidian_lab
```

All commands below must be run from the repository root, where `compose.yaml`
is located.

## Quick start: browser UI

### 1. Confirm Docker is running

```powershell
docker version
docker compose version
```

`docker version` must show both Client and Server sections. If the Server
section is missing, start Docker Desktop and wait for the Linux engine.

### 2. Build and start the application

```powershell
docker compose up --build -d docling-lab
```

The first build installs the pinned Python dependencies and bakes RapidOCR
weights into the image. The first document conversion may download additional
Docling and tokenizer models into the persistent `docling-models` Docker
volume. Later runs reuse that cache.

### 3. Check application health

```powershell
docker compose ps
docker compose logs --tail 50 docling-lab
```

The service should show `healthy`. Open:

**[http://localhost:8501](http://localhost:8501)**

### 4. Parse a document

1. Select `standard`, `scanned`, or `visual`.
2. Upload one supported local document.
3. Select **Parse document**.
4. Inspect the Summary, Markdown, JSON, Core chunks, Visual OCR, Tables,
   Pictures, and Evaluation tabs. HTML and page-preview tabs are intentionally empty because
   those large debug artifacts are not produced.
5. Record quality scores if required.
6. Select **Prepare complete output ZIP**, then download the generated ZIP. It
   contains every artifact produced for that run and is created only on demand.

Supported extensions:

```text
PDF, DOCX, PPTX, MD, Markdown, HTML, HTM, TXT,
PNG, JPG, JPEG, TIF, TIFF, BMP, WEBP
```

The maximum input size is 100 MB. PDF and DOCX are the main acceptance-tested
formats.

### 5. Stop the application

```powershell
docker compose down
```

This keeps the model cache. Do not use `docker compose down -v` unless you
intentionally want to delete downloaded models.

## Parsing profiles

### `standard`

- Automatic OCR without forced full-page OCR
- Accurate table extraction with cell matching
- Heading hierarchy detection
- Page and picture evidence images at 2x scale
- No VLM or remote enrichment

Use this for normal text PDFs, DOCX files, and digitally generated documents.

### `scanned`

Includes all standard settings and forces full-page OCR.

Use this for scanned PDFs or image-only pages. It is slower than `standard`.

### `visual`

Includes the standard settings and enables picture classification and chart
extraction. Remote services, external plugins, picture descriptions, formula
enrichment, and code enrichment remain disabled.

Use this for architecture documents, reports, charts, and image-heavy files.

## Extraction artifacts

Every run produces the same lean extraction artifact set. Parsing settings are
unchanged: OCR, table mode, cell matching, heading hierarchy, image scale, and
picture-text extraction remain enabled.

- Keeps `document.md`, `document.json`, `document.raw.md`, `document.txt`,
  `chunks.jsonl`, `picture_chunks.jsonl`, `picture_text.jsonl`,
  `semantic_integrity.json`, `visual_integrity.json`, `manifest.json`,
  `canonical_document.json`, `clean_chunks.jsonl`, `review_queue.jsonl`,
  `gemini_review.json`, `evaluation.json`, `pictures/`, targeted AI evidence,
  table CSV, and table HTML.
- Skips page PNGs, generated page-preview assets, normal table PNGs,
  `document.html`, `document_preview.html`, and persistent `result.zip` files.
- Keeps table-fragment evidence only when a continued table was repaired.

## CLI usage

The repository maps the host `input/` directory to `/data/input` in the
container and `output/` to `/data/output`.

Copy a document into `input/`, then parse it:

```powershell
docker compose run --rm cli parse /data/input/example.pdf --profile standard
```

Choose a specific output directory:

```powershell
docker compose run --rm cli parse /data/input/example.pdf `
  --profile scanned `
  --output /data/output/scanned-tests
```

Parse every supported document in `input/` sequentially:

```powershell
docker compose run --rm cli batch /data/input `
  --profile standard `
  --output /data/output/batch-tests
```

Show command help:

```powershell
docker compose run --rm cli --help
docker compose run --rm cli parse --help
docker compose run --rm cli batch --help
```

Only one document is parsed at a time. Validation and conversion failures
return a nonzero exit code while preserving diagnostic output where possible.

## Output structure

Every parse creates a new immutable directory:

```text
output/<safe-name>__<hash8>__<UTC-timestamp>/
```

Typical contents:

```text
manifest.json
document.json
document.md
document.raw.md
document.txt
chunks.jsonl
picture_chunks.jsonl
picture_text.jsonl
semantic_integrity.json
visual_integrity.json
canonical_document.json
clean_chunks.jsonl
review_queue.jsonl
gemini_review.json
evaluation.json
assets/
pictures/
tables/
repaired_table_evidence/
```

- `manifest.json` records the source hash, parsing profile, package/model
  versions, artifact mode, status, timings, warnings, errors, counts, and
  artifact inventory.
- `stage_timings` inside `manifest.json` records harness stages with `ran`,
  `seconds`, and `skipped_reason` fields. Docling native pipeline timings are
  enabled during conversion and stored under `conversion.timings` when Docling
  emits them.
- `document.json` is the lossless Docling representation.
- `document.raw.md` is Docling Markdown with only basic entity cleanup.
- `document.md` is the cleaned Markdown intended for review and future LLM
  extraction. PDF cleanup normalizes numbered and unnumbered heading depth,
  removes repeated page furniture using page provenance, conservatively repairs
  explicit continued-table rows and one-letter wrapped table headers, protects
  source-code comments from becoming headings, and restores structured text
  found inside picture regions.
- `chunks.jsonl` is the **core** HybridChunker feed: document text and tables
  only. Its readiness is controlled by structural/table integrity findings.
- `picture_chunks.jsonl` is a separate visual-only feed containing accepted
  picture-text chunks and standalone warnings for uncovered qualifying images.
  It is never appended to `chunks.jsonl`, so low-trust visual OCR cannot block a
  clean core text/table feed.
- `picture_text.jsonl` records the best available text for exported picture
  regions. Records with `source: docling_structured` preserve Docling child-item
  text and provenance and are labelled medium trust in Markdown. Tesseract runs
  only as a fallback and produces `source: tesseract_ocr`, labelled low trust.
  A quality filter rejects sparse/repetitive OCR residue before it is injected
  into Markdown or emitted as a visual chunk.
- `semantic_integrity.json` describes the core document/table gate;
  `visual_integrity.json` describes independent visual OCR and coverage risk.
- `canonical_document.json` is the typed DOCX/PDF representation used to
  distinguish headings, prose, lists, tables, code/configuration, and pictures.
- `clean_chunks.jsonl` is the only downstream decision-LLM feed. It contains
  deterministic verified blocks plus claims accepted by both Gemini passes.
- `review_queue.jsonl` contains every unresolved or excluded candidate with its
  source evidence path/hash and recommended human action.
- `gemini_review.json` records schema-validated extraction and independent
  verification results, timings, usage, prompt/schema versions, and evidence
  hashes. It never records the API key.
- `gemini_events.jsonl` is the append-only operational journal behind the live
  dashboard. It records candidate selection, request/pass starts and finishes,
  reported token usage, estimated spend, retries, guard stops, and completion.
- Native DOCX paragraph/table references use stable `docx://...` identifiers;
  aligned Docling `#/texts/...` and `#/tables/...` references are retained
  alongside them when available.
- `clean_readiness` is `ready`, `partial_ready`, or `blocked`. A partial feed is
  safe to ingest as a verified subset but is not a complete-document result.
- `evaluation.json` begins as `pending` and is updated when UI scores are saved.
- `repaired_table_evidence/` contains pre-merge table fragment images and
  metadata only when continued-table repair happened.
- The UI can create an in-memory ZIP download containing every generated run
  artifact. It is not saved inside `output/`.

Hybrid chunking uses `sentence-transformers/all-MiniLM-L6-v2`, peer merging,
repeated table headers, and a strict maximum of 1,200 measured tokens.

Rerunning the same input creates a separate timestamped directory and never
overwrites an earlier result.

## Run the test suite

Build the dedicated test image:

```powershell
docker build --target test -t decidian-docling-test .
```

Run unit tests and real Docling integration conversions:

```powershell
docker run --rm `
  -v decidian_docling-models:/models `
  decidian-docling-test
```

The integration suite generates synthetic:

- Text PDF with headings, lists, a table, and an image
- DOCX with structured content
- Scanned-image PDF requiring OCR

The tests verify output files, artifact generation, source traceability,
manifest validity, output isolation, and the 1,200-token chunk limit.

## Optional host-Python development

Docker is the supported runtime. For editor assistance or quick unit tests on
Windows, Python 3.11 is required:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install uv
uv sync --frozen --extra dev
pytest -m "not integration"
```

Host-based PDF parsing can encounter Windows model-cache symlink restrictions.
Use Docker for real conversion testing.

## Troubleshooting

### Docker Server is unavailable

Start Docker Desktop and wait until the engine is ready:

```powershell
docker version
```

### Port 8501 is already in use

Stop the other service, or change the host-side port in `compose.yaml`:

```yaml
ports:
  - "8502:8501"
```

Then open `http://localhost:8502`.

### View live parsing logs

```powershell
docker compose logs -f docling-lab
```

Press `Ctrl+C` to stop following logs; this does not stop the container.

### Rebuild after dependency changes

```powershell
docker compose build --no-cache
docker compose up -d --force-recreate docling-lab
```

### Clear only generated outputs

Delete the contents below `output/`, but keep `output/.gitkeep`. Outputs are
ignored by Git.

### Clear the model cache

This forces model downloads on the next parse:

```powershell
docker compose down -v
```

### Check disk usage

```powershell
docker system df
docker volume inspect decidian_docling-models
```

## Project layout

```text
Decidian_lab/
├── compose.yaml
├── Dockerfile
├── pyproject.toml
├── uv.lock
├── README.md
├── Document/
│   ├── Decidian.txt
│   ├── Decidian_Architecture_v2.md
│   └── PROJECT_GUIDE.md
├── src/decidian_docling/
│   ├── artifacts.py
│   ├── chunking.py
│   ├── cli.py
│   ├── models.py
│   ├── parser.py
│   ├── profiles.py
│   ├── ui.py
│   └── validation.py
├── tests/
├── input/
├── output/
└── work/
```

Additional implementation and evaluation notes are in
[`Document/PROJECT_GUIDE.md`](Document/PROJECT_GUIDE.md).
The broader product history and planned system architecture are in
[`Document/Decidian.txt`](Document/Decidian.txt) and
[`Document/Decidian_Architecture_v2.md`](Document/Decidian_Architecture_v2.md).

## Current boundary

This laboratory intentionally does not include:

- R2, S3, or GCP object storage
- API server or FastAPI
- Redis, BullMQ, or another queue
- Postgres or another database
- LLM decision extraction

Those integrations should be added only after representative documents have
been evaluated and the parsing profiles are supported by measured results.
