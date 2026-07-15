# Gemini Defence and Canonical Parsing Implementation

## Outcome

The Decidian Docling Lab now uses a defence-in-depth parsing architecture for
DOCX and PDF:

1. Docling preserves the baseline extraction and raw audit artifacts.
2. A deterministic canonical layer classifies structural blocks.
3. An atomic chunker produces a verified downstream feed.
4. Targeted Gemini extraction and independent verification can resolve diagram
   and structural ambiguity.
5. Unresolved content is excluded and retained in a review queue.

The API key is read only from the local environment. It is never written to a
manifest, artifact, log, cache key, or download ZIP.

## Canonical DOCX/PDF layer

`canonical_document.json` contains ordered typed blocks with section paths,
source references, provenance, integrity status, and ambiguity reasons.

For DOCX, native OOXML is inspected in addition to Docling. Word styles,
outline levels, numbering, table boundaries, indentation, and monospace/code
signals are used to decide structure. Numbered validation and database-index
records are demoted from false headings. Numeric heading depth normalizes real
headings such as `7.6.2`. Consecutive Docker, Kubernetes, YAML, shell, and
pipeline lines become fenced code blocks, so comments such as
`# Application Service` remain literal code rather than H1 headings.

For PDF, Docling body/group references are flattened in reading order. Existing
semantic table repair findings are attached to canonical blocks. Heading depth
mismatches and code-comment misclassification become deterministic repairs or
targeted ambiguity candidates. Table/page evidence already produced by the
semantic-integrity layer is reused by Gemini.

If native DOCX reconciliation unexpectedly fails, the parser preserves the
Docling representation, marks the canonical document review-required, and
blocks unsafe clean ingestion rather than silently trusting degraded structure.

## Atomic clean feed

`clean_chunks.jsonl` is the only intended downstream decision-extraction feed.
It does not replace the existing `chunks.jsonl`; the latter remains a baseline
compatibility artifact.

Clean chunking guarantees:

- complete table rows with repeated table/column context;
- field labels kept with their values;
- headings represented through complete section-path context;
- code split only at safe line boundaries;
- prose split at paragraph/sentence boundaries;
- a 1,200-token maximum;
- no unresolved picture OCR or ambiguous structural blocks; and
- Gemini content only when the independent verification pass returns
  `verified` with direct evidence.

`review_queue.jsonl` records excluded candidates, evidence paths, findings,
verdicts, errors, and recommended human action.

## Guarded Gemini defence

Candidates are restricted to qualifying DOCX/PDF diagrams, PDF semantic
integrity findings, and structural blocks that remain ambiguous after
deterministic reconciliation.

The extraction pass must return schema-valid JSON containing the submitted
candidate ID, visible labels, normalized evidence boxes, relationships,
candidate claims, and unresolved regions. Claims without direct evidence or
with invented IDs are rejected.

The verification pass receives the original evidence and extraction JSON under
a separate prompt. It returns exactly one verdict per claim:

- `verified`;
- `partially_verified`;
- `unsupported`; or
- `unreadable`.

Only fully verified claims are promoted. Numeric model confidence is never an
acceptance rule. Partial results can contribute individually verified claims,
but the candidate remains in the review queue and final readiness stays
partial. If extraction produces no evidence-bearing claims, the verifier is not
called. Verification is also capped independently, so extraction cannot fan out
into an unbounded second pass.

Requests use the Gemini Developer API `v1beta` Interactions endpoint. The
default operating profile is intentionally conservative:

| Control | Default |
| --- | ---: |
| Model | `gemini-3.5-flash` |
| Thinking level | `medium` |
| Estimated run budget | ₹100 |
| Candidate limit | 5 |
| Total API-attempt limit | 7 |
| Verification-call limit | 2 |
| Concurrency | 1 |
| Request timeout | 45 seconds |
| Document AI runtime | 300 seconds |
| Retry count | 1 |
| Total reported-token limit | 60,000 |
| Output-token limit per request | 8,192 |
| Pre-request budget reserve | ₹20 |

Before every actual request or retry, the client checks the request, elapsed
time, reported-token, estimated-budget, and budget-reserve limits. A stopped
candidate and all deferred evidence go to `review_queue.jsonl`; verified local
content remains usable. The review is sequential, and responses are cached by
candidate evidence, model, API version, prompt version, and schema version.

The first actual candidate is a capability/quota probe. A shared
authentication, model, billing, quota, or response-schema failure opens a
circuit breaker so the remaining candidates are queued without repeating the
same failed API request. Gemini failure never invalidates the baseline Docling
conversion.

