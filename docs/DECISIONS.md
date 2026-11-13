# Decisions

## Phase 1: parser and chunker

**Parse the PDF from its layout instead of plain text.** PyMuPDF returns each line with its position, font and
bold flag. Clause numbers sit in their own column ("2.1.1" at one indent, its text at the next), headings are bold,
and the footer page number sits in the bottom margin. Using this layout is more reliable than regex over extracted
text. The PDF's built-in bookmarks are not used, because many contracts have none.

**Lines are grouped into rows with a 3 pt vertical tolerance.** A clause number and its title can be 0.1 pt
apart vertically. Matching them line by line loses clauses 2, 18 and 27 of the sample. This is the "found only
21 of 27" problem the spec warned about.

**A number counts as a heading only if it plausibly comes next.** "2.9" can follow "2.8.3", but "3." after "2.8"
with no paragraph break is a wrapped cross-reference ("…paragraphs 2.9 to 2.17 of Schedule / 3."). A number
must also start a new paragraph. At a page break, an inline number counts only if it is outdented or the previous
sentence had finished.

**The table of contents is the check, not the source.** The tree is built from the body text and then compared
with the printed TOC (numbers, fuzzy title match, printed page within ±1). A mismatch is an error and makes the
CLI exit non-zero.

**Tokens are estimated as characters ÷ 4.** This avoids depending on a tokenizer during parsing. Qwen's real
tokenizer may count about 10–20% differently, which is fine for 200–400-token targets.

**Chunk sizes.** A parent is a level-2 clause or schedule paragraph. A level-1 clause always splits into its
level-2 parts. A level-2 parent splits into level-3 parents only above 800 tokens. Children are about 400 tokens.
A parent under 400 tokens is stored once with `role: "both"` instead of duplicating it as parent and child.
Each definition is its own chunk.

**Lead-ins.** A sentence ending in ':' always stays with its list. When a list is too big for one chunk, each
piece repeats the lead-in. A sentence that introduces a table is copied into each of the table's chunks.
`check_lead_ins` enforces this and runs in the dry run and the tests.

**Tables come from pdfplumber, one chunk per group of rows, header repeated.** Merged cells take their header from
their merged span. A table at the top of a page with the same columns as one on the previous page is treated as
a continuation and reuses that table's header. Quality is the share of real (unmerged) cells that hold text. Below
0.45 the table is flagged `needs_vision` for the vision-LLM re-read in a later phase. Phase 1 has no AWS, so it
does not re-read them yet.

**Placeholders are found by content.** A schedule whose only content is a bracketed note ("[Attached separately]",
"[Intentionally left blank]") is a placeholder.

**Forms are found from the schedule title.** "Form of A, B and C" gives the form names, and a centred bold heading
containing one of those names starts a new form.

**Tables with a two-row header combine the header rows** ("GBT / Standard Configuration 1"). Equal values in
different columns are all kept, so each value stays with its own column. (Fixed after Phase 1 review.)

## Tables drawn with filled rectangles, blank form grids, and the scanned page

The three tables first flagged as garbled (PDF pages 73, 103, 127) do not need an image model: their text layer
is complete. (Decided after Phase 1 review; this replaces the earlier vision-model proposal for tables.)

- **Re-read from word positions.** Some tables draw their borders as filled rectangles instead of lines, so the
  default detector builds a distorted grid. Page 127 gave 31 columns, a split header and a split
  "Antananarivo_city". When 25% or more of the detected columns are empty, the table is re-read with pdfplumber's
  text settings. Each line of a multi-line cell then lands on its own row. Rows filling fewer than half the
  columns are fragments, merged into the nearest full row by vertical position ("Antananarivo" sits above row 1
  and "_city" below it).
- **The re-read is used only if it passes two checks:** every number in it appears in the page text, and every
  word inside the table area survives whole. Merged-cell tables (pages 69, 85 and 90) fail the second check,
  because the text strategy cuts "Number" into "Nu" | "mber", so they keep the line-based result. Only page 127
  is re-read.
