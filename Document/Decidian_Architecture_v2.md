# Decidian — Architecture & Stack (v2)
Status: Locked for MVP build. Target: pilot with one team, 15–30 developers, 50–100 ingested docs (RFCs/SRS/proposals).

---

## 1. Tech Stack

| Layer | Choice | Why |
|-------|--------|-----|
| Frontend | Next.js + TypeScript | Pages + API routes together, team already knows React |
| API server | Fastify + TypeScript | Native JSON-schema validation — most of this app is "enforce exact shape" |
| ORM | Drizzle | Closer to raw SQL, better fit once decision_relationships graph queries show up |
| Database | PostgreSQL | Everything connected by foreign keys — project → document → decision → relationship |
| File storage | S3 or Cloudflare R2 | Presigned upload URLs — browser uploads directly, never through API server |
| Queue | Redis + BullMQ | Retries, progress tracking, failure states — doc processing takes minutes |
| Worker | Node.js + TypeScript | Owns orchestration: queue, DB writes, classification, LLM calls |
| Parsing service | Python (FastAPI) — GCP VM | Docling runs here. See §2.2 |
| AI provider | Claude, behind provider-agnostic interface | LLMProvider.extract(chunk, schema) — Claude first, swappable later |
| Docling host | GCP Compute Engine E2 custom, 6 vCPU, 24 GB RAM, Spot instance | Persistent disk caches models, ~$31/mo, data never leaves GCP |

**Explicitly not decided yet** (flag before pilot, not before MVP code): auth/identity provider, hosting/deploy target for main app, Slack/email notification service.

---

## 2. Extraction Pipeline

Core insight: parse first, don't dump whole document to the model. Claude can ingest raw PDFs, but doing that by default breaks exact source provenance — a TL needs to see the real page/paragraph a decision came from, not the model's guess.

### 2.1 Pipeline stages

```
upload → secure receive → parse & normalize → chunk → classify
      → extract candidates → validate → dedupe/conflict-check
      → TL review → approved decision
```

1. **Secure receive** — presigned upload (browser → S3/R2 directly), file-type check from actual contents (not extension), size/page limits, hash-based dedup, status = queued.
2. **Parse & normalize** — Docling extracts clean text, preserving page number, section heading, paragraph range, and position for every chunk. This location data is not optional — it's what the TL review screen shows as evidence.
3. **Chunk intelligently** — split by heading/section/requirement block, not fixed character count. Small overlap between chunks so a decision split across a page boundary isn't lost. Generate a short document summary (purpose, systems involved, actors, date/status).
4. **Classify before extracting** — tag each chunk as one of: approved_decision, business_requirement, technical_constraint, proposal, rejected_option, background, open_question. This stops "we considered MongoDB but rejected it" from becoming an approved rule.
5. **Extract candidates** — Claude structured output, JSON schema enforced. Output goes to decision_candidates, never directly to decisions. Model can suggest owner/criticality, TL confirms.
6. **Validate** — second pass (cheaper model, same provider for MVP) checks: is this genuinely approved, is the source excerpt sufficient, did the first pass add anything not in the text, is context missing.
7. **Dedupe / conflict detection** — group likely duplicates, but never silently merge. Flag conflicting decisions (overlapping scope, different constraints) for TL resolution — never auto-resolved.
8. **TL review** — every candidate requires human approval, regardless of confidence score. Confidence only reorders the queue. Approve / edit / reject.
9. **Approved decision** — becomes decisions + first decisionVersions row. This is what the diff engine checks future code against.

### 2.2 Parsing: Docling on GCP VM (FastAPI microservice)

Parsing is owned by a dedicated Python (FastAPI) microservice running on a GCP Compute Engine VM — called by the Node worker, not folded into it.

**VM spec (locked):**
- Machine: E2 custom, 6 vCPU, 24 GB RAM
- Disk: 50 GB standard persistent boot disk (Container-Optimized OS)
- Provisioning: Spot/Preemptible (~$31/mo vs $155 on-demand)
- Network: Internal IP + Cloud NAT, no public IP
- Region: Same as main backend (us-central1)

