# IntegraIntake

IntegraIntake is a policy-governed, multi-tenant intake engine. It accepts
structured case requests and one email-like inbound webhook, normalizes
bounded freight documents, queues tenant-scoped jobs, and runs a synchronous
agent loop behind an asynchronous PostgreSQL worker.

The service uses one provider-neutral signed webhook for email-like messages.
It does not include IMAP, Gmail OAuth, a production email SaaS integration, or
outbound delivery.

## Data flow

~~~text
API key / signed webhook
        |
        v
server-derived tenant
        |
        +--> POST /v1/cases       -> direct received case
        |
        +--> POST /v1/intake      -> idempotent AgentJob
        |
        +--> POST /v1/inbound/email/webhook
                              -> strict InboundMessage
                              -> bounded document normalization
                              -> idempotent AgentJob
                                      |
                                      v
                              PostgreSQL worker
                                      |
                                      v
                              synchronous AgentLoop
                                      |
                                      v
                         structured proposal -> policy/tools/approval
~~~

Authentication always supplies the tenant from a server-side API-key lookup.
Request JSON cannot select a tenant. Every repository operation includes the
authenticated tenant ID, and cross-tenant case reads behave like missing
resources (404).

## Implemented capabilities

- FastAPI application with /health, OpenAPI, Docker Compose, PostgreSQL,
  Alembic, and SQLAlchemy 2 async sessions.
- Tenant-scoped API keys. Raw keys are printed once by the local seed command;
  PostgreSQL stores only a SHA-256 digest and a display-safe prefix.
- Strict YAML tenant profiles with safe loading, field catalogs, action policy,
  and deterministic routing. Policy precedence remains code-owned.
- Direct case creation and retrieval through /v1/cases.
- An idempotent PostgreSQL intake boundary through /v1/intake, leased worker
  jobs, bounded retries, and safe durable error codes.
- A provider-independent synchronous AgentLoop and an isolated
  OpenAI-compatible adapter with strict AgentProposal validation and one
  validation retry.
- Tool and policy boundaries with typed structured outputs. confidence and
  other model signals never authorize an action.
- Human approval decisions with tenant-scoped operator credentials, expiry,
  append-only approval events, and execute-once behavior.
- Freight document normalization for bounded text/plain and
  application/pdf rate confirmations. Normalized snapshots contain extracted
  text or a typed extraction error plus a SHA-256 digest; raw document bytes
  are not persisted or sent to the model.
- A signed email-like inbound webhook at
  POST /v1/inbound/email/webhook. It accepts one bounded attachment and
  reuses the existing intake/job flow.

## API

All tenant endpoints use X-API-Key unless noted otherwise.

| Endpoint | Contract |
|---|---|
| GET /health | Returns {"status":"ok","env":"..."}. No authentication. |
| POST /v1/cases | Creates a received case directly. Body: channel, subject, body, optional customer_id and extracted_fields. Returns 201. |
| GET /v1/cases/{case_id} | Returns a case only when it belongs to the authenticated tenant. Returns 404 for an unknown or cross-tenant UUID. |
| POST /v1/intake | Accepts channel, subject, and body with a required Idempotency-Key; queues one AgentJob and returns 202 with job_id and status="queued". |
| POST /v1/inbound/email/webhook | Accepts a signed email-like JSON envelope, normalizes its optional attachment, and queues one job. Returns 202 with the same accepted response shape as /v1/intake. |
| POST /v1/approvals/{approval_id}/decide | Requires an active operator API key with the approval_decider capability. Body: decision (approve or reject) and a non-blank reason. |

### Signed inbound webhook

The inbound wire envelope is intentionally closed:

~~~json
{
  "provider_id": "local-msg-0001",
  "from_addr": "dispatcher@example.test",
  "subject": "Rate confirmation",
  "body": "Please review the attached confirmation.",
  "attachments": [
    {
      "media_type": "text/plain",
      "content_base64": "b3JpZ2luOiBDaGljYWdv..."
    }
  ]
}
~~~

Required headers are X-API-Key, X-Inbound-Timestamp, and
X-Inbound-Signature. The signature is
v1=<hex HMAC-SHA256(api_key, "v1." + timestamp + "." + raw_body)> and the
timestamp must be within five minutes of server time. Invalid signatures,
expired timestamp replays, malformed JSON, unsupported media types, and
oversized bodies return sanitized errors without exposing parser or
authentication details. A duplicate delivery with the same provider ID is
handled by idempotency and returns the existing job.

The server supplies tenant_id and channel=email_webhook; neither can be
provided by the payload. provider_id is namespaced as
email_webhook:<provider_id> at the existing tenant-scoped idempotency
boundary. Repeating the same provider delivery returns the existing job. The
same ID with a changed body, sender, subject, or normalized document returns
409.

The adapter accepts at most one attachment. Supported media types are
text/plain and application/pdf; each document is limited to 5 MiB, the
webhook body to 8 MiB, and the text body to 100,000 characters. Attachment
bytes exist only while the document normalizer runs. Jobs, transcripts, audit
events, approvals, and model-facing snapshots contain no raw binary or
base64; they retain only the bounded normalized document result and digest.

