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

## 8. Run live freight intake checks

PowerShell environment variables belong to one terminal process. With Option A,
run the smoke in the terminal that captured the freight service key. With
Option B, open a second PowerShell terminal, activate the virtual environment,
and repeat the freight seed command from Section 6 there before running:

```powershell
python scripts/live_provider_smoke.py --profile freight-broker --base-url http://localhost:8000
```

The command submits synthetic body intake through the real API, polls
`GET /v1/jobs/{job_id}`, and prints a human-readable source-free report. Use
`--json` for the same allowlisted report as one machine-readable object.
`succeeded` and `awaiting_approval` are valid live outcomes. Provider output can
vary, so the report shows the path actually observed and prints
`NOT OBSERVED` when a nullable policy or tool field was not reached.

The named cases below use the real HTTP endpoint and the checked-in synthetic
fixtures. Run them with one freight service key in the current shell.

### `body-complete`

```powershell
python scripts/live_provider_smoke.py `
  --profile freight-broker `
  --demo-case body-complete `
  --base-url http://localhost:8000
```

This checks complete body intake and prints the policy/tool path. Add
`--expect-path tool` when the demonstration must fail if the live proposal does
not request a tool.

### `body-incomplete`

```powershell
python scripts/live_provider_smoke.py `
  --profile freight-broker `
  --demo-case body-incomplete `
  --base-url http://localhost:8000
```

The case omits `contact` and expects `routing_status=awaiting_input` with
`missing_required_fields=["contact"]`.

### `webhook-body`

```powershell
python scripts/live_provider_smoke.py `
  --profile freight-broker `
  --demo-case webhook-body `
  --base-url http://localhost:8000
```

The command signs the exact JSON bytes and sends them to
`POST /v1/inbound/email/webhook`. A `202` response and matching job trace prove
that the real signature and tenant boundary accepted the delivery.

### `webhook-document-txt`

```powershell
python scripts/live_provider_smoke.py `
  --profile freight-broker `
  --demo-case webhook-document-txt `
  --base-url http://localhost:8000
```

This sends `evals/fixtures/docs/01-complete-en.txt` as a signed webhook
attachment and runs document normalization in the worker.

The same real webhook path can use transport selection directly:

```powershell
python scripts/live_provider_smoke.py `
  --profile freight-broker `
  --transport webhook `
  --attachment evals/fixtures/docs/01-complete-en.txt `
  --base-url http://localhost:8000
```

### `webhook-document-pdf`

```powershell
python scripts/live_provider_smoke.py `
  --profile freight-broker `
  --demo-case webhook-document-pdf `
  --base-url http://localhost:8000
```

This sends `evals/fixtures/docs/03-complete.pdf` through the same real webhook
path.

### `webhook-document-malformed`

```powershell
python scripts/live_provider_smoke.py `
  --profile freight-broker `
  --demo-case webhook-document-malformed `
  --base-url http://localhost:8000
```

The expected result is `status=succeeded`,
`routing_reason=document_unreadable`, `agent_steps=0`, an empty execution path,
and no approval. Deterministic preflight stops the job before provider, tool,
or approval construction.

### `webhook-duplicate`

```powershell
python scripts/live_provider_smoke.py `
  --profile freight-broker `
  --demo-case webhook-duplicate `
  --base-url http://localhost:8000
```

The command sends the same provider delivery twice and requires the second
response to reuse the first `job_id` and `trace_id`.

### `webhook-conflict`

```powershell
python scripts/live_provider_smoke.py `
  --profile freight-broker `
  --demo-case webhook-conflict `
  --base-url http://localhost:8000
```

The second delivery keeps the provider ID but changes the body. The command
requires `409` and polls the original job separately.

### Custom body input

Create a synthetic JSON input file and send it through the body endpoint:

```powershell
@'
{
  "transport": "intake",
  "channel": "email",
  "subject": "Custom freight request",
  "body": "Origin: Kyiv; destination: Lviv; equipment: dry_van; contact: ops@example.test"
}
'@ | Set-Content -LiteralPath .\custom-body.json -Encoding utf8

python scripts/live_provider_smoke.py `
  --profile freight-broker `
  --input-file .\custom-body.json `
  --base-url http://localhost:8000
```

The input file is read locally and its body is never printed by the smoke.

### Custom webhook input with a file

The attachment remains a local sender-side file. The server receives it only
through the signed webhook request:

```powershell
@'
{
  "transport": "webhook",
  "provider_id": "custom-live-message-1",
  "from_addr": "dispatcher@example.test",
  "subject": "Custom rate confirmation",
  "body": "Attached synthetic confirmation",
  "attachment_path": "evals/fixtures/docs/01-complete-en.txt"
}
'@ | Set-Content -LiteralPath .\custom-webhook.json -Encoding utf8

