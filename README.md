# IntegraIntake

IntegraIntake is a policy-governed, multi-tenant intake service. It accepts
direct cases, idempotent intake requests, and one signed email-like webhook,
then processes queued jobs through a dedicated PostgreSQL worker.

Tenant profiles define supported fields, documents, and actions. A typed agent
loop proposes work; server-owned policy and approval rules decide whether an
action may run. Durable results and diagnostic events remain source-free.

## Capabilities

- Tenant-scoped direct cases, idempotent queued intake, and safe job-status
  reads through one FastAPI service.
- A signed provider-neutral webhook with bounded text/PDF normalization and
  duplicate-delivery handling.
- Strict YAML tenant profiles for fields, routing, document capabilities, and
  allowed actions.
- A provider-independent `AgentLoop` with typed proposals, bounded execution,
  and an isolated OpenAI-compatible adapter.
- Deterministic policy, typed tools, and tenant-scoped human approvals for
  controlled side effects.
- PostgreSQL leases, structured source-free observability, and provider-free
  regression suites for freight, repair, and language-school scenarios.

## Architecture

~~~text
X-API-Key or signed webhook
        -> FastAPI authentication and validation
        -> server-derived tenant
                |
                +-> direct case -> durable case record
                |
                +-> idempotent AgentJob -> PostgreSQL lease
                                          -> worker runtime
                                          -> profile preflight and AgentLoop
                                          -> policy -> tool, approval, or result
~~~

| Component | Responsibility |
| --- | --- |
| `app/api/`, `app/auth/` | Authenticate service or operator credentials, validate requests, and derive tenant ownership. |
| `app/domain/`, `app/db/` | Own transactions, tenant-scoped repositories, idempotency, jobs, approvals, and audit records. |
| `app/workers/`, `app/runtime/` | Claim leased jobs, reconstruct trusted snapshots, and run agent work outside the HTTP request. |
| `app/agent/`, `app/providers/` | Enforce the bounded typed loop and isolate OpenAI-compatible HTTP and schema validation. |
| `app/policy/`, `app/tools/` | Decide whether proposed actions may run and execute registered tenant-scoped tools. |
| `app/tenants/` | Load and compile trusted YAML profiles against registered actions and document normalizers. |
| `app/observability/`, `evals/` | Emit source-free diagnostics and run deterministic provider-free scenario contracts. |

### Design decisions

- **Tenant authority:** The server derives the tenant from `X-API-Key`, and every repository operation includes that tenant. Service keys cannot act as operators, and operator keys cannot call service intake APIs. The boundary prevents request-controlled ownership.
- **Worker ownership:** FastAPI only accepts or reads work. The worker owns database sessions, provider access, retry state, and the synchronous `AgentLoop`. The process split keeps slow provider calls and session ownership outside HTTP requests.
- **Model authority:** The model returns a strict `AgentProposal`; deterministic policy decides whether tools or approvals may proceed. Confidence and other model signals never grant permission, so server code retains side-effect authority.
- **Bounded execution:** The loop allows at most eight steps and stops a third identical tool call. Jobs use `SKIP LOCKED`, lease heartbeat, and tenant/attempt fencing; backend failures persist `tool_execution_failed` instead of a false success. These limits prevent loops, duplicate execution, and stale worker writes.
- **Content boundary:** Document bytes exist only during normalization. Durable results and diagnostic events omit source text, transcripts, credentials, proposal rationale, and tool payloads. Safe projections preserve operational evidence without copying customer content.

### Technology stack

| Concern | Stack |
| --- | --- |
| API and validation | Python 3.12+, FastAPI, Uvicorn, Pydantic v2 |
| Persistence | PostgreSQL 16, SQLAlchemy 2 async, asyncpg, Alembic |
| Provider, profiles, and documents | httpx, strict JSON Schema, PyYAML, pypdf |
| Testing and operations | pytest, pytest-asyncio, Docker Compose, structlog |

## Quick start

Commands below use PowerShell. You need Python 3.12 or newer, Docker Desktop,
and a local virtual environment.

### 1. Install the project

~~~powershell
Copy-Item .env.example .env
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
~~~

### 2. Start the local stack

~~~powershell
docker compose build api worker
docker compose up -d db
docker compose ps db
docker compose run --rm api alembic upgrade head
docker compose up -d api worker
~~~

Wait until `docker compose ps db` reports the database as healthy, then apply
the migration. Check `http://localhost:8000/health` after the stack starts.
The first release starts new databases from `001_initial_schema`; it cannot
upgrade the retired development chain. Keep that baseline immutable and add
append-only revisions for later schema changes. API and worker startup never
applies migrations.

### 3. Create a demo service key

Use one checked-in profile: `freight-broker`, `repair-service`, or
`language-school`.

~~~powershell
$env:INTEGRA_DEMO_API_KEY = (
  python scripts/seed_demo.py --profile freight-broker
).Trim()
~~~

The command creates or reactivates the tenant, deactivates its older service
keys, and prints one new raw key. PostgreSQL stores only its SHA-256 digest and
a display-safe prefix. Keep the raw key in the current shell or a password
manager; the command cannot print it again.

### 4. Run a live-provider smoke

Set `LLM_API_KEY`, `LLM_BASE_URL`, and `OPENAI_MODEL` in `.env`. Recreate the
worker so Compose passes the changed values into its process:

~~~powershell
docker compose up -d --force-recreate worker
~~~

Then run:

~~~powershell
python scripts/live_provider_smoke.py `
  --profile freight-broker `
  --base-url http://localhost:8000
~~~

