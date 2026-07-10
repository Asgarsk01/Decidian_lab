# Extraction Artifact Output Report

## Current Design

The Docling laboratory now produces one extraction-focused output set. There is
no artifact-mode selector in the browser UI, no `--artifact-mode` CLI option,
and no full audit package code path.

Parsing quality is unchanged. OCR, accurate table extraction, heading cleanup,
picture-text enrichment, and chunking run locally. Non-essential exports are
removed while derived extraction cleanup protects code comments from becoming
headings, repairs narrow wrapped table headers, and adds provenance-labelled
picture-text chunks.

## Output Created for Every Run

```text
output/<safe-name>__<hash8>__<timestamp>/
  manifest.json
  evaluation.json

  document.json
  document.raw.md
  document.md
  document.txt

  chunks.jsonl
  picture_text.jsonl

  assets/                       # retained picture assets only
  pictures/                     # diagram/image audit evidence
  tables/                       # CSV and HTML table data
  repaired_table_evidence/      # only when table repair happened
```

`document.md` is the future decision-extraction feed. It contains cleaned
structure, repaired tables, protected source-code literals, and merged picture
text. `document.json` remains the structured ground truth and
`picture_text.jsonl` records the source and trust level of diagram-derived text.
`chunks.jsonl` includes both normal document chunks and picture-text supplement
chunks, which retain page, picture, provenance, and trust metadata.

## Files Deliberately Not Created

```text
pages/
assets/page_*.png
document.html
document_preview.html
normal unrepaired table PNGs
result.zip
```

Generated page-preview assets are removed after all extraction artifacts have
been created. This preserves picture PNGs and Markdown picture links while
eliminating large per-page preview files. The VECV 127-page test showed these
previews alone accounted for about 43 MB.

## Timing and Evidence Rules

`manifest.json` records harness stage timings and Docling native pipeline
timings when available. Skipped exports are marked with `ran: false` and a
reason, rather than being reported as zero seconds.

Picture PNGs stay because they are the visual evidence for diagram text merged
into `document.md`. Table-fragment images stay only when a continued-table
repair happens, allowing the repaired row to be audited against its original
fragments.

## How To Run

```powershell
docker compose up --build -d docling-lab
```

Open [http://localhost:8501](http://localhost:8501), choose a parsing profile,
upload a file, and select **Parse document**. The result directory is shown in
the Summary tab.

For CLI use:

```powershell
docker compose run --rm cli parse /data/input/example.pdf --profile standard
```

There is no Full mode. The UI offers an on-demand **Prepare complete output
ZIP** button, which packages every generated artifact in memory for browser
download without saving a ZIP inside `output/`.
