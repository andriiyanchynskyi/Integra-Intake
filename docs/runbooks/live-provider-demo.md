# Live provider demo runbook

This runbook starts a disposable local stack and runs one synthetic live-provider
intake. The worker owns provider access. The API and PostgreSQL do not receive
the provider key. Demo output contains IDs, status, routing fields, and stable
error codes. It does not contain source text, transcripts, proposal rationale,
tool payloads, or credentials.

## 1. Prerequisites

Use PowerShell from the repository root. Install Python 3.12 or newer, Docker
Desktop, and the project extras in an activated virtual environment:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

The local Compose database is disposable. The runbook uses the checked-in
`freight-broker`, `repair-service`, and `language-school` profiles. It sends
synthetic intake only.

## 2. Configure the provider

Create the local environment file:

```powershell
Copy-Item .env.example .env
```

Edit `.env` and set `LLM_API_KEY`, `LLM_BASE_URL`, and `OPENAI_MODEL` for the
OpenAI-compatible provider. Keep the values in `.env` or the current shell.


Compose passes provider settings to the worker service. The API and PostgreSQL
do not need `LLM_API_KEY`. An empty key is accepted at process startup; a live
job then follows the stable provider failure path.

## 3. Recreate the local database

The next command removes the local Compose PostgreSQL volume and all demo data.
Run it only when the database is disposable:

```powershell
docker compose down -v
```

## 4. Build images and apply the release schema

Build the API and worker images, start PostgreSQL, and wait for the health
check:

```powershell
docker compose build api worker
docker compose up -d db
docker compose ps db
```

Apply the release baseline from a one-shot API container and verify its
revision:

```powershell
docker compose run --rm api alembic upgrade head
docker compose run --rm api alembic current
docker compose run --rm api alembic heads
```

The current and head revisions must report `001_initial_schema`. This first
release targets databases created from scratch. Treat that revision as
immutable; future schema changes add append-only revisions.

## 5. Start and verify the API

Start the API after the database is healthy and the baseline is applied:

```powershell
docker compose up -d api
Invoke-RestMethod http://localhost:8000/health
```

The response must report `status = ok`. The API startup command does not apply
migrations.

## 6. Provision the freight demo tenant

Seed the freight profile and keep its one-time service credential in the
current shell:

```powershell
$env:INTEGRA_DEMO_API_KEY = (python scripts/seed_demo.py --profile freight-broker).Trim()
```

The seed command validates the checked-in profile, reactivates or creates the
tenant, deactivates older service keys for that tenant, and returns one raw key
for the current shell. PostgreSQL stores only its digest and safe prefix.

## 7. Start one worker

Use one worker mode for the demo. Both modes use the same database lease and
tenant fencing rules, but one mode keeps ownership and logs clear.

### Option A: Compose worker

```powershell
docker compose up -d worker
docker compose logs --tail 100 worker
```

The Compose worker uses `db` as the PostgreSQL host and receives provider
settings from Compose interpolation.

### Option B: Worker from PowerShell

Stop the Compose worker first, set the host database URL, and run the worker in
the activated virtual environment:

```powershell
docker compose stop worker
$env:DATABASE_URL = "postgresql+asyncpg://integra:integra@localhost:5432/integra"
python -m app.workers
```

Option B occupies its terminal. Keep it open while the API processes jobs. The
host worker reads provider settings from `.env` and uses `localhost` for
PostgreSQL. If `.env` changes, recreate the Compose worker with
`docker compose up -d --force-recreate worker`; an image rebuild is not needed.

## 8. Run the freight body smoke

PowerShell environment variables belong to one terminal process. With Option A,
run the smoke in the terminal that captured the freight service key. With
Option B, open a second PowerShell terminal, activate the virtual environment,
and repeat the freight seed command from Section 6 there before running:

```powershell
python scripts/live_provider_smoke.py --profile freight-broker --base-url http://localhost:8000
```

The smoke submits synthetic body intake, polls `GET /v1/jobs/{job_id}`, and
returns the job ID, server trace ID, scenario identity, routing summary,
approval identity, timestamps, and stable error codes. `succeeded` and
`awaiting_approval` are valid live outcomes. Provider output can vary, so use
the safe lifecycle and identity fields as the check.

## 9. Run the repair-service scenario

Keep the same API, database, and worker. Replace the service key and run only
the profile-specific commands:

