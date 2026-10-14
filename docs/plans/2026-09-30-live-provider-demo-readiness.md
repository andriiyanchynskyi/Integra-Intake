# Implementation plan: Live provider demo readiness

> **For agents:** Use the executing-plans skill to implement this plan task by task. Steps use checkbox syntax (`- [ ]`) for tracking. Perform all TDD work in the primary context; do not use subagents to design, write, or revise tests.

**Purpose:** Add a safe, reproducible, opt-in live-provider demonstration with tenant-scoped job visibility, sanitized durable outcomes, profile-aware provisioning, optional worker-only provider configuration, and bounded polling.

**Architecture:** Keep POST intake, the synchronous `AgentLoop`, async worker session ownership, policy, approvals, and observability unchanged. Add a domain-owned safe job projection behind an authenticated GET endpoint; make the worker persist only server-owned summary fields; drive the demo with a provider-blind HTTP script. Fix the existing CI baseline before feature work.

**Key files/stack:** Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy 2 async, PostgreSQL, Docker Compose, httpx, pytest/pytest-asyncio.

**Approved spec:** `docs/specs/2026-09-30-live-provider-demo-readiness-design.md`

---

## File map

| Path | Responsibility |
| --- | --- |
| `tests/test_health.py` | Public route-assembly regression without private `app.routes` assumptions |
| `tests/test_inbound_webhook.py` | Inbound dependency/session contract through the exported router |
| `tests/test_observability.py` | Tracked README product-documentation contract |
| `app/domain/jobs.py` | Closed job states, internal persisted projection, safe read model, and projection logic |
| `app/domain/job_repository.py` | Tenant-scoped job/approval read query |
| `app/api/jobs.py` | Authenticated read-only `GET /v1/jobs/{job_id}` |
| `app/main.py` | Jobs router registration only |
| `tests/test_jobs_api.py` | Read model, tenant isolation, historical-data redaction, and API tests |
| `app/workers/agent_worker.py` | Durable summary allowlist without model `final_response` |
| `tests/test_agent_worker.py` | Durable-result privacy regressions |
| `scripts/seed_demo.py` | Trusted `--profile` selection and credential-kind-safe rotation |
| `tests/test_seed.py` | Profile selection and service/operator rotation contracts |
| `docker-compose.yml` | Optional provider variables on worker only |
| `scripts/live_provider_smoke.py` | Synthetic intake, bounded polling, safe output, exit codes |
| `tests/test_live_provider_smoke.py` | Provider-free smoke-script behavior with fake transport and clock |
| `docs/runbooks/live-provider-demo.md` | Full local runbook and 3-5 minute recording flow |
| `README.md` | Concise current usage and link to the runbook |

No Alembic migration, new dependency, `.env.example` edit, provider SDK, live CI call, UI, or trace store is required.

---

### Task 0: Restore the CI baseline before feature work

**Files:**
- Edit: `tests/test_health.py`
- Edit: `tests/test_inbound_webhook.py`
- Edit: `tests/test_observability.py`
- Conditional edit only if incompatibility is proven: `pyproject.toml`

- [ ] **Step 1: Record the clean dependency versions relevant to the failure**

Run:

```powershell
python -c "import fastapi, starlette, httpx; print({'fastapi': fastapi.__version__, 'starlette': starlette.__version__, 'httpx': httpx.__version__})"
```

Expected: one dictionary containing installed versions. Copy the values into the execution notes; do not add a dependency constraint yet.

- [ ] **Step 2: Replace the route-layout assertion with the public OpenAPI contract**

Change the start of `test_app_assembly_keeps_api_routes_separate_from_worker_and_provider_startup` to:

```python
def test_app_assembly_keeps_api_routes_separate_from_worker_and_provider_startup() -> None:
    """Importing FastAPI must not claim jobs or construct an LLM runtime."""
    route_paths = set(app.openapi()["paths"])
    assert "/v1/cases" in route_paths
    assert "/v1/cases/{case_id}" in route_paths
    assert "/v1/intake" in route_paths
```