### Local webhook sender

scripts/send_inbound_webhook.py is a deterministic local sender. It uses the
same signing helper as the adapter, accepts .txt or .pdf, and never writes
the API key. It replaces a real provider for local checks:

~~~powershell
$env:INBOUND_WEBHOOK_API_KEY = $env:INTEGRA_DEMO_API_KEY
python scripts/send_inbound_webhook.py --url http://localhost:8000/v1/inbound/email/webhook --provider-id local-msg-0001 --from-addr dispatcher@example.test --subject "Rate confirmation" --body "Please review the message."
~~~

Add `--attachment path/to/file.txt` or `--attachment path/to/file.pdf` to
exercise document normalization with a local fixture.

The adapter contract is provider-neutral: a future provider integration only
needs to authenticate its delivery and map it to the frozen internal
InboundMessage(tenant_id, channel, from_addr, subject, body, attachments,
provider_id) contract. It must not bypass the existing enqueue service.

## Agent and worker guarantees

- AgentLoop is synchronous and provider-independent. FastAPI does not call
  the loop inline; the worker owns the async event loop and SQLAlchemy sessions.
- The loop enforces MAX_STEPS = 8 and stops after a third identical tool
  call. Tool results preserve explicit None values in the typed transcript.
- Proposals are validated structured data. Markdown parsing and LangChain are
  not part of the protocol.
- PostgreSQL jobs use leases and bounded retry delays (0.5, 1, 2, 4 seconds
  plus jitter, up to the configured retry limit).
- Tenant profile routing and action policy are deterministic. LLM confidence,
  priority, and injection flags are informational inputs to policy, never
  permission to perform a side effect.

## Quick start

### 1. Configure and install

~~~bash
cp .env.example .env
python -m venv .venv
# Windows: .venv/Scripts/activate
# Linux/macOS: source .venv/bin/activate
pip install -e ".[dev]"
~~~

### 2. Start PostgreSQL and migrate

~~~bash
docker compose up -d db
alembic upgrade head
~~~

### 3. Provision the local demo tenant

~~~bash
python scripts/seed_demo.py
~~~

The command creates or reuses the demo tenant, deactivates older demo keys,
and prints one new raw API key. Store it in a password manager or the current
shell session. Do not commit it or expect the command to print it again.

~~~bash
export INTEGRA_DEMO_API_KEY='<printed-key>'
# PowerShell: $env:INTEGRA_DEMO_API_KEY = '<printed-key>'
~~~

### 4. Run the API and worker

~~~bash
uvicorn app.main:app --reload
python -m app.workers
~~~

The worker is a separate process. The API accepts work and returns; it does
not wait for an LLM call.

### 5. Create a direct case

~~~bash
curl -X POST http://localhost:8000/v1/cases \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $INTEGRA_DEMO_API_KEY" \
  -d '{"channel":"email","subject":"Password reset","body":"Please reset my account password."}'
~~~

### Full local stack

~~~bash
docker compose up --build
~~~

## Configuration

Settings are loaded from .env and process environment variables. Keep
secrets out of source control and logs.

| Variable | Default | Purpose |
|---|---|---|
| DATABASE_URL | postgresql+asyncpg://integra:integra@localhost:5432/integra | Async PostgreSQL connection. |
| LLM_API_KEY | empty | Provider credential, held as SecretStr. |
| LLM_BASE_URL | https://api.openai.com/v1 | OpenAI-compatible API base. |
| OPENAI_MODEL | gpt-5.4-mini-2026-03-17 | Structured-output model ID. |
| APP_ENV | development | Value returned by /health. |
| TENANT_PROFILES_DIRECTORY | examples | Directory containing validated tenant YAML profiles. |
| WORKER_POLL_INTERVAL_SECONDS | 0.5 | Idle worker polling interval. |
| WORKER_LEASE_SECONDS | 60 | Job lease duration. |
| WORKER_CONCURRENCY | 1 | Worker concurrency setting. |
| WORKER_MAX_RETRIES | 4 | Maximum retry policy for recoverable jobs. |
| APPROVAL_TIMEOUT_SECONDS | 86400 | Approval deadline used by the runtime. |

## Testing

Run the deterministic suite without a provider API or email credentials:

~~~bash
pytest -q
~~~

Unit and API tests use fakes, dependency overrides, and httpx transports.
Webhook tests cover signing, timestamp expiry, replay rejection, strict
payload validation, limits, duplicate delivery, idempotency conflicts,
tenant isolation, attachment privacy, normalization, and worker hand-off.
PostgreSQL integration tests run only when an explicit reachable
DATABASE_URL is supplied; otherwise they skip instead of pretending to be
database evidence.

Before handoff, also run:

~~~bash
git diff --check
~~~

## Current boundaries

The service does not include eval CI, production observability and traces, MCP,
Linux/systemd operations, real email-provider OAuth or IMAP, outbound delivery,
TMS/Odoo, cloud object storage, or production SaaS integrations.