**Why GCP VM over alternatives:**

| | HF Spaces Free | GCP Cloud Run | GCP VM (E2 Spot) |
|---|---|---|---|
| Cost | $0 | ~$20-50/mo | ~$31/mo |
| RAM | 16GB | Up to 32GB | 24GB (configurable) |
| Cold start | 60s+ (sleep + model dl) | 45s+ (model dl every time) | ~10s (models baked in image) |
| Persistence | None (sleeps after 48h) | None | Persistent disk |
| Data control | ❌ HF hosts docs | ✅ Your project | ✅ Your project |
| Fit for Docling | Prototype only | Poor (stateless, heavy boot) | ✅ Correct |

**Container image:**
```dockerfile
FROM python:3.11-slim
RUN pip install docling fastapi uvicorn
RUN docling download-models  # bake models into image
COPY app.py .
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

**Endpoint:**
```
POST /parse
Body:   { "storage_url": "s3/r2 signed URL" }
Response: { "chunks": [...] }  // matches documentChunks schema
```

Node worker calls this, gets structured chunks back, writes to documentChunks, then proceeds to classification and Claude extraction. Python's scope stays narrow: bytes in, structured text out. It does not touch the queue, DB, or LLM calls.

**Docling replaces:** PyMuPDF + Camelot + Tesseract + unstructured. Single coherent pipeline (RT-DETR layout model, EasyOCR fallback, DOCX/PPTX/HTML support) that maps cleanly onto documentChunks fields.

### 2.3 Other AI providers (behind LLMProvider interface)

- **OpenAI (GPT-4.1/5-class)** — schema-constrained structured output, mature tooling
- **Google Gemini** — large context window, often cheaper, decent native PDF handling
- **Self-hosted (Llama/Qwen-class)** — not MVP, but a real lever later for regulated customers unwilling to send RFCs to third-party APIs

**v2 idea (not MVP):** cross-provider validation — step 6 using a different provider, not just a cheaper model from the same family. Same-family models share blind spots.

---

## 3. Database Schema

Full schema, no shortcuts on structure. See §4 for which relationship behaviors are real now vs. stubbed.

```typescript
import { pgTable, uuid, varchar, text, integer, timestamp, jsonb, pgEnum, boolean, real } from "drizzle-orm/pg-core";

// ── Enums ─────────────────────────────────────────
export const docTypeEnum = pgEnum("doc_type", ["rfc", "srs", "proposal", "meeting_notes", "other"]);
export const docStatusEnum = pgEnum("document_status", ["queued", "processing", "parsed", "parsing_failed", "done"]);
export const decisionTypeEnum = pgEnum("decision_type", [
  "approved_decision", "business_requirement", "technical_constraint",
  "proposal", "rejected_option", "background", "open_question",
]);
export const decisionStatusEnum = pgEnum("decision_status", [
  "pending_review", "approved", "edited", "rejected", "superseded",
]);
export const relationshipTypeEnum = pgEnum("relationship_type", [
  "supersedes", "conflicts_with", "depends_on", "duplicates", "relates_to",
]);
export const relationshipStatusEnum = pgEnum("relationship_status", ["unresolved", "resolved", "acknowledged"]);

// ── Core ──────────────────────────────────────────
export const projects = pgTable("projects", {
  id: uuid("id").defaultRandom().primaryKey(),
  name: varchar("name", { length: 255 }).notNull(),
  repoUrl: varchar("repo_url", { length: 500 }),
  techStack: jsonb("tech_stack"),
  createdBy: uuid("created_by").notNull(),
  createdAt: timestamp("created_at").defaultNow(),
});

