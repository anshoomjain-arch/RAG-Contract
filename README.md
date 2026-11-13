# Billing RAG

Upload a contract PDF and the app reads it for you. It extracts the key information (parties, dates, fees,
termination rights, liability caps and so on), writes a summary, and lets you ask questions in a chat. Every answer
and every extracted value cites the clause and page it came from, and clicking a citation opens the PDF at that
page with the text highlighted.

- **What the system does and how it is designed:** [SPEC.md](SPEC.md)
- **Why it was built this way, and what was measured:** [docs/DECISIONS.md](docs/DECISIONS.md)
- **Security checklist and production steps:** [docs/SECURITY.md](docs/SECURITY.md)

---

## How it works in one minute

```
Browser (static Next.js site on Amplify Hosting)
  |  1. POST /session                 -> gets an anonymous signed token (no login)
  |  2. POST /documents/upload-url    -> presigned S3 upload
  |  3. upload the PDF ------------------> S3 --> SQS --> Ingest Lambda
  |                                                     parse > chunk > tag > embed > store in Neon
  |                                                         |
  |                                                         v
  |                                                     Insights Lambda (key information + summary)
  |  4. poll status, read insights    -> API Lambda (HTTP API Gateway) --> Neon
  |  5. ask questions (streamed)      -> Chat Lambda (Function URL) --> Neon search --> Bedrock --> stream back
```

- **Backend:** Python on AWS Lambda. All four functions (ingest, insights, API, chat) are built from one Docker
  image and deployed with AWS Amplify Gen 2 (`amplify/backend.ts`, which is plain AWS CDK).
- **Database:** Neon Postgres with `pgvector` for vector search and built-in full-text search.
- **AI models:** Amazon Bedrock. Qwen3-32B writes tags, insights and answers; Titan Text Embeddings V2 turns text
  into vectors for search.