Response accounting deliberately precedes parsing. When the provider returns,
input/output/thought usage and estimated INR spend are committed immediately.
Only then is output checked for provider `incomplete` status, valid JSON,
Pydantic schema compliance, submitted IDs, evidence indices, and normalized
boxes. This prevents malformed but billable responses from appearing as zero
cost. The audit retains final-output JSON (including a truncated final response
when applicable), but never hidden reasoning or credentials.

The extraction contract is bounded to 24 labels, 16 components, 16
relationships, eight evidence-backed claims, four regions per claim, and eight
short unresolved items. Medium thinking remains enabled, while the response
allowance is 8,192 tokens to avoid consuming the entire allowance on thought
tokens before schema JSON finishes.

Evidence has three explicit states:

1. `source_evidence_path`: immutable local Docling/native evidence;
2. `prepared_evidence_paths`: selected copies or targeted tiles; and
3. `submitted_evidence_paths`: the exact files carried by an actual API attempt.

Only selected candidates are prepared. Extremely wide evidence is split into
overlapping tiles. Complex ERDs retain a full view and add two targeted tiles so
relationships have global context while field text remains readable.

Spend shown by the application is an estimate based on the response's reported
input tokens plus visible output and thought tokens, configurable per-million
token prices, and a configurable USD/INR conversion rate. It is not a provider
invoice. Because usage is known only after a response arrives, a single
in-flight response can cross the nominal limit; the ₹20 request reserve and
per-request output cap reduce this exposure.

Every attempted or circuit-skipped candidate retains safe audit metadata:
model/API version, prompt/schema version, evidence path/hash, failure stage,
attempt counts, timing, retryability, and the candidate that opened the circuit.
Exception text is redacted against the configured key before persistence.

## Readiness contract

| Status | Meaning |
| --- | --- |
| `ready` | All required candidates were deterministically resolved or two-pass verified. |
| `partial_ready` | Verified chunks exist, but unresolved evidence was excluded. |
| `blocked` | No safe clean feed exists or document-wide canonical integrity failed. |

Core and visual readiness remain available for backward-compatible diagnostics.
Downstream ingestion should gate exclusively on `clean_readiness` and read only
`clean_chunks.jsonl`.

## Operator experience and live observability

The UI explicitly discloses external Gemini processing and shows whether the
key is configured without revealing it. AI starts only after the operator
enables Gemini defence and checks an explicit approval for the displayed ₹100
estimated run budget.

During a run, a live dashboard displays:

- detected and selected diagrams/candidates;
- the current candidate and extraction or verification pass;
- actual API attempts versus the request limit;
- actual diagram API passes, including extraction, verification, and retries;
- verification calls versus their independent limit;
- input, visible output, thought, and total reported tokens;
- live estimated rupee spend and remaining run budget;
- budget, request, and runtime progress bars;
- retry, cache, guard-stop, and completion activity; and
- a timestamped event table suitable for diagnosing delay and spend.

The dashboard is fed by a structured progress callback. The same redacted
events are appended to `gemini_events.jsonl`, making the timeline inspectable
after the Streamlit rerun finishes. Dashboard state is persisted in the
Streamlit session and replayed from the journal after reruns or restarts.
`gemini_review.json` remains the detailed
candidate and response audit.

For DOCX, embedded drawings are aligned in native OOXML drawing order. Docling
parent references remain secondary anchors for generated DrawingML pictures.
This prevents every picture from inheriting the document's final heading and
keeps diagram selection, prompts, and downstream provenance attached to the
actual nearby section.

Results include canonical structure, verified clean chunks, review queue,
Gemini audit details, evidence previews, and separate core/visual/clean
readiness. The backward-compatible status is labelled `Legacy core readiness`
so it cannot be confused with the authoritative clean-feed gate.

Before any external request, deterministic `clean_chunks.jsonl` and
`review_queue.jsonl` are written once from local canonical processing. This
gives interruption-safe local artifacts if the browser, container, network, or
Gemini review stops. Final artifacts are regenerated after AI results so only
two-pass-verified supplements are promoted.

The CLI supports `--ai-review/--no-ai-review` and prints clean readiness plus
Gemini verification counts.

## Verification

Automated coverage includes DOCX code protection, false-heading demotion,
numeric hierarchy correction, native/Docling provenance, atomic table and
field/value chunking, no-key fail-safe behavior, evidence-path propagation,
systemic-failure circuit breaking, strict two-pass promotion, secret
non-disclosure, candidate/request/verification caps, medium-thinking request
configuration, no-claim verifier suppression, rupee-budget stopping, event
journaling, and all pre-existing parsing/integrity behavior. Live Gemini tests
remain explicitly gated by environment variables so the normal suite never
spends API quota.