export const documents = pgTable("documents", {
  id: uuid("id").defaultRandom().primaryKey(),
  projectId: uuid("project_id").references(() => projects.id).notNull(),
  filename: varchar("filename", { length: 500 }).notNull(),
  storageUrl: varchar("storage_url", { length: 1000 }).notNull(),
  fileHash: varchar("file_hash", { length: 64 }), // dedup on re-upload
  docType: docTypeEnum("doc_type"),
  status: docStatusEnum("status").default("queued"),
  uploadedBy: uuid("uploaded_by").notNull(),
  uploadedAt: timestamp("uploaded_at").defaultNow(),
});

export const documentVersions = pgTable("document_versions", {
  id: uuid("id").defaultRandom().primaryKey(),
  documentId: uuid("document_id").references(() => documents.id).notNull(),
  versionNumber: integer("version_number").notNull(),
  storageUrl: varchar("storage_url", { length: 1000 }).notNull(),
  summary: jsonb("summary"), // { purpose, systemsInvolved, actors, definedTerms, docStatus }
  uploadedAt: timestamp("uploaded_at").defaultNow(),
});

export const documentChunks = pgTable("document_chunks", {
  id: uuid("id").defaultRandom().primaryKey(),
  documentVersionId: uuid("document_version_id").references(() => documentVersions.id).notNull(),
  chunkIndex: integer("chunk_index").notNull(),
  pageNumber: integer("page_number"),
  sectionHeading: varchar("section_heading", { length: 500 }),
  paragraphRange: varchar("paragraph_range", { length: 50 }), // "31-35"
  text: text("text").notNull(),
  classification: decisionTypeEnum("classification"),
});

// ── Extraction pipeline ──────────────────────────
export const extractionRuns = pgTable("extraction_runs", {
  id: uuid("id").defaultRandom().primaryKey(),
  documentVersionId: uuid("document_version_id").references(() => documentVersions.id).notNull(),
  modelProvider: varchar("model_provider", { length: 50 }).notNull(), // "claude" | "openai" | "gemini"
  modelName: varchar("model_name", { length: 100 }).notNull(),
  status: varchar("status", { length: 20 }).default("running"),
  startedAt: timestamp("started_at").defaultNow(),
  completedAt: timestamp("completed_at"),
  errorMessage: text("error_message"),
});

export const decisionCandidates = pgTable("decision_candidates", {
  id: uuid("id").defaultRandom().primaryKey(),
  extractionRunId: uuid("extraction_run_id").references(() => extractionRuns.id).notNull(),
  documentChunkId: uuid("document_chunk_id").references(() => documentChunks.id).notNull(),
  decisionType: decisionTypeEnum("decision_type").notNull(),
  decisionStatement: text("decision_statement").notNull(),
  constraints: jsonb("constraints"),
  systemAreaHint: varchar("system_area_hint", { length: 255 }),
  sourceExcerpt: text("source_excerpt").notNull(),
  confidenceScore: real("confidence_score"),
  confidenceReason: text("confidence_reason"),
  missingInformation: text("missing_information"),
  validationPassed: boolean("validation_passed"),
  validationNotes: text("validation_notes"),
  duplicateOfCandidateId: uuid("duplicate_of_candidate_id"), // self-ref, nullable
  status: varchar("status", { length: 20 }).default("pending_review"),
  createdAt: timestamp("created_at").defaultNow(),
});

export const processingAttempts = pgTable("processing_attempts", {
  id: uuid("id").defaultRandom().primaryKey(),
  documentId: uuid("document_id").references(() => documents.id).notNull(),
  attemptNumber: integer("attempt_number").notNull(),
  status: varchar("status", { length: 20 }).notNull(),
  errorMessage: text("error_message"),
  startedAt: timestamp("started_at").defaultNow(),
  completedAt: timestamp("completed_at"),
});

