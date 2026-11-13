# Billing RAG: System Specification

This document describes **what the system does and how it is designed**, as built. Setup and deployment steps are in
[README.md](README.md). The reasoning behind each choice, with measurements, is in [docs/DECISIONS.md](docs/DECISIONS.md).

Contents:

1. [Purpose](#1-purpose)
2. [Users and access](#2-users-and-access)
3. [Architecture](#3-architecture)
4. [Document processing (ingest)](#4-document-processing-ingest)
5. [Chunks and tags](#5-chunks-and-tags)
6. [Data model](#6-data-model)
7. [Key information and summary](#7-key-information-and-summary)
8. [Questions and answers (chat)](#8-questions-and-answers-chat)
9. [API](#9-api)
10. [Frontend](#10-frontend)
11. [Security, limits and operations](#11-security-limits-and-operations)
12. [Configuration](#12-configuration)
13. [Quality and evaluation](#13-quality-and-evaluation)
14. [Known limitations and next steps](#14-known-limitations-and-next-steps)

---

## 1. Purpose

A web app where a user:

1. **Uploads a contract PDF.**
2. The system **processes it automatically** (parse, chunk, tag, embed) and shows live progress.
3. The user gets **key information and a summary**, with every fact linked to its clause and page.
4. The user **asks questions in a chat**, including follow-ups. Answers come only from the document and cite clauses
   and pages. If the document does not say something, the answer says so.

**Design principles**

- **Generic, not tied to one contract.** The pipeline targets long-form commercial contracts (numbered clauses,
  definitions, schedules, tables). Nothing specific to one contract (names, page numbers, page offsets) is
  hardcoded; everything is detected from the PDF. The sample contract (a 128-page tower-sharing agreement) is the
  test case.
- **Every claim is traceable.** Citations are built from stored metadata, never invented by the model. Numbers and
  dates are checked against the cited text, and anything that fails is flagged.
- **Never guess.** If the document does not state something, the system returns nothing or says "not stated".
- **Not legal advice.** The UI shows a permanent notice that content is AI-generated and must be checked against
  the document.

---

## 2. Users and access

- **No sign-up or login.** Anyone with the site link can use the app.
- **Anonymous identity per browser.** On first visit the API issues a random ID signed with HMAC-SHA256
  (`services/shared/session.py`). The browser keeps it in `localStorage` and sends it as `Authorization: Bearer <token>`
  on every call. Every Lambda verifies the signature before using the ID as `owner_id`; an ID sent by the client in
  any other way is ignored.
- **Each browser sees only its own documents and chats.** Every SQL query filters by `owner_id`. Another browser's
  document returns 404 (not 403), so its existence is not revealed.
- **Consequence:** clearing browser data loses access to the documents (they remain stored). The library page says
  so.

---

## 3. Architecture

```
Browser (Next.js static site on AWS Amplify Hosting)
   |
   |-- POST /session ------------------> API Lambda --> signed anonymous token
   |-- POST /documents/upload-url -----> API Lambda --> creates the documents row + presigned S3 POST
   |-- PDF upload --------------------> S3 (private) uploads/<document_id>.pdf
   |                                       |
   |                                S3 event -> SQS (DLQ after 2 tries) -> Ingest Lambda
   |                                       |      parse > chunk > tag > embed > store
   |                                       v
   |                                Neon Postgres (+ pgvector) <---- Insights Lambda (async)
   |                                                                 key information + summary
   |-- poll status, insights, outline -> HTTP API Gateway -> API Lambda -> Neon
   |-- chat (streamed NDJSON) ---------> Lambda Function URL -> Chat Lambda -> Neon search -> Bedrock -> stream
```

| Concern | Choice |
|---|---|
| Frontend | Next.js 16 (App Router, TypeScript, Tailwind, shadcn/ui on Base UI), **static export** on Amplify Hosting |
| Backend code | Python 3.13 for all four Lambdas (ingest, insights, API, chat) |
| Packaging | **One Docker image** (`services/Dockerfile`) with two targets: `lambda` (ingest, insights, API; the handler is chosen per function) and `chat` (adds AWS Lambda Web Adapter for streaming). Needed because PyMuPDF and pdfplumber exceed the 250 MB zip limit |
| Infrastructure | AWS Amplify Gen 2 with a custom CDK stack (`amplify/backend.ts`), deployed with `npx ampx sandbox` |
| File storage | S3, private, SSE-S3 encryption, TLS only, presigned URLs valid for 5 minutes |
| Queue | SQS between S3 and ingest (16-minute visibility, dead-letter queue after 2 receives) so uploads are never lost |
| API | HTTP API Gateway -> one API Lambda, throttled to 20 requests/s (burst 40) |
| Streaming chat | Lambda Function URL in RESPONSE_STREAM mode; a Starlette app under uvicorn behind the Lambda Web Adapter |
| Database | Neon Postgres with `pgvector` (HNSW index) and built-in full-text search. The app uses the **pooled** connection string; migrations use the **direct** one |
| LLM | **Qwen3-32B** on Amazon Bedrock (Converse and ConverseStream APIs, IAM auth), behind an `LLMProvider` interface (`services/shared/llm.py`) |
| Embeddings | **Amazon Titan Text Embeddings V2**, 1024 dimensions, normalised, behind an `EmbeddingProvider` interface (`services/shared/embeddings.py`) |
| Secrets | One Secrets Manager secret (JSON: `DATABASE_URL`, `SESSION_KEY`) written by `scripts/put_secret.py`; never in git or CloudFormation |
| Region | ap-southeast-2 for Bedrock, Lambda, S3 and Neon. The static site can be hosted in any region |
| Network | No VPC: Lambdas reach Neon and Bedrock over TLS (a VPC would need a ~$32/month NAT gateway for no gain here) |

**Qwen3-32B constraints the code respects**

- **Limited context (about 32K tokens).** A typical contract is 80-90K tokens, so the whole document is never sent
  in one prompt. Insights use map-reduce (section 7); chat sends only the retrieved clauses (about 9K tokens).
- **JSON is not always valid.** Every structured call has a schema, is validated, and is retried with the
  validation error fed back (`complete_json` in `services/shared/llm.py`).
- **`<think>` blocks** are stripped from all output before parsing or display. All calls run in non-thinking mode.

---

## 4. Document processing (ingest)

Code: `services/ingest/`. Lambda entry: `handler.py`. Pipeline: `run.py` and `pipeline.py`.

### 4.1 Status flow

`documents.status` moves through:

```
uploaded -> parsing -> chunking -> tagging -> embedding -> ready_for_insights -> extracting -> ready
                                                                            (any stage) -> failed
```

`documents.progress` (0-100) is updated within each stage, and the UI polls it.

### 4.2 File checks

Before parsing, the ingest checks the file is a real PDF (`%PDF-` header, opens with PyMuPDF), is not
password-protected, is at most **50 MB** and at most **500 pages**. S3 already refuses larger or non-PDF uploads
through the presigned POST policy. A file that fails is marked `failed` with a readable message and is not retried.
A PDF with no text layer (scanned) is rejected as `needs_ocr`; OCR is not supported yet.

### 4.3 Stages

1. **Extract text with layout** (PyMuPDF). Each line comes with its position, font and bold flag. Repeating
   headers, footers and page numbers are removed.
2. **Page numbers.** The printed page number usually differs from the PDF page number (cover and table-of-contents
   pages). The offset is computed from the footers. Both `page_pdf` and `page_printed` are stored, and citations
   show the printed page.
3. **Clause tree.** Built from the body: Clause (`1.`) > Sub-clause (`1.1`) > Sub-sub-clause (`1.1.1`) > lettered and
   roman list items, plus Schedule > Paragraph. A number counts as a heading only if it is in its own column,
   starts a paragraph and plausibly follows the previous number, which stops wrapped cross-references from being
   read as headings.
4. **Table-of-contents check.** The printed table of contents is parsed and compared with the tree (number, fuzzy
   title, printed page within ±1). A clause in the TOC that is missing from the body is a **parse warning**, shown in
   the processing view, and lowers `parse_confidence`. It does not stop ingest.
5. **Definitions.** The definitions clause (`"Term" means ...;`, `"Term" has the meaning given in ...`) is split into
   one record per term.
6. **Tables** (pdfplumber). One chunk per group of rows, with the header row repeated and each row written as
   readable text. Tables that continue across pages reuse the header. Tables drawn with filled rectangles are re-read
   from word positions, and the re-read is kept only if no numbers or words are lost. Blank form grids become one
   `form` chunk, not data rows.
7. **Special content.** Schedules that only say "[Attached separately]" or "[Intentionally left blank]" become
   `placeholder` chunks, so the chat can say the content is not included. Forms in schedules are chunked one per form.
   Signature pages are tagged `execution`.
8. **Chunk** (section 5).
9. **Tag** (section 5).
10. **Embed** the chunks (Titan, parallel batches with retries). Embeddings whose text and model are unchanged are
    reused on a rerun.
11. **Store** in Neon, set `ready_for_insights`, and invoke the Insights Lambda asynchronously.

### 4.4 Lambda settings

Ingest: 3 GB memory, 15-minute timeout, at most 2 running at once. A 128-page contract takes about 2 minutes in
AWS. Insights: 1 GB, 15 minutes, 1 automatic retry. The whole upload-to-ready time is about 3.5-4 minutes.

---

## 5. Chunks and tags

### 5.1 Chunking (`services/ingest/chunker/`)

- **Parent-child.** A **parent** is a level-2 clause or a schedule paragraph; it is what the LLM reads. A parent
  over about 800 tokens is split into level-3 parents. **Children** of about 400 tokens are what gets embedded and
  searched. A parent under 400 tokens is stored once with `role = both`.
- **A lead-in sentence ending in `:` always stays with its list.** If the list is split, each piece repeats the
  lead-in. A sentence introducing a table is copied into each of the table's chunks.
- **Worked examples stay with their paragraph.**
- **Breadcrumb.** Every chunk carries its path, for example
  `Master Tower Sharing Agreement > Clause 8 Charges > 8.2 Payment Terms`. It is embedded with the text and shown in
  the UI.
- Tokens are estimated as characters / 4.

### 5.2 Tags (`services/ingest/tagger/`)

Rules first (cheap and deterministic), then one LLM pass for what rules cannot decide.

| Tag | How | Meaning |
|---|---|---|
| `clause_id`, `clause_path`, `heading`, `level` | parser | e.g. `8.2.1` |
| `source_part` | parser | body / definition / schedule / form / table / execution / placeholder |
| `schedule_no`, `paragraph_no` | parser | for schedule content |
| `page_pdf`, `page_end_pdf`, `page_printed` | parser | for citations and the PDF viewer |
| `chunk_kind` | parser | clause / definition / table_row / form / example / execution / placeholder |
| `refs_out`, `refs_in`, `refs_unresolved` | rules | resolved cross-references; "paragraph 3.3 of Schedule 4" becomes `S4:3.3` |
| `defined_terms_used` | rules | defined terms found in the chunk |
| `modality`, `modality_counts` | rules | obligation (shall), right (may), prohibition, condition |
| `extracted_values` | rules | amounts, currencies, percentages, day counts, durations |
| `topic[]` | LLM | controlled vocabulary built from the clause and schedule titles (e.g. termination, liability, charges) |
| `party_obligor` | LLM | which party the provision binds; party names come from the contract's own preamble |

The LLM pass uses a strict JSON schema with validation and retries.

---

## 6. Data model

Neon Postgres, schema in `db/migrations/` (applied in order by `scripts/migrate.py`). All access goes through
`services/shared/db.py`.

| Table | Purpose | Key columns |
|---|---|---|
| `documents` | One row per upload | `id`, `owner_id`, `title`, `s3_key`, `status`, `progress`, `error`, `page_count`, `page_offset`, `parse_confidence`, `parse_warnings`, `content_sha256` |
| `chunks` | Parents and children | `document_id`, `parent_id`, `role`, `seq`, `chunk_kind`, `source_part`, `clause_id`, `breadcrumb`, `heading`, pages, `text`, tags (section 5.2), `embedding vector(1024)`, `embedding_model`, `tsv` (generated full-text vector) |
| `definitions` | One row per defined term | `document_id`, `term`, `definition`, `chunk_id` |
| `insights` | Key information and summary | `document_id`, `key_info`, `summary`, `meta` (costs, checks), `map_cache` |
| `conversations` | One chat thread | `document_id`, `owner_id` |
| `messages` | Questions and answers | `conversation_id`, `role`, `content`, `citations`, `meta` (standalone question, retrieved chunks, checks, timings), `feedback` (-1 / 1) |
| `usage_counters` | Daily limits | `day`, `scope` (owner or global), `kind`, `count` |

Indexes: HNSW on `chunks.embedding` (cosine), GIN on `chunks.tsv` and `chunks.topic`, plus `(document_id, clause_id)`
and `(owner_id, created_at)`. Deleting a document cascades to all its rows; the API also deletes the S3 object.

Database chunk IDs are `uuid5(document_id, chunk_id)`, so the same PDF uploaded from two browsers never collides.

---

## 7. Key information and summary

Code: `services/insights/`. Runs automatically after ingest.

### 7.1 Output

**Key information:** 29 fields in 7 sections (`services/insights/schema.py`):

| Section | Fields |
|---|---|
| Overview | parties, agreement type, agreement date, effective date, term, renewal |
| Commercial | fees and rates, premiums and discounts, escalation, payment terms, invoicing, taxes |
| Commitments | volume and site commitments, consequences of missing them |
| Service levels | availability targets, remedies, downtime rules |
| Term and termination | termination rights, consequences of termination |
| Risk allocation | liability caps, exclusions, indemnities, insurance, force majeure |
| Other | assignment, confidentiality, disputes and governing law, anti-bribery and sanctions, change control |

Each value carries a **verbatim quote** and **citations** (clause and printed page). A field the document does not
cover is empty. Also produced: **unusual or one-sided provisions** (labelled as the model's observation) and **missing
items** (placeholder schedules).

**Summary:** an executive summary (150-250 words), a "Top 10 things to know" list, and a one-paragraph summary per
clause and schedule.

### 7.2 Method: map-reduce

1. **Map:** one call per batch of about 3K tokens of parent clauses from one clause or schedule (about 55 calls for
   a 128-page contract). Each returns a summary paragraph, facts tagged with one of the 29 fields (each with a quote),
   and up to 2 unusual provisions. Map outputs are cached in `insights.map_cache`, so a rerun with unchanged text
   only pays for the reduce.
2. **Reduce:** one call per field that has several facts, one per clause that needed several batches, one for the
   unusual provisions, and a final call for the executive summary and top 10.
3. Placeholder schedules become missing items without an LLM call.

### 7.3 Citations and verification

- **Citations never come from the model.** The model can only point at passage IDs it was given (e.g. `S4:3.1`);
  labels and page numbers come from chunk metadata. The passage containing a fact's quote becomes its citation.
- **Verification** (`verify.py`): every number and date in a value is checked against the text of its cited chunks,
  with normalisation ("USD 1,456.00" = "USD 1,456", "30 days" = "thirty (30) days", "2 November 2021" = "the 2nd day of
  November 2021"). A value that fails is stored as **unverified** with the missing numbers, and the UI flags it.

Cost: about $0.10 and 2-3 minutes per 128-page contract.

---

## 8. Questions and answers (chat)

Code: `services/chat/`. Pipeline: `service.py`.

1. **Condense.** A follow-up ("and for Gold Sites?") is rewritten into a standalone question from the last 3 turns.
   A new topic is passed through unchanged.
2. **Hybrid retrieval** (`retrieve.py`): vector search over child chunks plus full-text search that requires every
   question word (a precision signal, at half weight), fused with Reciprocal Rank Fusion. Children are grouped to
   their parent clause.
3. **Exact matches:** a clause number in the question ("Clause 7.9") or a defined term fetches that clause or
   definition directly.
4. **Expand:** follow cross-references one hop from the top hits and attach the definitions of terms used.
5. **Answer** (`answer.py`): the top 8 parent clauses (about 9K tokens) go to Qwen3-32B with instructions to answer
   only from them and cite as `[Clause 8.2, p. 33]`. Each clause is introduced by the exact citation to copy. Missing
   or partial evidence must be stated, and "subject to" conditions mentioned.
6. **Stream** through Bedrock ConverseStream, as NDJSON events: `conversation`, many `token`, then `done` (with
   citations and checks) or `error`.
7. **Check:** citations in the answer are matched back to the supplied clauses (a sub-clause resolves to its clause).
   A citation that matches nothing is reported, never turned into a link. Numbers and dates are checked as in 7.3.
8. **Save** the question, answer, citations and details in `messages`. Thumbs up/down is saved as `feedback`.

No reranker: retrieval already has the right clause in the top 8 for every golden question, and a rerank call would
add 1-2 seconds per question for no measured gain.

Time to the first streamed word: about 4-7 seconds in AWS.

---

## 9. API

Base URL: the HTTP API (`apiUrl` in `amplify_outputs.json`). Every route except `/session` needs
`Authorization: Bearer <token>`. Every response is scoped to the caller's `owner_id`.

| Method and path | Purpose |
|---|---|
| `POST /session` | Issue a new anonymous token |
| `POST /documents/upload-url` | Create the document row; return a presigned S3 POST (size and type enforced) and `document_id`. Counts against the upload limits |
| `GET /documents` | The caller's documents with status |
| `GET /documents/{id}` | One document: status, progress, warnings, error |
| `DELETE /documents/{id}` | Delete the S3 object and all rows |
| `GET /documents/{id}/insights` | Key information and summary |
| `GET /documents/{id}/outline` | Clause and schedule tree |
| `GET /documents/{id}/chunks/{chunk_id}` | One clause's text and pages, for the citation panel |
| `GET /documents/{id}/pdf` | Short-lived presigned link to the PDF |
| `GET /documents/{id}/conversations` | The caller's conversations on that document (empty ones excluded) |
| `GET /conversations/{id}` | Message history |
| `POST /messages/{id}/feedback` | Save thumbs up (`1`) or down (`-1`) |

Chat (separate base URL, `chatUrl`):

| Method and path | Purpose |
|---|---|
| `POST /documents/{id}/chat` | Body `{"message": "...", "conversation_id": "..."?}`. Returns `application/x-ndjson`, one event per line. Counts against the question limits before any LLM call |

Errors return JSON `{"error": "..."}`. A limit returns HTTP 429 with a readable message.

---

## 10. Frontend

Code: `web/`. Static export (`output: "export"`), so the site is plain files with no server to run or secure.
Backend URLs are build-time variables `NEXT_PUBLIC_API_URL` and `NEXT_PUBLIC_CHAT_URL` (`web/src/lib/api.ts`).

**Pages**

- `/`: **Documents library.** Drag-and-drop upload, list with status chips, delete, and the note that documents are
  tied to this browser.
- `/document?id=...`: **Document workspace** (a query string, because a static export cannot serve dynamic paths).
  - **Processing view** while not ready: step tracker (Uploaded > Parsing > Chunking > Tagging > Embedding > Extracting
    > Ready), live progress, parse warnings.
  - **Summary** tab: executive summary, top 10, clause-by-clause summaries.
  - **Key information** tab: cards by section, each value with its quote, citation chips, and an "Unverified" flag.
  - **Clauses** tab: collapsible clause and schedule tree with search.
  - **Ask** tab: streaming answers, suggested questions, follow-ups, citation chips, copy, thumbs up/down, new
    conversation, earlier conversations.
- **Citation panel:** clicking any citation opens a side panel with the clause text (quote marked) and the PDF page
  (react-pdf) with the cited lines highlighted.
- Light, dark and system themes; responsive down to phone width; keyboard focus, skip link, labelled controls.
- Footer notice: AI-generated content, not legal advice.

`web/scripts/flatten-segments.mjs` runs after `next build` to copy Next.js's nested route payload files to the flat
names the browser requests, so static hosting does not return 404 during navigation.

---

## 11. Security, limits and operations

Full checklist: [docs/SECURITY.md](docs/SECURITY.md).

- **Identity and isolation:** signed anonymous tokens; every query filtered by `owner_id` (section 2).
- **CORS:** the API, the chat URL and the S3 bucket accept browser requests only from `ALLOWED_ORIGINS` (the site URL).
- **Browser security headers** (`customHttp.yml`): Content-Security-Policy that only allows the site, the API, the
  chat URL and S3; HSTS; `nosniff`; `X-Frame-Options: DENY`; strict referrer policy.
- **Uploads:** presigned POST limited to PDF and 50 MB; links expire in 5 minutes; the ingest re-checks the file.
- **No document text, prompts or answers in logs.** Logs are JSON with IDs, counts, timings and error type names.
- **Least-privilege IAM:** each function can call only the two Bedrock models, read the one secret, and touch only
  its bucket actions.
- **Usage limits** (`services/shared/limits.py`, counters in `usage_counters`, reset daily):

  | Limit | Default |
  |---|---|
  | Uploads per browser per day | 5 |
  | Questions per browser per day | 60 |
  | Documents per browser in total | 10 |
  | Ingests per day, whole app | 30 |
  | Questions per day, whole app | 1,000 |
  | File size / pages | 50 MB / 500 |

- **Monitoring:** CloudWatch alarms on errors of each of the four Lambdas and on any message in the ingest
  dead-letter queue (visible in the CloudWatch console; no email). There is no AWS Budgets alert; spend is bounded by
  the global daily limits (worst case roughly $5 a day in uploads and $2 a day in questions).
- **Failures:** a failed ingest sets `status = failed` with a readable `error`, shown in the library and workspace.
- **Environments:** development uses the Neon `dev` branch and an Amplify sandbox stack. Production steps (Neon `main`,
  a separate secret, a separate deploy) are in `docs/SECURITY.md`.

---

## 12. Configuration

| Setting | Where | Purpose |
|---|---|---|
| `DATABASE_URL` | `.env.local`; in AWS, the secret | Pooled Neon connection string (the app) |
| `DATABASE_URL_UNPOOLED` | `.env.local` | Direct Neon connection string (migrations, `db_check.py`) |
| `SESSION_KEY` | the secret (generated by `put_secret.py`); locally `out/dev_session_key` | Signs anonymous tokens. Changing it signs every browser out |
| `AWS_PROFILE` | `.env.local` or shell | Which AWS account local runs and scripts use |
| `BEDROCK_REGION`, `LLM_MODEL_ID`, `EMBEDDING_MODEL_ID`, `EMBEDDING_DIMENSIONS` | `.env.local`; in AWS, set by `amplify/backend.ts` | Model choice. Changing the embedding model means re-embedding every document |
| `DATABASE_SECRET_ID` | set by `amplify/backend.ts` | Name of the secret the Lambdas read (default `billing-rag/dev`, override with `BILLING_RAG_SECRET` at deploy) |
| `ALLOWED_ORIGINS` | shell, at deploy time | Site URL allowed by CORS. Default `*` |
| `UPLOADS_PER_DAY`, `QUESTIONS_PER_DAY`, `DOCUMENTS_PER_BROWSER`, `GLOBAL_INGESTS_PER_DAY`, `GLOBAL_QUESTIONS_PER_DAY`, `MAX_UPLOAD_MB`, `MAX_PAGES` | Lambda environment (add to `amplify/backend.ts`) | Usage limits |
| `NEXT_PUBLIC_API_URL`, `NEXT_PUBLIC_CHAT_URL` | `web/.env.local` or the hosting build environment | Backend URLs baked into the frontend |

---

## 13. Quality and evaluation

The `eval/` folder measures quality on the sample contract (its expected answers only apply to that contract):

| File | What it checks |
|---|---|
| `golden_questions.yaml` | 41 questions, each with the clause that must be retrieved and a reference answer |
| `golden_insights.yaml` | 17 checks on key information (parties, date, base rate, term, liability caps, governing law...) |
| `golden_conversations.yaml` | 4 multi-turn conversations, "not in the document" questions and a topic switch |
| `run_eval.py` | Retrieval hit@1/3/8 and MRR; with `--answers`, LLM-graded answer correctness |
| `check_insights.py` | Stored key information against `golden_insights.yaml` |

**Latest results** (`eval/results/2026-10-30.md`):

| Measure | Result |
|---|---|
| Retrieval: right clause in the top 3 | 95% (top 1: 80%, top 8: 100%) |
| Key information checks | 17/17; 29/29 fields filled |
| Answers to golden questions | 38 correct, 3 partial, 0 incorrect (of 41) |
| Conversation turns | 17/17 |

Automated tests: `python -m pytest -q` (parser, tagger, insights generation and verification, chat answer handling,
API and session security). Browser test: `web/e2e/full-flow.mjs` (22 checks from upload to chat).

---

## 14. Known limitations and next steps

- **Scanned PDFs are not supported** (no OCR). They are rejected with a clear message. Textract or a vision model is
  the planned path.
- **Run-to-run variation.** The LLM does not always produce identical insights; a golden check can occasionally fail.
- **The number check cannot see who a provision binds.** A value can have the right number but the wrong party.
  Quotes are shown next to each value so a reader can check.
- **The answer judge in the eval is the same model as the answerer**, so it can share its blind spots.
- **Anonymous identities can be re-created** by clearing the browser, so per-browser limits only slow abuse; the
  global daily caps bound the cost. Tokens do not expire.
- **Possible improvements:** a reranker if retrieval quality drops on other contracts; a stronger model for extraction
  only; OCR; a login if the app needs named users or sharing; an AWS Budgets alert.