```powershell
$env:INTEGRA_DEMO_API_KEY = (python scripts/seed_demo.py --profile repair-service).Trim()
python scripts/live_provider_smoke.py --profile repair-service --base-url http://localhost:8000
```

The safe scenario identity is `repair_service`. Its synthetic
`service_request` contains customer name, contact, issue, and asset type. The
profile declares `documents: {}`, so it does not support the freight
attachment or webhook demonstration.

## 10. Run the language-school scenario

Keep the same stack and replace the service key again:

```powershell
$env:INTEGRA_DEMO_API_KEY = (python scripts/seed_demo.py --profile language-school).Trim()
python scripts/live_provider_smoke.py --profile language-school --base-url http://localhost:8000
```

The safe scenario identity is `language_school`. Its synthetic
`course_inquiry` contains student name, contact, and language. The profile
declares `documents: {}`, so it does not support the freight attachment or
webhook demonstration.

## 11. Optional freight webhook

Only `freight-broker` has the registered document capability. Set the webhook
sender key to the current freight service key and send one synthetic signed
message:

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

The sender signs the raw request locally and never writes the key. The server
maps the message through the existing tenant-scoped intake and idempotency
boundary. A repeated provider ID reuses the existing job; a changed payload
returns a conflict.

## 12. Optional approval decision

Run this flow only when the safe smoke result has `status` set to
`awaiting_approval`. Copy its safe approval identity into `$approvalId` in the
current shell. Provision a separate operator key for the freight tenant:

```powershell
$env:INTEGRA_DEMO_OPERATOR_KEY = (
  python scripts/seed_demo.py --profile freight-broker --operator-ref demo-operator
).Trim()
$headers = @{ "X-API-Key" = $env:INTEGRA_DEMO_OPERATOR_KEY }
$decision = @{ decision = "approve"; reason = "Local demo approval" } | ConvertTo-Json
Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/v1/approvals/$approvalId/decide" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $decision
```

Read the job again after the decision. Service keys cannot decide approvals;
operator keys cannot submit service intake. The reason stays in the local
decision request and is not copied into diagnostic events or demo artifacts.

## 13. Troubleshooting

- A missing key, invalid key, unavailable model, or unreachable base URL follows
  the stable provider error path. The worker may schedule bounded retries. Do
  not put the key into a command, log, or report.
- Invalid structured output gets one validation retry. A second invalid
  response ends with a stable failure code.
- If the migration is missing, run `docker compose run --rm api alembic current`
  and apply `docker compose run --rm api alembic upgrade head` before starting
  jobs.
- If the worker is stopped, start exactly one selected mode and inspect
  source-free worker logs. Running both modes can coordinate through leases, but
  this demo uses one owner.
- A profile/key mismatch produces a tenant-scoped result for the profile that
  was seeded. Re-seed the intended profile and replace
  `INTEGRA_DEMO_API_KEY` in the current shell.
- A polling timeout does not delete or replay the job. Inspect the safe job
  state and JSON event lines, then check worker health and lease expiry.
- `failed_uncertain` means the system will not replay a possible side effect
  automatically. A backend tool failure persists `tool_execution_failed` and
  never produces `succeeded`.
- An unreadable or unavailable document terminates deterministic preflight
  without provider, tool, or approval construction.
- Ordinary POST bodies are limited to 1 MiB before JSON parsing. Intake and case
  body text is limited to 100,000 characters. Webhook envelopes are limited to
  8 MiB and one attachment to 5 MiB.
- A job lease fences stale attempts. The old attempt cannot persist its result
  after another worker owns the job.

Logs, job reads, durable summaries, approval records, and optional reports stay
source-free. They contain no raw document bytes, base64, source text,
transcripts, proposal rationale, tool payloads, credentials, or exception text.

## 14. Stop the demo and clear secrets

Stop a host worker with `Ctrl+C` in its terminal. Then stop the Compose stack
without removing its volume:

```powershell
docker compose down
Remove-Item Env:LLM_API_KEY -ErrorAction SilentlyContinue
Remove-Item Env:INTEGRA_DEMO_API_KEY -ErrorAction SilentlyContinue
Remove-Item Env:INTEGRA_DEMO_OPERATOR_KEY -ErrorAction SilentlyContinue
Remove-Item Env:INBOUND_WEBHOOK_API_KEY -ErrorAction SilentlyContinue
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
```

Delete `.env` only as an explicit final cleanup step. Removing it also removes
the operator's local provider configuration:

```powershell
Remove-Item -LiteralPath .env -ErrorAction SilentlyContinue
```