python scripts/live_provider_smoke.py `
  --profile freight-broker `
  --input-file .\custom-webhook.json `
  --base-url http://localhost:8000
```

Use `--json` when saving the source-free result for automation. Do not save the
input file, raw document, credential, signature, provider response, or worker
logs with shared demo evidence.

### What an ideal verification proves

An ideal verification proves the governed path from a clean database to a
durable, source-free result. It does not depend on one model phrase or one
classification. A model can produce plausible text while the wrong schema,
tenant, profile, worker, policy, or persistence boundary handles the job.

Use the evidence below to explain each step to another operator:

| Check | Evidence to capture | Why the check matters |
| --- | --- | --- |
| Clean release state | `alembic current` reports `001_initial_schema (head)` after a fresh database reset. | The demo cannot rely on retained tables, old revisions, or manual schema changes. |
| API readiness | `/health` responds after the migration completes. | The API connects to the database that received the release schema. |
| Tenant and profile identity | The seed command uses the selected profile, and the job result reports its expected `scenario_key`. | The server derives tenant access from the API key and applies the checked-in profile rather than request-supplied ownership. |
| Worker ownership | One selected worker claims the job and emits source-free lifecycle events for the same trace. | Provider access, leases, and result persistence remain in the worker boundary. |
| End-to-end job lifecycle | The smoke result contains a `job_id`, `trace_id`, terminal status, routing fields, and any approval identity. | Another operator can connect the accepted request, worker execution, and durable result without reading source content. |
| Policy and approval gate | An `awaiting_approval` job has no executed side effect before an authorized operator decides it. | Server policy, not model confidence, controls high-impact actions. |
| Privacy boundary | Commands, logs, and captured output contain no API keys, source text, documents, transcripts, proposal rationale, or tool payloads. | The demo can be shared as diagnostic evidence without exposing customer data or credentials. |

A good result can be `succeeded`, `awaiting_approval`, or a stable expected
failure. `succeeded` proves that the permitted path reached a durable result.
`awaiting_approval` proves that the policy gate stopped execution before the
side effect. A stable failure such as `provider_unavailable` proves that the
system records a bounded failure without leaking provider or source details.

Record the profile slug, `scenario_key`, `job_id`, `trace_id`, terminal status,
routing outcome, approval ID when present, and stable error code when present.
Those fields let another person repeat the run and compare outcomes. Do not
record the request body, document text, credentials, or provider response.

The live commands prove the real HTTP, PostgreSQL, worker, configured provider,
and observed policy/tool path. The deterministic freight gate proves all 50
version-controlled freight cases, including policy branches and adversarial
inputs that one live provider proposal may not reach.

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

## 11. Optional low-level freight webhook sender

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

## 12. direct-case

The direct case endpoint is synchronous. It authenticates the tenant, writes one
case, and returns `201`; it does not call the worker or provider. Verify the
created record through the tenant-scoped GET endpoint:

```powershell
$headers = @{ "X-API-Key" = $env:INTEGRA_DEMO_API_KEY }
$caseBody = @{
  channel = "email"
  subject = "Synthetic direct case"
  body = "Synthetic freight request"
} | ConvertTo-Json
$createdCase = Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/v1/cases" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $caseBody
Invoke-RestMethod `
  -Method Get `
  -Uri "http://localhost:8000/v1/cases/$($createdCase.id)" `
  -Headers $headers
```

Capture only the case ID, status, tenant-safe channel, and timestamp in shared
evidence. The direct response contains the submitted synthetic body by design;
do not use production data in this demo.

## 13. approval-decision

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

Use `decision = "reject"` to demonstrate the other terminal decision. If no
live smoke result has `status=awaiting_approval` and an approval ID, record
`SKIPPED: no pending approval`; do not fabricate an ID.

## 14. Deterministic freight gate

Run the provider-free matrix after the live checks:

```powershell
pytest evals/test_freight.py -q
```

The command runs 50 cases from
`evals/datasets/freight.v1.yaml`. Six cases exercise signed-webhook semantics,
and document cases resolve files from `evals/fixtures/docs/manifest.yaml`.
The eval runner verifies HMAC and maps webhook messages through application
services in memory. It does not call the live HTTP endpoint, PostgreSQL,
worker, or provider; treat it as deterministic behavioral evidence.

## 15. Troubleshooting

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
- `expected_path_not_observed` means the provider completed through a different
  safe path than the optional `--expect-path` assertion. Review the printed
  execution path before rerunning with a stricter expectation.
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

## 16. Stop the demo and clear secrets

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