// ── Approved truth ────────────────────────────────
export const decisions = pgTable("decisions", {
  id: uuid("id").defaultRandom().primaryKey(),
  projectId: uuid("project_id").references(() => projects.id).notNull(),
  currentVersionId: uuid("current_version_id"), // set after first decisionVersion row
  status: decisionStatusEnum("status").default("approved"),
  criticality: varchar("criticality", { length: 20 }).default("normal"), // normal | high | critical
  ownerId: uuid("owner_id"),
  createdFromCandidateId: uuid("created_from_candidate_id").references(() => decisionCandidates.id),
  createdAt: timestamp("created_at").defaultNow(),
  updatedAt: timestamp("updated_at").defaultNow(),
});

export const decisionVersions = pgTable("decision_versions", {
  id: uuid("id").defaultRandom().primaryKey(),
  decisionId: uuid("decision_id").references(() => decisions.id).notNull(),
  versionNumber: integer("version_number").notNull(),
  ruleText: text("rule_text").notNull(),
  scope: jsonb("scope"), // glob patterns: files/folders this decision governs
  editedBy: uuid("edited_by").notNull(),
  editedAt: timestamp("edited_at").defaultNow(),
  changeNote: text("change_note"),
});

export const decisionSources = pgTable("decision_sources", {
  id: uuid("id").defaultRandom().primaryKey(),
  decisionVersionId: uuid("decision_version_id").references(() => decisionVersions.id).notNull(),
  documentChunkId: uuid("document_chunk_id").references(() => documentChunks.id).notNull(),
});

// ── Relationships ─────────────────────────────────
export const decisionRelationships = pgTable("decision_relationships", {
  id: uuid("id").defaultRandom().primaryKey(),
  fromDecisionId: uuid("from_decision_id").references(() => decisions.id).notNull(),
  toDecisionId: uuid("to_decision_id").references(() => decisions.id).notNull(),
  relationshipType: relationshipTypeEnum("relationship_type").notNull(),
  status: relationshipStatusEnum("status").default("unresolved"),
  note: text("note"),
  detectedBy: varchar("detected_by", { length: 20 }), // "system" | "human"
  resolvedBy: uuid("resolved_by"),
  resolvedAt: timestamp("resolved_at"),
  createdAt: timestamp("created_at").defaultNow(),
});
```

---

## 4. decision_relationships — Schema Now, Behavior by Priority

All five relationship types are in the schema from day one. Not all five get real behavior in the MVP — that distinction is deliberate, not a shortcut:

| Type | Behavior in MVP | Why |
|------|-----------------|-----|
| **supersedes** | Real — on TL confirmation, auto-flip old decision to superseded, diff engine stops checking it | Load-bearing: the design promises "compare against current, not original" |
| **conflicts_with** | Real — surfaced in TL review inbox, blocks newer candidate from auto-promoting until resolved | Biggest trust risk in the product — silent conflicts are worse than no detection |
| **depends_on** | Stub — edge stored, no cascade logic | Cascading impact analysis is a graph problem with real edge cases (cycles, multi-hop chains); build once there's usage data |
| **duplicates** | Stub — candidate-level dedup already covers the common case; this is for already-approved decisions found to duplicate later | Low frequency, low stakes if it sits until a human notices |
| **relates_to** | Informational only, always | Human cross-reference, never needs automation |

That gets you the full schema with zero shortcuts, while keeping coding effort pointed at the two relationship types that protect the thing you can't afford to get wrong: decisions silently going stale (supersedes) or silently conflicting (conflicts_with).

---

## 5. What's Next (Remaining Topics, in Sequence)

1. ~~Decision database~~ — done, this document
2. **Linking layer** — decision-to-code mapping, bootstrap tagging → semantic matching, coverage metric
3. **Diff/drift engine** — checkpoint tiers (file-save / pre-commit / PR), false-positive tolerance
4. **IDE/agent guard** — hooks, bypass mitigation (hash-based outcome watching, git-hook second layer)
5. **Dashboard + TL notification flow**
6. **Pilot mechanics** — onboarding a 15–30 dev team, week-1 flow for the first 50–100 docs
