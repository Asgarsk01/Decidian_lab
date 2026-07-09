# Decidian Docling Lab: Project Guide

## Purpose

This repository isolates the document-ingestion stage of Decidian. Its purpose
is to measure Docling's output quality locally before parsed content is stored
in R2 or sent to an LLM for decision extraction.

Related documents:

- [`Decidian_Architecture_v2.md`](Decidian_Architecture_v2.md) contains the
  broader MVP architecture.
- [`Decidian.txt`](Decidian.txt) contains the product discussion and design
  context that preceded this laboratory.

The current flow is:

```text
Local document
    -> validation
    -> Docling conversion
    -> evidence and structured exports
    -> hybrid chunking
    -> manual quality review
```

Cloud storage, background queues, databases, and LLM calls are outside the
current scope.

## Acceptance workflow

Use a representative set of documents rather than relying only on synthetic
fixtures:

1. A clean, digitally generated PDF.
2. A DOCX containing headings, numbered sections, a table, and an image.
3. A scanned or image-only PDF.
4. A visual architecture document containing diagrams or charts.
5. A long design document that exercises chunk boundaries.

Parse normal documents with `standard`, scans with `scanned`, and diagram-heavy
documents with `visual`.

For each run, inspect and score:

- Reading order
- Heading hierarchy
- Table reconstruction
- OCR accuracy
- Picture extraction
- Source provenance
- Chunk quality

Scores use:

- `0`: broken or unusable
- `1`: partially correct and requires cleanup
- `2`: correct enough for downstream use

Record concrete issues in the Evaluation tab. For example, identify the page,
table, heading, or chunk where the failure occurs.

## Promotion criteria

The parsing stage is ready for R2 and LLM integration only when:

- Important text is present and follows the expected reading order.
- Heading hierarchy is consistent enough to provide chunk context.
- Required tables are usable in CSV or HTML form.
- Scanned documents produce readable OCR text.
- Pictures and page evidence can be traced back to their source.
- Chunk source references and page numbers are present where the source format
  provides page information.
- Every contextualized chunk remains below the configured token limit.
- Failures preserve useful diagnostics rather than silently dropping documents.

## Output contract

Each run directory is immutable and uniquely identified by:

```text
<safe-source-name>__<first-8-hash-characters>__<UTC-timestamp>
```

The downstream integration should treat:

- `manifest.json` as the run-level status and diagnostics contract.
- `document.json` as the lossless structured representation.
- `chunks.jsonl` as the initial LLM-ingestion candidate.
- `pages/`, `pictures/`, and `tables/` as review and provenance evidence.
- `evaluation.json` as human quality feedback, not automated ground truth.

Do not make downstream services depend only on rendered Markdown. Preserve the
lossless JSON and source references.

## Resource model

The current container is CPU-only and restricted to one active document.
Compose is configured for up to six CPUs and 24 GB RAM. This is intentional:
concurrency should be introduced later through multiple isolated workers, not
by parsing several large documents simultaneously inside one process.

## Next phase

After the local benchmark is accepted:

1. Upload source documents and completed run artifacts to object storage.
2. Persist document/run metadata and statuses.
3. Move parsing into an asynchronous worker.
4. Feed reviewed chunks to the decision-extraction model.
5. Preserve links from every extracted decision to chunk, source item, page,
   document hash, and parser profile.

Do not tune the LLM extraction prompt to compensate for systematic parsing
errors. Fix or explicitly document parsing limitations first.