Keep the existing source assertions against worker/provider construction.

- [ ] **Step 3: Inspect the inbound route through its exported router**

Import `router as inbound_router` from `app.api.inbound` and replace the failing iterator with:

```python
route = next(
    route
    for route in inbound_router.routes
    if isinstance(route, APIRoute) and route.path == "/inbound/email/webhook"
)
```

Keep the assertions that authentication uses the cached session and the enqueue service requests a fresh session.

- [ ] **Step 4: Restrict the tracked product-doc test to tracked product documentation**

Replace the loop over `("README.md", "AGENTS.md")` with:

```python
text = (root / "README.md").read_text(encoding="utf-8").lower()
for phrase in ("trace", "structured", "json", "source-free", "external"):
    assert phrase in text, phrase
for forbidden in (
    "phase 12",
    "roadmap phase",
    "interview",
    "vacancy",
    "learning project",
    "tutorial",
    "training context",
):
    assert forbidden not in text, forbidden
```

- [ ] **Step 5: Run the exact CI failures**

Run:

```powershell
pytest tests/test_health.py::test_app_assembly_keeps_api_routes_separate_from_worker_and_provider_startup tests/test_inbound_webhook.py::test_inbound_webhook_enqueue_uses_a_fresh_db_session_after_auth_lookup tests/test_observability.py::test_product_docs_describe_local_observability_without_training_context -q
```

Expected: `3 passed`. If public OpenAPI or HTTP behavior fails, stop and diagnose production routing before changing dependency versions.

- [ ] **Step 6: Prove the baseline is green**

Run: `pytest -q`

Expected: all configured tests pass; PostgreSQL tests may skip only because no explicit reachable database is configured. Do not begin Task 1 if this command fails.

---

### Task 1: Define the safe job read model and tenant-scoped query

**Files:**
- Create: `app/domain/jobs.py`
- Edit: `app/domain/job_repository.py`
- Create: `tests/test_jobs_api.py`

- [ ] **Step 1: Write failing projection tests for every safe state and hostile stored data**

Add tests that construct an internal row containing hostile values in `source_snapshot`, profile snapshot, and result:

```python
row = PersistedJobRead(
    job_id=JOB_ID,
    trace_id=TRACE_ID,
    status="succeeded",
    attempt_count=2,
    result={
        "status": "completed",
        "reason": "final",
        "steps": 1,
        "final_response": "secret model rationale",
        "routing_status": "ready",
        "routing_reason": "complete",
        "missing_required_fields": ["contact"],
        "tool_result": {"body": "secret"},
    },
    error_code=None,
    tenant_config_snapshot=resolved_profile.snapshot,
    tenant_config_sha256=resolved_profile.sha256,
    approval_id=None,
    approval_status=None,
    created_at=NOW,
    started_at=NOW,
    finished_at=NOW,
)
safe = project_job_read(row)
assert safe.model_dump(mode="json") == {
    "job_id": str(JOB_ID),
    "trace_id": str(TRACE_ID),
    "scenario_key": "freight_broker",
    "status": "succeeded",
    "attempt_count": 2,
    "routing_status": "ready",
    "routing_reason": "complete",
    "missing_required_fields": ["contact"],
    "approval": None,
    "error_code": None,
    "created_at": NOW.isoformat().replace("+00:00", "Z"),
    "started_at": NOW.isoformat().replace("+00:00", "Z"),
    "finished_at": NOW.isoformat().replace("+00:00", "Z"),
}
assert "secret" not in safe.model_dump_json()
assert "final_response" not in safe.model_dump_json()
```

Parameterize `queued`, `running`, `awaiting_approval`, `succeeded`, `failed`, and `failed_uncertain`; assert approval is populated only from joined approval columns and missing fields are sorted/deduplicated.

- [ ] **Step 2: Run the new projection test and confirm the missing contract**

Run: `pytest tests/test_jobs_api.py -q`