- **Empty columns are dropped from every table.**
- **Blank form templates are not data tables.** A table counts as a blank template when at least 60% of the cells
  below its header are empty and the page has "____" fill-in blanks. Here that's page 73 (the inspection
  checklist) and page 103 (the Site Proposal grid). Each becomes one `form` chunk listing its labels, never
  `table_row` data.
- **Remaining work:** PDF page 64 (a signature page) is still an image with no text, and is reported as
  `image_only_page`. It holds only signatures, so nothing is lost for Q&A. OCR for scanned pages stays the spec's
  fallback path; the tool (Textract or a vision model) is to be decided when a scanned contract needs it.

## Where the sample differs from the SPEC estimates (not forced to match)

| SPEC said | Found | Why |
|---|---|---|
| ~224 definitions | 251 | 251 distinct quoted terms from "Abandoned Site" to "48V". An independent line count finds 248 starting with `"Term" means/has/...`. 2 more use "mean" or `"X" or "Y" means`, and "GSM/GSM Equivalent Tenant" shares a text run with "Group Company" (found in Phase 2 review; now split at `;"Term" means`). |
| ~226 level-2 | 115 body + 111 schedule = 226 | The spec's figure counted body and schedules together. |
| ~335 level-3 | 209 body + 121 schedule = 330 | Same. The remaining ~5 are probably cross-references the spec's regex counted as headings. |
| 188 lettered items | 220 list items | Includes (i)/(ii), (A)-(D) and "a." items as well as (a). |
| Placeholders: Schedules 5, 6 | 5, 6, 11, 15 | Schedule 11 says "[Intentionally left blank]" and Schedule 15 says "[Attached separately]" in the PDF. |
| 4 forms in Schedule 7 | 3 | The PDF has one combined "[Service Order / Service Contract]" form. |
| Schedule 1 table garbled | readable | pdfplumber reads it (quality 0.97); its last cell runs onto PDF page 67, which is handled as a continuation. |

## Phase 3: database and retrieval

**Schema additions to SPEC section 6.** `chunks.values` is named `extracted_values` (`values` is a reserved SQL
word). Added `documents.content_sha256` so re-loading a file replaces its chunks, and on `chunks`: `role`, `seq`,
`breadcrumb`, `page_end_pdf`, `tokens`, `modality_counts`, `refs_unresolved`, `table_meta`, `embedding_model`.
Database chunk IDs are `uuid5(document_id, chunk_id)`: the chunker's IDs come from the file hash, so the same PDF
uploaded from two browsers would otherwise collide. After storing, a document's status is `ready_for_insights`
(SPEC section 4.3); Phase 4 moves it to `ready`.

**Embeddings.** Titan Text Embeddings V2, 1024 dimensions, normalised; input is breadcrumb + text. The sample's
652 embedded chunks used 88,867 tokens ($0.0018). A reload reuses embeddings whose text and model are unchanged.

**Retrieval tuning, measured on 41 golden questions** (`eval/run_eval.py`; hit@3 = expected clause in the top 3
parent clauses):

| Setting | hit@3 |
|---|---|
| First version: equal-weight RRF, full-text matching any question word, parents scored best child + 25% of the others | 59-62% |
| Vector search alone | 95% (39/41 on the replay) |
| Chosen: vector + full-text requiring every word at half weight, best child only, definitions after clauses | **95%** (hit@1 80%, hit@8 100%, MRR 0.88) |
| Chosen + topic as a soft boost | 90% |
| Chosen + topic as a hard filter | 90% (hit@8 93%) |