- **Frontend:** Next.js 16 (static export), Tailwind and shadcn/ui, hosted on AWS Amplify Hosting.
- **No login.** Each browser gets an anonymous identity and only sees its own documents. Daily limits keep costs
  bounded (see [Costs and limits](#costs-and-limits)).

---

## Repository map

| Path | What is in it |
|---|---|
| `services/ingest/` | Reads the PDF: `parser/` (text, clause tree, definitions, TOC check), `tables/`, `chunker/`, `tagger/` (rules + LLM), `store.py` (embeddings into Neon), `handler.py` (Lambda entry) |
| `services/insights/` | Key information and summary: `generate.py` (map-reduce prompts), `verify.py` (checks numbers and dates against the cited text) |
| `services/chat/` | Q&A: `retrieve.py` (hybrid search), `answer.py` (prompting and citations), `service.py` (the pipeline), `app.py` (streaming web app) |
| `services/api/` | REST API Lambda: routes in `handler.py`, SQL in `queries.py` |
| `services/shared/` | Shared code: `db.py` (all database access), `llm.py` and `embeddings.py` (Bedrock), `session.py` (anonymous tokens), `limits.py` (daily limits), `secrets.py` |
| `services/Dockerfile` | The one image for all four Lambdas |
| `amplify/backend.ts` | All AWS resources: S3, SQS, Lambdas, API Gateway, Function URL, alarms |
| `db/migrations/` | SQL schema, applied in number order by `scripts/migrate.py` |
| `web/` | Frontend. `src/app/` pages, `src/components/` UI, `src/lib/api.ts` backend calls, `e2e/full-flow.mjs` browser test |
| `scripts/` | `db_check.py`, `migrate.py`, `put_secret.py`, `dev_server.py` (local backend), `check_deploy.py`, `serve_static.py` |
| `tests/` | Unit tests (`pytest`) |
| `eval/` | Golden questions and expected answers for the sample contract, scoring scripts, saved results |
| `amplify.yml`, `customHttp.yml` | Frontend build settings and security headers for Amplify Hosting |

---

## What you need

| Item | Notes |
|---|---|
| **Python 3.13** | `py -3.13 --version` on Windows |
| **Node.js 20.9 or newer** | Tested with Node 24 |
| **Git** | |
| **Docker Desktop** | Only for deploying: the deploy builds the Lambda image. It must be running |
| **AWS CLI v2** with a profile for the AWS account you will use | `aws configure --profile <name>` |
| **Bedrock model access** in **ap-southeast-2** for `qwen.qwen3-32b-v1:0` and `amazon.titan-embed-text-v2:0` | AWS console > Amazon Bedrock > Model access. Without it every model call fails |
| **Neon account** | Free tier is enough for development |
| **A contract PDF** | Must have a text layer (exported from Word, not scanned). The sample contract used by the evals is not in the repo because it is confidential: get it from the project owner and put it in `samples/` |

Everything runs in **ap-southeast-2** (Bedrock, Lambda, S3). The examples below use Windows PowerShell; on macOS or Linux
use `source .venv/bin/activate` and `export NAME=value` instead.

---

## Setup from scratch

### 1. Clone and install

```powershell
git clone <repo url> billing-rag
cd billing-rag
py -3.13 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
npm install                        # AWS deploy tools (Amplify, CDK) for the repo root
cd web; npm install; cd ..         # frontend
```

If npm reports that install scripts were blocked for `esbuild` or `@parcel/watcher`, approve them
(`npm install-scripts approve esbuild`): the deploy needs esbuild.

### 2. Create the Neon database

1. In the Neon console click **New Project**, name it `billing-rag`, region **AWS ap-southeast-2**.
2. Neon creates a `main` branch. Keep it for production. Open **Branches > New branch**, name it `dev`, parent
   `main`. **Use `dev` for all development.**
3. Open **Connect**, choose the `dev` branch, and copy two connection strings:
   - **Pooled** (host contains `-pooler`): used by the app.
   - **Direct** (no `-pooler`): used by migrations.

### 3. Create `.env.local`

Copy `.env.example` to `.env.local` in the repo root and fill it in. `.env.local` is git-ignored: **never commit it,
never paste it into chat or tickets.**

```
DATABASE_URL=<pooled string>
DATABASE_URL_DIRECT=<direct string>
AWS_PROFILE=<your AWS CLI profile>
BEDROCK_REGION=ap-southeast-2
LLM_MODEL_ID=qwen.qwen3-32b-v1:0
EMBEDDING_MODEL_ID=amazon.titan-embed-text-v2:0
EMBEDDING_DIMENSIONS=1024
```

`AWS_PROFILE` decides which AWS account local runs bill Bedrock calls to. Check it with
`aws sts get-caller-identity --profile <your profile>`.

### 4. Check the database and create the tables

```powershell
python scripts/db_check.py      # enables pgvector; must end with "OK: Neon is ready."
python scripts/migrate.py       # applies db/migrations/*.sql (each file once)
```

### 5. Run the tests

```powershell
python -m pytest -q
```

All tests should pass. The tests in `tests/ingest/test_sample_contract.py` are skipped unless a PDF is in `samples/`.

---

## Run it locally

### The whole app in the browser (no AWS deploy needed)

`scripts/dev_server.py` runs the real API and chat code on your machine, with a folder (`out/local-s3/`) instead of
S3. It still uses Neon `dev` and Bedrock, so uploads cost money (about $0.17 each).

Create `web/.env.local`:

```
NEXT_PUBLIC_API_URL=http://localhost:8000/api
NEXT_PUBLIC_CHAT_URL=http://localhost:8000/chat
```

Then, in two terminals:

```powershell
python scripts/dev_server.py             # terminal 1: backend on http://localhost:8000
cd web; npm run dev                      # terminal 2: frontend on http://localhost:3000
```

Open http://localhost:3000 and upload a PDF. Processing takes about 4-7 minutes locally (faster in AWS).

Browser test of the full flow (Playwright: upload, wait for ready, check every tab and a chat):

```powershell
cd web
npx playwright install chromium          # once
node e2e/full-flow.mjs "..\samples\<contract>.pdf" ..\out\screens
```

### Each stage on its own (for debugging)

These command-line tools run one stage at a time. They need `$env:PYTHONPATH = "services"`.

```powershell
$env:PYTHONPATH = "services"

# 1. Parse only (free): prints the clause tree check, page offset, definitions, tables and warnings
python -m ingest.cli "samples\<contract>.pdf" --dry-run

# 2. Parse, chunk and tag (LLM tagging costs about $0.06)
python -m ingest.cli "samples\<contract>.pdf" --tag --llm-tags all --out out/chunks.jsonl

# 3. Embed and store in Neon (reruns reuse embeddings)
python -m ingest.load "samples\<contract>.pdf" --chunks out/chunks.jsonl

# 4. Key information and summary (about $0.10)
python -m insights.run --show

# 5. Chat in the terminal
python -m chat.cli
python -m chat.cli --ask "What is the base rate?" --ask "And for Gold Sites?" -v
```

The parser CLI is the main debugging tool: if a contract processes badly, start with `--dry-run`. Its exit code is
1 when something serious is found (for example a clause in the table of contents that is missing from the body).
Command-line runs store documents under the owner `local-dev`; browser uploads use the browser's own identity.

---

## Deploy to AWS

The backend is deployed as an Amplify Gen 2 "sandbox" stack in ap-southeast-2. Use the same AWS profile for every
command in this section.

```powershell
$env:AWS_PROFILE = "<your profile>"
$env:AWS_REGION = "ap-southeast-2"
aws sts get-caller-identity                     # confirm this is the account you mean to use

# 1. Store the database URL and a session-signing key in Secrets Manager (values are never printed).
#    Reads DATABASE_URL from .env.local. The secret must be in the SAME account as the deploy.
python scripts/put_secret.py

# 2. Once per account and region: prepare the account for CDK deploys
npx cdk bootstrap "aws://$(aws sts get-caller-identity --query Account --output text)/ap-southeast-2"

# 3. Build the Docker image and deploy everything (Docker Desktop must be running; about 10 minutes the first time)
$env:ALLOWED_ORIGINS = "https://main.<app id>.amplifyapp.com"     # your site URL; leave unset on the very first deploy
npx ampx sandbox --once
```

The deploy writes `amplify_outputs.json` (git-ignored) with the two backend URLs under `custom`:
`apiUrl` and `chatUrl`.

**`ALLOWED_ORIGINS`** is the website allowed to call the backend from a browser. On the first deploy the site
does not exist yet, so leave it unset (it defaults to `*`, any site). Once the site is hosted, **set it on every
deploy**: if you forget, the backend quietly goes back to accepting every site.

Check the deployed backend end to end (uploads a PDF through the real URLs, waits for `ready`, asks one question;
about $0.17):

```powershell
python scripts/check_deploy.py "samples\<contract>.pdf"
```

To remove the backend: `npx ampx sandbox delete`. This also deletes every PDF in the S3 bucket. The data in
Neon stays.

---

## Host the frontend

The frontend is a set of static files built by `npm run build` in `web/` into `web/out/`. The backend URLs are
baked in at build time from `NEXT_PUBLIC_API_URL` and `NEXT_PUBLIC_CHAT_URL`.

### Option A: connect the GitHub repository (rebuilds on every push)

1. AWS console > Amplify > **Create new app** > GitHub > pick this repository and the `main` branch.
2. Amplify finds `amplify.yml` (frontend build) and `customHttp.yml` (security headers) automatically.
3. Under **Environment variables** add `NEXT_PUBLIC_API_URL` and `NEXT_PUBLIC_CHAT_URL` with the `apiUrl` and
   `chatUrl` values from `amplify_outputs.json`.
4. Deploy. The site is at `https://main.<app id>.amplifyapp.com`.

### Option B: upload a zip (no GitHub connection)

```powershell
cd web
$env:NEXT_PUBLIC_API_URL  = "<apiUrl>"
$env:NEXT_PUBLIC_CHAT_URL = "<chatUrl>"
npm run build
cd ..
python -c "import zipfile,pathlib; r=pathlib.Path('web/out'); z=zipfile.ZipFile('site.zip','w',zipfile.ZIP_DEFLATED); [z.write(p,p.relative_to(r).as_posix()) for p in r.rglob('*') if p.is_file()]; z.close()"

# First time only: create the app (with the security headers) and a branch
$id = aws amplify create-app --name billing-rag --platform WEB --custom-headers (Get-Content customHttp.yml -Raw) --query app.appId --output text
aws amplify create-branch --app-id $id --branch-name main --stage PRODUCTION

# Every time: upload and publish
$d = aws amplify create-deployment --app-id $id --branch-name main | ConvertFrom-Json
Invoke-WebRequest -Uri $d.zipUploadUrl -Method Put -InFile site.zip -ContentType "application/zip"
aws amplify start-deployment --app-id $id --branch-name main --job-id $d.jobId
```

The zip must hold the *contents* of `web/out` with forward-slash paths, which is why it is made with Python
rather than `Compress-Archive`. The Amplify app does not have to be in the same region as the backend.

### After the site exists

Redeploy the backend with `ALLOWED_ORIGINS` set to the site URL (see [Deploy to AWS](#deploy-to-aws)), then run
the browser test against the live site:

```powershell
cd web
$env:WEB_URL = "https://main.<app id>.amplifyapp.com"
node e2e/full-flow.mjs "..\samples\<contract>.pdf" ..\out\screens
```

All checks should pass.

---

## Evaluation

The `eval/` folder measures quality on the sample contract. The expected answers are written for that one contract,
so the evals only make sense with it.

```powershell
python eval/run_eval.py                                # retrieval: is the right clause in the top 3? (free)
python eval/check_insights.py                          # key information against expected values (free)
python eval/run_eval.py --mode off --answers           # answer quality, graded by the LLM (about $0.05)
```

They read the document loaded under owner `local-dev` (step 3 of [Each stage on its own](#each-stage-on-its-own-for-debugging)).
Latest results are in `eval/results/` (retrieval 95% in the top 3; 17/17 insight checks; 38 of 41 answers correct
and 3 partial; 17/17 conversation turns).

---

## Costs and limits

| What | Approximate cost |
|---|---|
| Processing one ~130-page contract | $0.17 (Bedrock) |
| One chat question | $0.002 |
| Idle AWS costs | about $1 a month (5 CloudWatch alarms, 1 secret, image storage). Lambdas cost nothing when unused |

Because there is no login, the app limits use (`services/shared/limits.py`, counted in the `usage_counters` table):

| Limit | Default |
|---|---|
| Uploads per browser per day | 5 |
| Questions per browser per day | 60 |
| Documents per browser (total) | 10 |
| Uploads per day, whole app | 30 |
| Questions per day, whole app | 1,000 |
| File size / pages | 50 MB / 500 pages |

To change a limit, add an environment variable with its name (for example `UPLOADS_PER_DAY: "20"`) to the `env`
object in `amplify/backend.ts` and redeploy the backend. There is no AWS Budgets alert; set one in the AWS Billing console if you want an email when spend rises.

---

## Troubleshooting

| Problem | Cause and fix |
|---|---|
| `db_check.py`: `DATABASE_URL_DIRECT is not set` | `.env.local` is missing, misnamed, or not in the repo root |
| Authentication or SSL error from Neon | Wrong or reset password: copy a fresh string from Neon **Connect**, keep `?sslmode=require` |
| First query after a pause is slow | Neon suspends idle databases; the first query wakes it. Normal |
| `AccessDeniedException` from Bedrock | Model access not enabled for that model in ap-southeast-2, or `AWS_PROFILE` points at another account |
| Deployed site: every request fails with HTTP 500 at `/session` | The secret is missing in the account you deployed to. Run `put_secret.py` with the same `AWS_PROFILE` as the deploy. The API's CloudWatch log shows `ResourceNotFoundException` |
| Browser console: CORS error | `ALLOWED_ORIGINS` does not match the site URL exactly (scheme, no trailing slash). Redeploy the backend with the right value |
| `npx ampx sandbox` fails while building the image | Docker Desktop is not running |
| `cdk bootstrap` error during deploy | The account/region has not been bootstrapped: run step 2 of the deploy |
| Amplify: "maximum number of apps" | The account's Amplify app quota is full in that region. Create the app in another region (the frontend region does not matter) or ask AWS for a higher quota |
| Upload shows `failed` | The message says why: not a PDF, too large, password-protected, or scanned (no text layer). Scanned PDFs are not supported yet |
| You hit a daily limit while testing | Counts reset at midnight UTC. A private browser window is a new identity for the per-browser limits |

---

## Rules for working on this project

- All database access goes through `services/shared/db.py`.
- Never print, log or commit connection strings or secrets. Logs carry IDs, counts and timings only, never contract
  text, prompts or answers.
- Develop against the Neon `dev` branch, not `main`.
- Nothing may be specific to one contract (names, page numbers, offsets): everything is detected from the PDF.
- Run `python -m pytest -q` (and `npx eslint` in `web/`) before committing.
- Record design decisions with real trade-offs in `docs/DECISIONS.md`.