Expected: collection failure because `app.domain.jobs` does not exist.

- [ ] **Step 3: Implement strict domain contracts and an allowlisted projector**

Create `app/domain/jobs.py` with:

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.runtime.profiles import resolve_persisted_profile

JobStatus = Literal[
    "queued", "running", "awaiting_approval", "succeeded", "failed", "failed_uncertain"
]

@dataclass(frozen=True, slots=True)
class PersistedJobRead:
    job_id: UUID
    trace_id: UUID
    status: str
    attempt_count: int
    result: dict[str, object] | None
    error_code: str | None
    tenant_config_snapshot: dict[str, object]
    tenant_config_sha256: str
    approval_id: UUID | None
    approval_status: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

class ApprovalRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    approval_id: UUID
    status: str

class JobRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    job_id: UUID
    trace_id: UUID
    scenario_key: str
    status: JobStatus
    attempt_count: int
    routing_status: str | None = None
    routing_reason: str | None = None
    missing_required_fields: tuple[str, ...] = ()
    approval: ApprovalRead | None = None
    error_code: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
```

Implement `project_job_read(row)` by validating `row.status` against the closed set, resolving the persisted profile hash-first with `resolve_persisted_profile`, reading only the three allowlisted result keys `routing_status`, `routing_reason`, and `missing_required_fields`, and constructing `ApprovalRead` solely from joined columns. Reject malformed persisted profile identity with the existing typed profile error; never return the snapshots.

- [ ] **Step 4: Add the tenant-scoped repository query**

Add `JobRepository.get_read_for_tenant(job_id, tenant_id)` using:

```python
statement = (
    select(AgentJob, Approval.id, Approval.status)
    .outerjoin(
        Approval,
        (Approval.job_id == AgentJob.id)
        & (Approval.tenant_id == AgentJob.tenant_id),
    )
    .where(AgentJob.id == job_id, AgentJob.tenant_id == tenant_id)
)
```

Convert the row into `PersistedJobRead` with `deepcopy` for JSON mappings. Return `None` when absent. Do not add an unscoped lookup.

- [ ] **Step 5: Run domain and repository tests**

Run: `pytest tests/test_jobs_api.py tests/test_job_repository.py -q`

Expected: all tests pass, including an assertion that the compiled SQL contains both job ID and tenant ID predicates.

---

### Task 2: Add the authenticated read-only jobs endpoint

**Files:**
- Create: `app/api/jobs.py`
- Edit: `app/main.py`
- Edit: `tests/test_jobs_api.py`
- Edit: `tests/test_health.py`

- [ ] **Step 1: Write failing API tests for ownership and redaction**

Use dependency overrides for `get_current_tenant` and `get_db_session`. Cover:

```python
response = client.get(f"/v1/jobs/{JOB_ID}", headers={"X-API-Key": SERVICE_KEY})
assert response.status_code == 200
assert set(response.json()) == {
    "job_id", "trace_id", "scenario_key", "status", "attempt_count",
    "routing_status", "routing_reason", "missing_required_fields",
    "approval", "error_code", "created_at", "started_at", "finished_at",
}
assert "final_response" not in response.text
assert "source_snapshot" not in response.text
```

Add unknown and cross-tenant cases that both assert `404` with `{"detail": "Job not found"}`. Snapshot the fake repository state before and after GET to prove no mutation.

- [ ] **Step 2: Run the API tests and confirm the route is absent**

Run: `pytest tests/test_jobs_api.py -q`

Expected: FAIL with `404` for the own-tenant request.

- [ ] **Step 3: Implement and register the jobs router**

Create `app/api/jobs.py`:

```python
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import get_current_tenant
from app.db.models import Tenant
from app.db.session import get_db_session
from app.domain.job_repository import JobRepository
from app.domain.jobs import JobRead, project_job_read
from app.runtime.profiles import TenantProfileUnavailableError

router = APIRouter(prefix="/jobs", tags=["jobs"])