- An any-word full-text query matches most long clauses (every clause mentions "Airtel", "Site", "days"), and equal
  RRF weight let those beat the vector's correct first hit. Requiring every word makes full text a precision
  signal: it fires on 16 of 41 questions and is the only method that finds exact names ("tower height at
  Bemanevika" -> Schedule 16). Keeping it at half weight costs one place on "How is Monthly Uptime calculated?"
  (rank 4).
- Topics stay **off by default**. Inferring topics from question words is fragile ("government" matched
  `governance`; "charge" matched the broad `charges` topic and hid S4:3.3), and the soft boost also displaced
  correct hits. The tags remain available for UI filters and for an LLM-chosen filter later.
- Definitions rank after clauses unless the question asks what something means, and are attached as context
  (question terms + terms used in the top clauses).
- Remaining misses: "What happens if Airtel does not remove its equipment after termination?" (15.6 at rank 7;
  15.5 on removal ranks first) and "How is Monthly Uptime calculated?" (rank 4). A reranker (SPEC section 8,
  step 5) is the next lever if Phase 5 answers suffer; not added yet.
- The eval set's 35 extra questions were written from the contract text in Phase 3 and should be reviewed.

## Phase 4: key information and summary

**Map-reduce over parent chunks** (SPEC section 3: Qwen3-32B cannot take the 82K-token contract). Map: one call per
batch of about 3K tokens of parent clauses from a single clause or schedule (55 calls for the sample); each returns
a summary paragraph, facts tagged with one of 29 generic key-information fields, and up to 2 unusual provisions.
Reduce: one call per field with several facts, one per clause that needed several batches, one for the unusual
provisions, and a final call for the executive summary and top 10. Placeholders (Schedules 5, 6, 11, 15) become
"missing items" without the LLM. Map outputs are cached in `insights.map_cache` (keyed by passage text, model and
`PROMPT_VERSION`), so a rerun with unchanged chunks only pays for the reduce. Status: `extracting` -> `ready`.

**Citations never come from the model.** It can only point at IDs it was given; labels and pages come from chunk
metadata. What the eval runs showed, and what was changed:

| Problem seen | Change |
|---|---|
| Positional IDs (P1, U1...) next to clause numbers: the model cited the neighbouring passage ("four weeks" cited to para 2.3, it is in 2.4), and the top 10 cited Clause 6 for Clause 7 | Passages and parts are identified by their own keys (`S2:2.4`, `U:7`); sub-clause and label forms (`16.1.2`, "Schedule 4, para 3.1") resolve to the passage |
| Facts attached a number to the wrong condition or party (Clause 16.2 caps) | Each fact carries a verbatim `quote` before its value; the passage that contains the quote becomes the citation (about three quarters of facts are quoted exactly) |
| Values dropped numbers their quote contains (caps without 30% / 60%) | Soft validation error: the batch is retried once with the omissions named |
| Invented field names (`late_payment`, `governing_law`) lost real facts | Soft retry, then a remap only when every word of the name fits exactly one field |
| Merges dropped key facts (base rate, 12-year term) and over-cited (17 sources on one item) | One reduce call per field, at most 4 sources per item, and any fact with an amount, percentage or period that no item covers is added back |
| The top 10 copied the prompt's example IDs onto every item | Placeholder IDs in prompts; soft error when items share sources or state numbers their sources lack |
| "Not specified in these passages" filler | Dropped by the validator |

A soft error retries the call but keeps the last usable reply rather than losing a batch (`complete_json`).

**Verification** (`services/insights/verify.py`). Every number and date in a value, the top 10, the observations and
the per-clause summaries is checked against the text of its cited chunks; the executive summary against the whole
document. Matching is normalised ("USD 1,456.00" = "USD 1,456", "30 days" = "thirty (30) days", "2 November 2021" =
"the 2nd day of November 2021", "year 12" = "the twelfth Contract Year"); clause references are not checked. A
failure is stored as `check.status = unverified` with the missing values, for the UI to flag.

**Results on the sample** (two fresh runs after the last change): 17/17 checks in `eval/golden_insights.yaml`
(parties, date, base rate, premiums and discounts, escalation, payment, commitment, term, renewal, liability caps
and exclusions, insurance, arbitration seat, governing law, Schedule 6 missing); 29/29 fields filled; about 125
values verified and 1 unverified per run. Both unverified values were real errors (a 33.75% rate from para 4.6
shown under para 4.7; USD 70,000 from Clause 7.4 cited to 7.5). Cost about $0.10 and 2.5 minutes per document.

**Known limitations.** Run-to-run variation is real: before the last changes a golden check failed in about one run
in two. The number check cannot see who a provision binds, so wording errors survive it: the liability caps are
written as "Towerco's liability" although Clause 16.2 applies them to each party, and the top 10 says "Airtel commits
to constructing 135 sites" where Airtel commits to instructing Towerco to construct them. The UI should show the
quotes next to each value. Qwen3's thinking mode did not engage through Bedrock's Converse API (a call without
`/no_think` returned in 4 s with no reasoning), so all calls run in non-thinking mode; a stronger model for
extraction only is the lever the spec allows if this is not good enough.

## Phase 5: chat backend

**Pipeline** (`services/chat/service.py`, SPEC section 8). (1) A follow-up is rewritten into a standalone question
from the last 3 turns; a new topic is returned unchanged (an early prompt added the previous topic to "Does the
contract say anything about personal data protection?"). (2-4) Phase 3 hybrid retrieval, clause references,
one-hop expansion and definitions, now with chunk IDs and pages for definitions. (6) The answer is generated only
from those clauses, up to about 9K tokens of context, each clause introduced by the exact citation to copy
("[Clause 8.2, p. 33]"). (7) Text is streamed through Bedrock ConverseStream with any `<think>` block removed as
it arrives. (8) The user message, the answer, its citations (chunk IDs and pages) and details (standalone question,
retrieved chunks, verification, tokens, timings) are saved; `messages.feedback` holds thumbs up or down.

**Citations are matched, not trusted.** Citations in the answer are mapped back to the context clauses; a
sub-clause ("[Clause 8.4.2]") resolves to its clause, and one that matches nothing is reported, never turned into a
link. Numbers and dates in the answer are checked against the clauses it cites, as in Phase 4.

**Rerank (SPEC section 8, step 5) is not added.** Retrieval has the expected clause in the top 3 for 39 of 41
questions and in the top 8 for all; the answer receives the top 8, and both remaining misses are still answered
correctly. An LLM rerank would add a call (about 1-2 s) to every question for no measured gain.

**Answer evaluation** (`python eval/run_eval.py --mode off --answers`, about $0.05): each golden question and each
turn of `eval/golden_conversations.yaml` (4 multi-turn conversations, 4 "not in the document" questions, a topic
switch) goes through the pipeline, and Qwen3 grades the answer against the reference. Over four runs:

| | Correct | Partial | Incorrect | Cites an expected clause |
|---|---|---|---|---|
| Golden questions (41) | 39-41 | 0-2 | 0 | 36/41 at first, 41/41 after sub-clause citations resolved |
| Conversation turns (17) | 16-17 | 0 | 0-1 | 14-16/17 |

- Follow-ups work: "What rate is used?" -> "What rate is used for accruing interest when Airtel pays an invoice late
  under the contract?";
  "And the other way round?" -> "Can Towerco terminate the agreement if Airtel loses its licence?" (Clause 14.5).
- "Not in the document" answers are honest: data protection and carbon targets are "not addressed"; "List the
  Legacy Sites" cites Schedule 6 as attached separately; Exclusion Sites cites Schedule 11 as left blank.
- The one "incorrect" verdict was a data-protection answer that also pointed to the confidentiality clause; the two
  data-protection reference answers were then clarified to allow that. Partial verdicts were the two retrieval
  misses, marked down for extra detail.
- The judge is the same model as the answerer, so it can share its blind spots. Its reasons were read for every
  verdict below "correct"; the answers judged correct were not all checked by hand.

**Latency from a local machine:** about 3 s for retrieval (database round trips to us-east-1) and 5-9 s to the
first token; inside AWS the retrieval part should drop to a fraction.

## Phase 6: AWS wiring

**Identity: a signed session token, not Cognito guest access** (SPEC section 2 allows either). On first visit the
API issues a random ID signed with HMAC-SHA256 (`services/shared/session.py`); the browser sends it as a bearer
token and every Lambda verifies the signature before using the ID as `owner_id`. Uploads use presigned URLs, so the
browser never needs AWS credentials, and the streaming chat function can check the token in two lines; Cognito would
add an identity pool and a user pool and make the check inside a Function URL awkward. Trade-off: anyone can mint new
identities, so the per-browser limits only slow a determined abuser; the global daily caps, API throttling and the
budget alarm (Phase 8) are what bound the cost.

**Chat streams through a Lambda Function URL with the AWS Lambda Web Adapter.** Lambda streams responses natively
only for Node; the Python chat runs as a small Starlette app under uvicorn, and the adapter (an extension in the
`chat` image target) turns it into a RESPONSE_STREAM function. The body is NDJSON, one event per line
(`conversation`, `token`..., `done` or `error`). Everything else goes through an HTTP API Gateway to one API Lambda,
which keeps request throttling and leaves room for WAF. Trade-off: two base URLs for the frontend, and a Function
URL cannot take WAF directly (CloudFront would be needed).

**Uploads are a presigned POST, not PUT** (SPEC section 9 says PUT; behaviour is the same). A POST policy lets S3
itself refuse files over 50 MB or with another content type; a PUT URL cannot enforce a size. The ingest Lambda
checks again: real PDF header, opens, not password-protected, at most 500 pages.

**One container image for all four functions** (`services/Dockerfile`): the `lambda` target serves ingest, insights
and the API with a different handler each; the `chat` target adds the web adapter. One build, one set of
dependencies, and the API shares the query code with chat. The API pays for it with a slower cold start (about 1-2
s) than a zip function would have.

**Ingest flow.** S3 `uploads/<document_id>.pdf` > SQS (visibility 16 min, dead-letter queue after 2 receives) >
Ingest Lambda (3 GB, 15 min, at most 2 at a time) > asynchronous Insights Lambda (1 retry). The upload API creates
the `documents` row first, so the ingest knows the owner from the key alone. A file that cannot be ingested (not a
PDF, too large, scanned) is marked failed with a readable message and not retried; an unexpected error is marked
failed and retried once. A clause in the table of contents that the parser did not find no longer stops ingest: it
is stored as a parse warning for the processing view (SPEC section 10).

**Limits** (`services/shared/limits.py`, table `usage_counters`): per browser 5 uploads and 60 questions a day and 10
documents in total; for the whole app 30 ingests and 1,000 questions a day; API throttled to 20 requests/s. All are
environment variables. Counters live in Neon so every Lambda instance shares them.

**Secrets.** One Secrets Manager secret (`billing-rag/dev`) holds the pooled Neon string and the session key as
JSON. `scripts/put_secret.py` writes it from `.env.local`, so the values never pass through CloudFormation or git.

**No VPC.** The Lambdas reach Neon and Bedrock over TLS on the public endpoints, as local runs do; a VPC would need
a NAT gateway (about $32 a month) for no security gain here.

## Phase 7: frontend

**A static export served by Amplify Hosting** (`web/`, Next.js 16, App Router, TypeScript, Tailwind, shadcn/ui on
Base UI). Every screen depends on the browser's session token, which lives in `localStorage`, so there is nothing
useful to render on a server: the site is plain files (`output: "export"`), with no server-side rendering to pay for or
secure. Consequence: the workspace is `/document?id=...` rather than `/documents/[id]`, because a static export
cannot serve unknown dynamic paths. The API and chat URLs are build-time variables (`NEXT_PUBLIC_API_URL`,
`NEXT_PUBLIC_CHAT_URL`); `amplify.yml` builds the frontend only, because the backend images need Docker to build.

**Screens (SPEC section 10).** Library with drag-and-drop upload, status chips and delete; processing view with the
step tracker, live progress and parse warnings; workspace tabs Summary (executive summary, top 10, clause-by-clause),
Key information (cards by section, each value with its verbatim quote, citation chips and an "Unverified" flag with
the missing numbers), Clauses (tree rebuilt from breadcrumbs, with search) and Ask (streaming answers, suggested
questions, follow-ups, inline citation chips, copy, thumbs up/down, new conversation, earlier conversations). Every
citation opens a side panel with the clause text (the quote marked) and the PDF page (react-pdf) with the cited lines
highlighted. Light, dark and system themes; keyboard focus rings, a skip link, labelled controls and live regions.

**Local development without AWS.** `scripts/dev_server.py` runs the real API handler and chat app against Neon dev,
with `out/local-s3/` in place of S3; an upload there runs the same ingest and insights code the Lambdas run.

**Acceptance** (`web/e2e/full-flow.mjs`, Playwright): upload the sample through the UI, wait for `ready` with no
manual steps (417 s locally), then summary, key information, clauses and a two-turn chat. 20/20 checks pass: every
citation tested (summary, key information, chat) opened the cited printed page with its text highlighted; no
horizontal scroll at phone width; dark mode; no browser errors.

Found by the full flow and fixed:
- The ingest wrote the downloaded PDF to `/tmp`, which exists in Lambda but not on Windows; it now uses the
  system temp folder.
- The final insights call failed once with "0 valid sources": Qwen3 wrote sources as `<K4>`, keeping the angle
  brackets of the prompt's `"<K id>"` placeholder. The ID parser now accepts `<...>` as well as `[...]`; the rerun
  produced the full top 10. This affected Phase 4 too and would have left a document without its summary.
- PDF highlighting matched single short words on unrelated lines; it now marks only whole lines or long fragments.

Known limitations: the first chat token takes 9-20 s locally (database round trips to us-east-1 plus condense and
retrieval); answers can still cite a related but not the best clause (one top-10 item cites Clause 4 for the base
rate alongside the correct Schedule 4 paragraph).

## Phase 8: hardening

**Alarms are part of the stack**, created by the next deploy: an error alarm on each of the four functions and one
on the ingest dead-letter queue, about $0.50 a month. They have no email action and there is no AWS Budgets alert:
the owner chose to keep the deploy free of alert settings, so spend is bounded by the daily limits in the app (30
ingests and 1,000 questions a day for the whole app, plus per-browser limits) and alarm state is read in the
CloudWatch console.

**WAF is not added** (about $6 a month). The HTTP API is throttled (20 requests/s) and every route needs a token and
counts against daily limits; WAF would also not cover the chat Function URL without a CloudFront distribution.

**Security headers** (`customHttp.yml`): a Content-Security-Policy that allows connections only to the site, the
API, the chat URL and S3, plus HSTS, `nosniff`, `frame-ancestors 'none'`. Testing it against the production build
found that the PDF.js worker (`.mjs`) was refused when served as `text/plain`; the file now also sets
`Content-Type: text/javascript` for `.mjs` (Amplify Hosting has served `.mjs` with a generic type before). With
both, the browser check passes 20/20 on the static build.

**Log audit:** the four Lambdas log JSON with IDs, counts, timings and error type names only.

**Evaluation run committed:** `eval/results/2026-10-30.md` (retrieval 95% hit@3; 17/17 insight checks; answers 38
correct, 3 partial, 0 incorrect of 41; conversations 17/17).

The checklist against SPEC section 11 is in [SECURITY.md](SECURITY.md), with the production steps.
