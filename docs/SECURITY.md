# Security and operations checklist (SPEC section 11)

Status as of 2026-10-30. "Deploy" means the item is in `amplify/backend.ts` and takes effect when the stack is
deployed (`npx ampx sandbox --once`); everything else is enforced in code and covered by tests or the browser check.

| SPEC section 11 item | Status | Where |
|---|---|---|
| No login, but every call carries a verified anonymous identity | Done | `services/shared/session.py`: HMAC-signed token; API and chat verify it before using the ID (tests: forged and tampered tokens rejected; a client-sent `owner_id` is ignored) |
| Each browser sees only its own documents, in code and in SQL | Done | Every query in `services/api/queries.py`, `chat/`, `insights/store.py` filters on `owner_id`; another browser's document returns 404, never 403 |
| Never expose a list of all documents | Done | `GET /documents` is scoped to the caller; there is no admin route |
| Optional password gate while testing | Not set | Amplify Hosting branch password; switch on in the Amplify console if wanted |
| Per-browser daily limits on uploads and questions; cap on documents per browser | Done | `services/shared/limits.py`: 5 uploads and 60 questions a day, 10 documents per browser (env-configurable) |
| Global daily cap on ingests and LLM calls | Done | 30 ingests and 1,000 questions a day for the whole app |
| AWS Budgets alarm with email | Removed (owner's decision, 2026-10-30) | Spend is bounded by the daily limits above. A budget can be added in the AWS Billing console at any time |
| WAF rate limiting on the API | Not added | About $6 a month. The HTTP API is throttled instead (20 requests/s, burst 40, free). A Function URL cannot take WAF without CloudFront in front |
| S3 private, short-lived presigned URLs, encryption on | Done | Bucket blocks public access, enforces TLS, SSE-S3; upload and PDF links expire in 5 minutes; uploads are a presigned POST that limits size (50 MB) and type |
| Do not log document text or prompts | Done | Lambda logs are JSON with IDs, counts, timings and error type names only (audited 2026-10-30). Unhandled exceptions print a stack trace; none of the code formats contract text into an exception message |
| Secrets only via Secrets Manager; `.env.example`, no real keys in git | Done | One secret (`billing-rag/dev`) written by `scripts/put_secret.py` without printing values; `.env.local`, `web/.env.local`, `out/` are git-ignored; commits are scanned for connection strings |
| Rate-limit chat per browser identity | Done | Questions count against the daily limit before any LLM call |
| Cap upload size and page count; validate a real PDF | Done | 50 MB (S3 policy and ingest), 500 pages, `%PDF-` header, opens with PyMuPDF, not password-protected; a rejected file fails with a readable message and is not retried |
| CloudWatch alarms for Lambda errors and ingest failures | Deploy | Five alarms: errors on each of the four functions, and any message in the ingest dead-letter queue. No email: their state shows in the CloudWatch console |
| A failed ingest sets `status=failed` with a readable error | Done | `services/ingest/handler.py`; shown in the library and the workspace |
| Dev and prod separation | Partly | Dev: Amplify sandbox + Neon `dev` branch. Prod steps below; not created yet |
| Cache LLM outputs, batch embeddings, cheap model for tagging | Done | Insight map step cached per document; embeddings reused when text is unchanged and sent in parallel batches; one model (Qwen3-32B) for all LLM work, so no cheaper tier is in use |

Also in place:

- **Browser security headers** (`customHttp.yml`, served by Amplify Hosting): Content-Security-Policy limited to the
  site, the API, the chat URL and S3; `frame-ancestors 'none'`; HSTS; `nosniff`; strict referrer. The browser check
  passes against the production build served with these headers (`scripts/serve_static.py`).
- **CORS**: `ALLOWED_ORIGINS` (API, chat URL, bucket) is `*` until the Amplify Hosting URL exists; set it to that URL
  and redeploy. Every call still needs a valid token.
- **Least-privilege IAM**: each function can call only the two Bedrock model IDs, read the one secret, and touch
  only this bucket (the API: put, get, delete; ingest: get).
- **No VPC or NAT**: Lambdas reach Neon and Bedrock over TLS.

## Production (not created yet)

1. Neon: use the project's default branch (`main`) for production; run `python scripts/migrate.py` against it with
   `DATABASE_URL_DIRECT` set to that branch.
2. `python scripts/put_secret.py --name billing-rag/prod` with `DATABASE_URL` set to the `main` pooled string.
3. Deploy with `BILLING_RAG_SECRET=billing-rag/prod` and `ALLOWED_ORIGINS=<production site URL>` set (a separate Amplify backend environment, or `npx ampx pipeline-deploy` from CI with Docker available).
4. Point the production branch of Amplify Hosting at that deploy's `apiUrl` and `chatUrl`.

## Known gaps

- Anyone can mint new browser identities, so per-browser limits slow an abuser but do not stop one; the global daily caps
  and API throttling bound the cost.
- Tokens do not expire; there is no way to revoke one browser short of rotating `SESSION_KEY` (which signs out
  everyone).
- `script-src 'unsafe-inline'` is required by the Next.js static export.