@router.get("/{job_id}", response_model=JobRead)
async def get_job(
    job_id: UUID,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session, use_cache=False),
) -> JobRead:
    row = await JobRepository(session).get_read_for_tenant(job_id, tenant.id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    try:
        return project_job_read(row)
    except (TenantProfileUnavailableError, RuntimeError, TypeError, ValueError) as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Job profile unavailable",
        ) from error
```

Register `jobs_router` under `/v1` in `app/main.py`. Add `/v1/jobs/{job_id}` to the OpenAPI route-assembly assertion.

- [ ] **Step 4: Verify API contracts**

Run: `pytest tests/test_jobs_api.py tests/test_health.py tests/test_auth.py -q`

Expected: all pass; cross-tenant and unknown IDs remain indistinguishable.

---

### Task 3: Remove model rationale from durable job summaries

**Files:**
- Edit: `app/workers/agent_worker.py`
- Edit: `tests/test_agent_worker.py`

- [ ] **Step 1: Change worker tests to require a closed durable summary**

Replace assertions that expect `final_response` with:

```python
assert summary == {
    "status": "completed",
    "reason": "final",
    "steps": 2,
}
assert "final_response" not in summary
assert "done" not in repr(summary)
```

Add a body-job regression where `final_response="secret body excerpt"`; assert the value and key are absent. Keep document preflight expectations for server-owned routing fields, but remove any compatibility expectation that persists a null `final_response` key.

- [ ] **Step 2: Run focused tests and confirm the privacy failure**

Run: `pytest tests/test_agent_worker.py -q`

Expected: FAIL because `_result_summary` still writes `final_response`.

- [ ] **Step 3: Make `_result_summary` an allowlist**

Change the initial summary to:

```python
summary: dict[str, object] = {
    "status": result.status.value,
    "reason": result.reason.value,
    "steps": result.steps,
}
```

Retain only validated server-owned `routing_status`, `routing_reason`, and sorted `missing_required_fields` extraction from the last policy tool result. Delete the document-only nulling branch in `_persist_result`; it is no longer necessary because no job type persists the field.

- [ ] **Step 4: Verify worker, document, approval, and job-read compatibility**

Run:

```powershell
pytest tests/test_agent_worker.py tests/test_documents.py tests/test_approvals.py tests/test_jobs_api.py -q
```

Expected: all pass; no new migration is required.

---

### Task 4: Add trusted profile selection and credential-safe provisioning

**Files:**
- Edit: `scripts/seed_demo.py`
- Edit: `tests/test_seed.py`

- [ ] **Step 1: Write failing CLI/profile and credential-rotation tests**

Add tests that monkeypatch `TenantProfileResolver.resolve` or use the checked-in examples and assert:

```python
assert resolve_profile_slug("freight-broker") == "freight-broker"
assert resolve_profile_slug("repair-service") == "repair-service"
assert resolve_profile_slug("language-school") == "language-school"
with pytest.raises(ValueError, match="trusted tenant profile"):
    resolve_profile_slug("../secret")
```

Add a main-path test passing `profile="repair-service"`. Add a regression that provisions a service key and then an operator key for the same slug and asserts the service key remains active.

- [ ] **Step 2: Run seed tests and confirm missing profile CLI behavior**

Run: `pytest tests/test_seed.py -q`

Expected: FAIL because `resolve_profile_slug` and `main(..., profile=...)` do not exist.

- [ ] **Step 3: Implement trusted profile resolution**

Use `TenantProfileResolver(settings.tenant_profiles_directory).resolve(profile)` and return `resolved.config.slug`; translate `TenantProfileUnavailableError` into a concise `ValueError("trusted tenant profile is unavailable")`. Do not accept a path or tenant UUID.

Change `main` to:

```python
async def main(
    output: Callable[[str], None] = print,
    *,
    operator_ref: str | None = None,
    profile: str = DEMO_TENANT_SLUG,
) -> None:
    slug = resolve_profile_slug(profile)
    async with async_session_factory() as session:
        if operator_ref is None:
            raw_key = await seed_demo_tenant(session, slug)
        else:
            raw_key = await seed_operator_key(session, operator_ref, slug)
    output(raw_key)
