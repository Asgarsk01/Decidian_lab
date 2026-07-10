# Artifact Mode Implementation Report

## Summary

We added an artifact-mode layer to the local Docling harness so Decidian can
produce either a full audit package or a smaller extraction package without
changing parsing quality.

The central rule is:

```text
Full mode and extraction mode must use the same parse settings.
Only export artifacts may differ.
```

That means OCR, table extraction, cell matching, heading hierarchy, image scale,
picture text extraction, and Markdown cleanup remain identical between modes.

## What Changed

### Artifact Modes

The app now supports two artifact modes:

```text
full
extraction
```

`full` is the default and keeps the old behavior.

`extraction` skips expensive debug artifacts while keeping the extraction feed
intact.

### Stage Timings

`manifest.json` now includes a `stage_timings` object. Each stage records
whether it ran and how long it took. Skipped extraction-mode stages are recorded
with a `skipped_reason`, not fake zero-second timings.

Example:

```json
{
  "stage_timings": {
    "docling_conversion": {
      "ran": true,
      "seconds": 123.45,
      "native_timings_available": true
    },
    "page_image_export": {
      "ran": false,
      "skipped_reason": "artifact_mode=extraction"
    },
    "archive_zip": {
      "ran": false,
      "skipped_reason": "artifact_mode=extraction"
    }
  }
}
```

Docling's native pipeline profiling is enabled around conversion. If Docling
emits internal timings, they are stored under `conversion.timings`.

### Repaired Table Evidence

Continued-table repair now records structured repair metadata. When a table is
stitched across pages, the harness can export pre-merge source fragment images
under:

```text
repaired_table_evidence/
```

This avoids checking the parser against its own stitched result. The evidence
shows the original fragments that caused the repair.

### Parity Check

A new CLI command compares the extraction feed files between two run folders:

```powershell
docker compose run --rm cli compare `
  /data/output/full-run-dir `
  /data/output/extraction-run-dir
```

It checks these files byte-for-byte:

```text
document.md
document.json
picture_text.jsonl
```

Those files are the core safety gate. If they differ between full and
extraction mode for the same input and profile, extraction mode is touching
parse behavior and should not be trusted until investigated.

## Output After Implementation

### Full Mode Output

Full mode is for audit, debugging, and external verification.

```text
output/<safe-name>__<hash8>__<timestamp>/
  manifest.json
  evaluation.json

  document.json
  document.raw.md
  document.md
  document.txt
  document.html
  document_preview.html

  chunks.jsonl
  picture_text.jsonl

  assets/

  pages/
    page-0001.png
    page-0002.png
    ...

  pictures/
    picture-0001.png
    picture-0002.png
    ...

  tables/
    table-0001.png
    table-0001.csv
    table-0001.html
    ...

  repaired_table_evidence/
    repair-0001/
      table-fragment-0012.png
      table-fragment-0013.png
      metadata.json

  result.zip
```

`repaired_table_evidence/` appears only when a continued-table repair happened.

### Extraction Mode Output

Extraction mode is for future Decidian decision extraction.

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

  assets/

  pictures/
    picture-0001.png
    picture-0002.png
    ...

  tables/
    table-0001.csv
    table-0001.html
    ...

  repaired_table_evidence/
    repair-0001/
      table-fragment-0012.png
      table-fragment-0013.png
      metadata.json
```

Extraction mode skips:

```text
pages/
document.html
document_preview.html
normal unrepaired table PNGs
automatic result.zip
```

It keeps picture PNGs because diagram-derived text is now merged into
`document.md`, and the cropped picture remains the cheapest audit evidence for
that text.

## How To Run

Full mode:

```powershell
docker compose run --rm cli parse /data/input/example.pdf `
  --profile standard `
  --artifact-mode full
```

Extraction mode:

```powershell
docker compose run --rm cli parse /data/input/example.pdf `
  --profile standard `
  --artifact-mode extraction
```

Compare two runs:

```powershell
docker compose run --rm cli compare `
  /data/output/full-run-dir `
  /data/output/extraction-run-dir
```

## Expected Timing Impact

This change reduces export, image-write, preview, and ZIP overhead. It does not
skip Docling's CPU conversion, OCR, accurate table extraction, heading analysis,
picture text extraction, or chunking.

For large PDFs, the likely improvement depends on the timing split:

```text
If Docling conversion dominates: modest improvement.
If artifact export and ZIP dominate: larger improvement.
```

The new `stage_timings` data is the source of truth for future optimization.

## Safety Rules

1. `extraction` mode must not weaken parse settings.
2. `image_scale` stays parse-affecting, not an export-only speed toggle.
3. Picture PNGs stay because they verify diagram text that enters `document.md`.
4. Normal table PNGs can be skipped, but repaired table source evidence stays.
5. `document.md`, `document.json`, and `picture_text.jsonl` must match between
   full and extraction mode for the same input/profile.