The smoke submits synthetic intake and returns a source-free terminal job
projection. The worker is the only Compose service that receives provider
settings. Provider configuration is optional for the API, database, and direct
case endpoint; a live-provider intake needs it.

For a clean database reset, the host-worker alternative, all demo profiles,
approval decisions, and expected evidence, use the
[live provider demo runbook](docs/runbooks/live-provider-demo.md).

## API

All tenant endpoints require `X-API-Key` unless noted otherwise.

| Endpoint | Contract |
| --- | --- |
| `GET /health` | Returns `{"status":"ok","env":"..."}`. No authentication. |
| `POST /v1/cases` | Creates a received case directly. Body: `channel`, `subject`, `body`, optional `customer_id` and `extracted_fields`. Returns `201`. |
| `GET /v1/cases/{case_id}` | Returns a case only for its tenant. Unknown and cross-tenant UUIDs return `404`. |
| `POST /v1/intake` | Requires `Idempotency-Key`; accepts `channel`, `subject`, and `body`; queues one `AgentJob`; returns `202`. |
| `GET /v1/jobs/{job_id}` | Returns a tenant-scoped, read-only, source-free job status and result projection. |
| `POST /v1/inbound/email/webhook` | Verifies a signed email-like envelope, normalizes its optional attachment, and queues one job. Returns `202`. |
| `POST /v1/approvals/{approval_id}/decide` | Requires an active operator key with `approval_decider`; accepts `approve` or `reject` and a non-blank reason. |

### Signed inbound webhook

`POST /v1/inbound/email/webhook` requires `X-API-Key`,
`X-Inbound-Timestamp`, and `X-Inbound-Signature`. The signature format is
`v1=<hex HMAC-SHA256(api_key, "v1." + timestamp + "." + raw_body)>`; the
timestamp must be within five minutes of server time.

The envelope carries `provider_id`, `from_addr`, `subject`, `body`, and an
optional attachment. The server supplies `tenant_id` and `channel=email_webhook`.
Duplicate provider IDs reuse the original job; a changed delivery with the
same ID returns `409`.

Ordinary POST bodies are limited to 1 MiB before JSON parsing. Intake and case
body text is limited to 100,000 characters. Webhook envelopes are limited to
8 MiB, one attachment to 5 MiB, and supported attachment types to `text/plain`
and `application/pdf`.

Use the deterministic local sender for a signed check:

~~~powershell
$env:INBOUND_WEBHOOK_API_KEY = $env:INTEGRA_DEMO_API_KEY
python scripts/send_inbound_webhook.py `
  --url http://localhost:8000/v1/inbound/email/webhook `
  --provider-id local-msg-0001 `
  --from-addr dispatcher@example.test `
  --subject "Rate confirmation" `
  --body "Please review the message."
~~~

Add `--attachment path/to/file.txt` or `--attachment path/to/file.pdf` to
exercise document normalization. A provider integration must authenticate its
delivery and map it through the existing enqueue and idempotency boundary.

## Observability and privacy

The server creates `X-Trace-ID` before authentication and persists it with a
new intake job. Idempotent duplicates reuse that job trace. `X-Correlation-ID`
is optional, bounded, and untrusted.

The API and worker write structured JSON events with safe IDs, closed outcomes,
durations, stop reasons, policy and approval decisions, and provider token
counts when available. Logs and durable summaries are source-free: they omit
request and document content, credentials, authorization and signature
headers, proposals, tool payloads, exception text, and transcripts.

The local scope has no external observability backend, trace table, dashboard,
alerting, or transcript storage. Case and approval audit records remain the
durable business history.

## Configuration

Settings load from `.env` and process environment variables. Keep secrets out
of source control and logs.

| Variable | Default | Purpose |
| --- | --- | --- |
| `DATABASE_URL` | `postgresql+asyncpg://integra:integra@localhost:5432/integra` | Async PostgreSQL connection. |
| `LLM_API_KEY` | empty | Provider credential, held as `SecretStr`. |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible API base. |
| `OPENAI_MODEL` | `gpt-5.4-mini-2026-03-17` | Structured-output model ID. |
| `APP_ENV` | `development` | Value returned by `/health`. |
| `TENANT_PROFILES_DIRECTORY` | `examples` | Directory with validated tenant YAML. |
| `WORKER_POLL_INTERVAL_SECONDS` | `0.5` | Idle worker polling interval. |
| `WORKER_LEASE_SECONDS` | `60` | Job lease duration. |
| `WORKER_MAX_RETRIES` | `4` | Retry limit for recoverable jobs. |
| `APPROVAL_TIMEOUT_SECONDS` | `86400` | Approval deadline in seconds. |

## Testing

Run the provider-free checks:

~~~bash
pytest -q
pytest evals/test_freight.py -q
git diff --check
~~~

Unit and API tests use fakes, dependency overrides, and `httpx` transports.
PostgreSQL integration tests require a reachable `DATABASE_URL`; a skipped test
does not prove database behavior. CI supplies disposable PostgreSQL without
provider credentials.

The version-controlled freight corpus contains 40 core and 10 adversarial
cases. Each case runs once (`k=1`) with scripted typed `AgentProposal` values.
The CI gate requires a 100% pass rate. It does not measure live-model
extraction, classification, or injection-detection quality. An optional
`--freight-eval-report PATH` writes only counts, case IDs, and mismatch codes.

## Scope

IntegraIntake provides local structured observability and deterministic evals.
It excludes external trace or metrics backends, dashboards, alerts, MCP,
real email-provider OAuth or IMAP, outbound delivery, TMS/Odoo, cloud object
storage, and production SaaS integrations.