```

The operator path requires an existing tenant and rotates only active operator credentials. It must not call `seed_demo_tenant` first.

- [ ] **Step 4: Add the CLI argument**

Add:

```python
parser.add_argument(
    "--profile",
    default=DEMO_TENANT_SLUG,
    choices=("freight-broker", "repair-service", "language-school"),
    help="trusted tenant profile slug",
)
```

Pass `arguments.profile` to `main`. Preserve `--operator-ref`.

- [ ] **Step 5: Verify provisioning**

Run: `pytest tests/test_seed.py tests/test_auth.py tests/test_approvals_api.py -q`

Expected: all pass; service and operator credentials remain separated.

---

### Task 5: Pass optional provider configuration only to the Compose worker

**Files:**
- Edit: `docker-compose.yml`
- Create: `tests/test_demo_compose.py`

- [ ] **Step 1: Write a failing static Compose contract test**

Load `docker-compose.yml` with `yaml.safe_load` and assert:

```python
worker_environment = compose["services"]["worker"]["environment"]
api_environment = compose["services"]["api"]["environment"]
assert worker_environment["LLM_API_KEY"] == "${LLM_API_KEY:-}"
assert worker_environment["LLM_BASE_URL"] == "${LLM_BASE_URL:-https://api.openai.com/v1}"
assert worker_environment["OPENAI_MODEL"] == "${OPENAI_MODEL:-gpt-5.4-mini-2026-03-17}"
assert "LLM_API_KEY" not in api_environment
assert "LLM_BASE_URL" not in api_environment
assert "OPENAI_MODEL" not in api_environment
```

- [ ] **Step 2: Run the test and confirm variables are missing**

Run: `pytest tests/test_demo_compose.py -q`

Expected: FAIL on missing worker `LLM_API_KEY`.

- [ ] **Step 3: Add optional interpolation to the worker service**

Add exactly:

```yaml
      LLM_API_KEY: ${LLM_API_KEY:-}
      LLM_BASE_URL: ${LLM_BASE_URL:-https://api.openai.com/v1}
      OPENAI_MODEL: ${OPENAI_MODEL:-gpt-5.4-mini-2026-03-17}
```

Do not add `env_file`, an override file, API-service provider variables, or a secret value.

- [ ] **Step 4: Verify Compose resolution without a provider key**

Run:

```powershell
pytest tests/test_demo_compose.py -q
docker compose config --quiet
```

Expected: test passes and Compose exits 0 without requiring `LLM_API_KEY`. Do not capture expanded Compose output.

---

### Task 6: Build the provider-blind live smoke script

**Files:**
- Create: `scripts/live_provider_smoke.py`
- Create: `tests/test_live_provider_smoke.py`

- [ ] **Step 1: Write failing tests for payloads, polling, failures, and redaction**

Define a fake `httpx.MockTransport` sequence. Cover all three profile payloads, success, approval, failed, failed-uncertain, timeout, `401`, network error, trace mismatch, scenario mismatch, and missing tenant key. A success test must assert:

```python
result = run_smoke(
    profile="freight-broker",
    base_url="http://testserver",
    api_key=RAW_TENANT_KEY,
    timeout_seconds=5,
    poll_interval_seconds=0,
    client=httpx.Client(transport=transport),
    clock=fake_clock,
    sleep=lambda _: None,
)
assert result["status"] == "succeeded"
assert result["scenario_key"] == "freight_broker"
assert set(result) <= SAFE_JOB_RESPONSE_KEYS
assert RAW_TENANT_KEY not in captured_output
```

Assert POST JSON contains only `channel`, `subject`, and `body`; profile, tenant, document capability, and action never enter the body. Assert headers include a generated `Idempotency-Key` and tenant `X-API-Key`.

- [ ] **Step 2: Run the script tests and confirm the module is absent**

Run: `pytest tests/test_live_provider_smoke.py -q`

Expected: collection failure because `scripts.live_provider_smoke` does not exist.

- [ ] **Step 3: Implement closed scenario fixtures and safe response validation**

Define immutable constants:

```python
PROFILE_SCENARIOS = {
    "freight-broker": ("freight_broker", {"channel": "email", "subject": "Synthetic freight request", "body": "..."}),
    "repair-service": ("repair_service", {"channel": "web", "subject": "Synthetic repair request", "body": "..."}),
    "language-school": ("language_school", {"channel": "web", "subject": "Synthetic course inquiry", "body": "..."}),
}
TERMINAL_STATUSES = frozenset({"succeeded", "failed", "failed_uncertain", "awaiting_approval"})
SAFE_JOB_RESPONSE_KEYS = frozenset({
    "job_id", "trace_id", "scenario_key", "status", "attempt_count",
    "routing_status", "routing_reason", "missing_required_fields",
    "approval", "error_code", "created_at", "started_at", "finished_at",
})
```

Use synthetic complete values from the checked-in profile field catalogs. Validate every GET response has no unknown keys before printing it.

- [ ] **Step 4: Implement intake and bounded polling with injected seams**

`run_smoke` must accept injected `httpx.Client`, monotonic clock, and sleep callable. It posts once, requires `202`, requires `X-Trace-ID`, polls until deadline, verifies returned `trace_id` and `scenario_key`, and returns only on `succeeded` or `awaiting_approval`. Raise a local `SmokeFailure` containing only stable status/code text for all other exits; never include response bodies, headers, request objects, or keys.

- [ ] **Step 5: Implement the CLI without reading the provider key**

Add arguments `--profile`, `--base-url`, `--timeout-seconds`, and `--poll-interval-seconds`. Read only `INTEGRA_DEMO_API_KEY`. Print the validated final mapping with `json.dumps(..., sort_keys=True)`. Exit 0 for `succeeded`/`awaiting_approval` and non-zero for all failures. Do not read or mention the value of `LLM_API_KEY`.

- [ ] **Step 6: Verify the smoke script offline**

Run: `pytest tests/test_live_provider_smoke.py -q`

Expected: all tests pass with no network access and no secret in captured output.

---

### Task 7: Write the runbook and synchronize product documentation

**Files:**
- Create: `docs/runbooks/live-provider-demo.md`
- Edit: `README.md`
- Edit: `tests/test_observability.py`

- [ ] **Step 1: Add failing documentation assertions**

Assert README contains `GET /v1/jobs/{job_id}`, `--profile`, `live_provider_smoke.py`, and a link to `docs/runbooks/live-provider-demo.md`. Assert it says provider variables are worker-only and optional. Assert neither README nor the runbook tells users to print, paste into chat, commit, or upload provider keys.

- [ ] **Step 2: Run the documentation test**

Run: `pytest tests/test_observability.py -q`

Expected: FAIL on missing job endpoint/runbook references.

- [ ] **Step 3: Write the extended runbook**

Include exact PowerShell commands for:

```powershell
Copy-Item .env.example .env
docker compose up -d db
alembic upgrade head
python scripts/seed_demo.py --profile freight-broker
docker compose up -d --force-recreate api worker
python scripts/live_provider_smoke.py --profile freight-broker --base-url http://localhost:8000
```

Also document host-process startup, all three profile choices, signed freight webhook, approval operator provisioning/decision, bounded polling outcomes, provider timeout/invalid-schema interpretation, JSON trace correlation, and cleanup:

```powershell
Remove-Item Env:LLM_API_KEY -ErrorAction SilentlyContinue
Remove-Item Env:INTEGRA_DEMO_API_KEY -ErrorAction SilentlyContinue
```

State that changing `.env` requires container recreation, not image rebuild. Warn against `docker compose config` screenshots/artifacts because interpolation may expose secrets. Do not place any real key in commands or examples.

- [ ] **Step 4: Add the 3-5 minute recording flow**

Document the exact sequence: offline three-scenario proof, stack startup, freight provisioning, one body live smoke, safe job/trace output, one signed freight webhook, and explanation that repair/education are selectable but live behavior is not deterministic.

- [ ] **Step 5: Update README to current behavior only**

Add the job GET endpoint to the API table, profile-aware seed examples, worker-only optional Compose provider settings, the smoke command, and the runbook link. Remove any claim that a live provider outcome is deterministic. Do not mention project phases, training, vacancies, or interviews.

- [ ] **Step 6: Verify documentation contracts**

Run:

```powershell
pytest tests/test_observability.py tests/test_health.py -q
rg -n -i "phase 12|roadmap phase|interview|vacancy|tutorial|training context|learning project" README.md docs/runbooks/live-provider-demo.md
```

Expected: tests pass and `rg` returns no matches.

---

### Task 8: Run full compatibility, privacy, and demo-readiness verification

**Files:** Verify every file changed in Tasks 0-7.

- [ ] **Step 1: Run focused feature suites**

Run:

```powershell
pytest tests/test_jobs_api.py tests/test_agent_worker.py tests/test_seed.py tests/test_demo_compose.py tests/test_live_provider_smoke.py -q
```

Expected: all pass.

- [ ] **Step 2: Run the full configured suite in a fresh local temp directory**

Run:

```powershell
$demoTemp = '.pytest-tmp-live-provider-demo-final'
if (Test-Path -LiteralPath $demoTemp) { throw "Temporary test path already exists: $demoTemp" }
pytest -q --basetemp $demoTemp
```

Expected: all collected unit/API tests pass. Report PostgreSQL skips separately.

- [ ] **Step 3: Run deterministic scenario gates**

Run:

```powershell
pytest tests/test_scenario_contracts.py -q
pytest evals/test_freight.py -q
```

Expected: repair/education contracts pass and exactly 50 freight cases pass.

- [ ] **Step 4: Verify migration topology and optional PostgreSQL evidence**

Run:

```powershell
alembic heads
pytest tests/test_migrations.py -q
```

Expected: one head, `007_phase12_job_trace_id`, and migration tests pass. If a reachable explicit `DATABASE_URL` exists, additionally run the repository/API PostgreSQL suites; otherwise state that live PostgreSQL verification was not performed.

- [ ] **Step 5: Run privacy and scope scans**

Run:

```powershell
rg -n "final_response" app/workers tests/test_agent_worker.py app/api/jobs.py app/domain/jobs.py
rg -n "source_snapshot|tenant_config_snapshot|rationale|tool_result|pending_action|decision_reason|LLM_API_KEY" app/api/jobs.py app/domain/jobs.py scripts/live_provider_smoke.py
rg -n "LLM_API_KEY|LLM_BASE_URL|OPENAI_MODEL" docker-compose.yml .github/workflows/ci.yml
```

Expected: worker matches are in-memory contracts/tests only, job API/domain matches are internal projection inputs or explicit prohibitions rather than response fields, smoke script has no provider-key access, and provider variables occur only in the Compose worker—not GitHub Actions.

- [ ] **Step 6: Verify whitespace and final scope**

Run:

```powershell
git diff --check
git status --short
git diff --stat
git diff -- app scripts tests docker-compose.yml README.md .github pyproject.toml
```

Expected: whitespace check exits 0; no migration, new dependency, CI secret, live network test, UI, exporter, transcript store, parser, outbound integration, or unrelated change is present.

- [ ] **Step 7: Perform the manual live smoke only when the user has configured local credentials**

Follow `docs/runbooks/live-provider-demo.md`. Never ask the user to paste a key into chat. Capture only: command exit status, safe job response, trace ID, and source-free event names/outcomes. Do not make live execution a prerequisite when credentials are unavailable; report it as not performed.

---

### Task 9: Run the configured final quality checks

**Files:** Verify the complete implementation diff. No formatter, linter, or static type checker is configured in `pyproject.toml`; do not invent one.

- [ ] **Step 1: Run the complete pytest suite once more after all documentation and script changes**

Run:

```powershell
$qualityTemp = '.pytest-tmp-live-provider-demo-quality'
if (Test-Path -LiteralPath $qualityTemp) { throw "Temporary test path already exists: $qualityTemp" }
pytest -q --basetemp $qualityTemp
```

Expected: all collected tests pass. List PostgreSQL skips separately and do not describe them as integration evidence.

- [ ] **Step 2: Run the deterministic freight gate**

Run: `pytest evals/test_freight.py -q`

Expected: exactly 50 cases pass.

- [ ] **Step 3: Check migration topology and whitespace**

Run:

```powershell
alembic heads
git diff --check
```

Expected: Alembic reports only `007_phase12_job_trace_id (head)` and the whitespace check exits 0.

- [ ] **Step 4: Inspect final scope**

Run:

```powershell
git status --short
git diff --stat
git diff -- app scripts tests docker-compose.yml README.md .github pyproject.toml
```

Expected: only files named by this plan plus pre-existing user changes are present; no dependency, migration, CI-secret, external-service, UI, transcript-store, or unrelated change was introduced.

---

### Task 10: Review the complete implementation before corrections

**Files:** Review the full diff plus the approved spec and this plan.

No project-local code-review subagent exists. Use the available `code-review` skill.

- [ ] **Step 1: Call the `code-review` skill on all changes made by this plan**

Provide the reviewer:

- the complete diff;
- `docs/specs/2026-09-30-live-provider-demo-readiness-design.md`;
- `docs/plans/2026-09-30-live-provider-demo-readiness.md`;
- the Task 9 verification output;
- this review focus:

```text
Review live provider demo readiness for: the Task 0 CI baseline fix without
private FastAPI route-layout assumptions; tenant-scoped read-only job lookup;
404 equivalence for missing and cross-tenant jobs; hash-first scenario identity;
strict allowlisted job responses; historical and new final_response redaction;
no source/profile snapshot, proposal rationale, extracted value, tool payload,
operator reason, exception text, or credential disclosure; unchanged POST
contracts; optional provider environment only on the worker; profile-aware
provisioning with service/operator rotation separation; provider-blind bounded
smoke polling; no live network or secret in pytest/CI; unchanged synchronous
loop, async session ownership, policy authority, approval behavior, retries,
side-effect uncertainty, deterministic scenario contracts, and 50-case freight
gate; no migration, new dependency, UI, exporter, transcript store, parser,
outbound integration, or scope creep. Require file-and-line evidence for every
finding.
```

- [ ] **Step 2: Present findings before changing code**

Do not make corrections automatically. Report findings ordered by severity with file/line evidence, then discuss proposed corrections with the user. If there are no findings, report that explicitly together with the Task 9 verification evidence.

---

## Coverage map

| Specification requirement | Plan task |
| --- | --- |
| Pre-existing CI failure as Task 0 | 0 |
| Tenant-scoped safe job visibility | 1, 2 |
| Historical row redaction | 1, 2 |
| Durable `final_response` removal | 3 |
| Profile-aware provisioning | 4 |
| Operator key preserves service key | 4 |
| Optional worker-only provider environment | 5 |
| Reusable provider-blind smoke script | 6 |
| Three body scenarios, one live invocation | 4, 6, 7 |
| Freight webhook remains separate | 7 |
| Bounded polling and failure behavior | 6 |
| Approval demonstration | 7 |
| Source-free output and secret handling | 1-7 |
| Extended runbook and short recording flow | 7 |
| Deterministic provider-free CI/evals | 0, 8, 9 |
| Final configured quality checks | 9 |
| Evidence-based review before corrections | 10 |
| No TDD subagents | plan header and execution instruction |
| Rollback/no migration/no new dependency | 1-10 |
