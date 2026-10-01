# Live provider demo runbook

This runbook is a manual, local-only smoke path. It uses synthetic intake and
never places a provider credential in source control, chat, test output,
screenshots, or uploaded artifacts. Provider behavior is non-deterministic;
the acceptance signal is the safe lifecycle and trace, not a particular model
phrase, tool call, or routing choice.

## Prerequisites

- Python 3.12+ with the project installed: `pip install -e "[dev]"`.
- Docker Desktop with a reachable PostgreSQL container.
- A provider API key available only in the local shell or `.env`.
- A local tenant service key printed once by `seed_demo.py`.

## Local environment

Create the local settings file from the secret-free template:

```powershell
Copy-Item .env.example .env
```

Set `LLM_API_KEY`, `LLM_BASE_URL`, and `OPENAI_MODEL` in `.env` locally. Do not
commit `.env`, print its contents, or capture expanded Compose configuration.
The API does not need the provider credential; the worker does.

## Start the stack

Containerized database with host processes:

```powershell
docker compose up -d db
alembic upgrade head
python scripts/seed_demo.py --profile freight-broker
uvicorn app.main:app --reload
python -m app.workers
```

Full Compose stack:

```powershell
docker compose up -d --build
alembic upgrade head
```

If `.env` changes while containers are running, recreate the worker. An image
rebuild is not needed for an environment-only change:

```powershell
docker compose up -d --force-recreate worker
```

Compose starts without a provider key. A provider-dependent job without one
follows the normal bounded retry and stable failure path.

## Provision a profile

The trusted profiles are selected by slug, not by tenant ID or file path:

```powershell
python scripts/seed_demo.py --profile freight-broker
python scripts/seed_demo.py --profile repair-service
python scripts/seed_demo.py --profile language-school
```

The default is `freight-broker`. Save the one-time service key in the current
shell only:

```powershell
$env:INTEGRA_DEMO_API_KEY = '<locally captured key>'
```

For approval demonstration, provision an operator key for an already seeded
profile. This does not rotate the service key:

```powershell
python scripts/seed_demo.py --profile freight-broker --operator-ref demo-operator
```

## Body-intake live smoke

Run one profile per invocation:

```powershell
python scripts/live_provider_smoke.py `
  --profile freight-broker `
  --base-url http://localhost:8000
```

The script sends only synthetic `channel`, `subject`, and `body`, captures the
server `job_id` and `X-Trace-ID`, and polls the tenant-scoped read model with a
bounded timeout. It accepts `succeeded` and `awaiting_approval`; failures print
only stable error codes and the last safe state. It never reads `LLM_API_KEY`,
starts services, edits the database, or makes an approval decision.

The read endpoint is:

```text
GET /v1/jobs/{job_id}
```

It returns status, trace/scenario identity, routing summary, approval identity,
timestamps, and stable error codes only. It never returns source, snapshots,
transcripts, proposal rationale, tool payloads, operator reasons, credentials,
or exception text.

## Freight signed webhook capability

The webhook is a separate freight document capability, not a universal body
intake guarantee:

```powershell
$env:INBOUND_WEBHOOK_API_KEY = $env:INTEGRA_DEMO_API_KEY
python scripts/send_inbound_webhook.py `
  --url http://localhost:8000/v1/inbound/email/webhook `
  --provider-id local-msg-0001 `
  --from-addr dispatcher@example.test `
  --subject "Rate confirmation" `
  --body "Please review the message." `
  --attachment evals/fixtures/docs/01-complete-en.txt
```

The sender signs the request locally and does not write the key. Duplicate
provider deliveries use the existing tenant-scoped idempotency boundary.

## Approval path

When the job read model reports `awaiting_approval`, inspect its `approval`
identity and use the existing operator endpoint with the operator key. The
reason is entered only in that local request body; it is not copied into the
diagnostic timeline or demo artifacts. Read the job again after the decision.

## Failure interpretation

- `provider_unavailable` or a timeout: the worker may schedule bounded retries.
- Invalid structured output: the adapter performs one controlled validation
  retry, then exposes a stable failure.
- `failed_uncertain`: do not replay a side effect automatically.
- Poll timeout: preserve the job and inspect the last source-free event lines.
- Unreadable document: deterministic preflight completes without provider,
  tool, or approval construction.

## Runtime safety contract

Service keys cannot act as operators, and operator keys cannot call service
intake APIs. Inactive tenant keys are rejected. Ordinary POST bodies are
limited to 1 MiB before JSON parsing; intake and case body text is limited to
100,000 characters. Webhook envelopes retain an 8 MiB limit and one 5 MiB
attachment limit.

One worker process handles one job at a time. Multiple worker containers may
scale horizontally through PostgreSQL `SKIP LOCKED`, lease heartbeat, and
tenant/attempt fencing. A lost lease cannot persist the old attempt's result.
Backend tool failures persist `tool_execution_failed`, never `succeeded`.

The approval-status constraint has one migration owner. Recreate the disposable
local PostgreSQL volume before validating a clean migration upgrade after
schema-chain changes.

Do not infer live-model quality from one response. Offline repair, education,
and freight contracts plus the 50-case freight gate are the deterministic
evidence.

## 3-5 minute recording flow

1. Show the provider-free repair, education, and freight contract checks.
2. Start PostgreSQL, API, and worker and apply migrations.
3. Provision `freight-broker` and run one body live smoke.
4. Show `job_id`, `trace_id`, the safe read model, and matching JSON event
   names/outcomes.
5. Send one signed freight webhook to show the separate document capability.
6. Explain that `--profile` can select repair or language school, while their
   contract suites do not make live LLM results deterministic.

## Cleanup

After the recording, clear shell variables and remove local files without
including their values in command output:

```powershell
Remove-Item Env:LLM_API_KEY -ErrorAction SilentlyContinue
Remove-Item Env:INTEGRA_DEMO_API_KEY -ErrorAction SilentlyContinue
Remove-Item Env:INBOUND_WEBHOOK_API_KEY -ErrorAction SilentlyContinue
Remove-Item -LiteralPath .env -ErrorAction SilentlyContinue
```
