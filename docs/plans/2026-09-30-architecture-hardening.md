# Implementation plan: Architecture hardening

> **For agents:** Use the executing-plans skill to implement this plan task by
> task. Track each checkbox. Use the unit-test-writer skill directly in the
> main agent for every test step. Do not delegate TDD or code review to a
> subagent.

**Purpose:** Repair the migration chain, credential boundaries, worker lease
ownership, failure propagation, request limits, and demo documentation found
in the end-to-end architecture review.

**Architecture:** Keep each worker process sequential and coordinate multiple
processes with PostgreSQL leases, heartbeat renewal, and attempt fencing.
Represent tool failure with typed loop data, enforce service/operator
separation at authentication, and reject oversized bodies before FastAPI JSON
parsing. Preserve source-free job and observability projections.

**Key files/stack:** Python 3.12+, FastAPI/ASGI, SQLAlchemy async, PostgreSQL,
Alembic, Pydantic v2, pytest, Docker Compose, GitHub Actions.

**Specification:**
`docs/specs/2026-09-30-architecture-hardening-design.md`

---

## File map

- `alembic/versions/004_phase8_approval_workflow.py`: stop creating the status
  constraint owned by revision `006`.
- `.github/workflows/ci.yml`: run PostgreSQL migration and integration coverage
  in CI without provider credentials.
- `app/auth/api_keys.py`: enforce service principal and active-tenant filters.
- `scripts/seed_demo.py`: reactivate a seeded service tenant and reject inactive
  tenants for operator provisioning.
- `app/agent/models.py`, `app/agent/loop.py`: type controlled tool stops and
  tool failures; compare complete tool calls.
- `app/tools/executor.py`: return a typed failure without leaking backend
  exceptions.
- `app/domain/job_repository.py`: require tenant/attempt fencing, renew leases,
  and raise on stale transitions.
- `app/workers/agent_worker.py`: maintain the lease heartbeat and persist stable
  tool failure codes.
- `app/observability/events.py`: add job route, lease-loss outcome, and worker
  error codes.
- `app/api/body_limit.py`: enforce path-aware ASGI body limits before parsing.
- `app/main.py`: install body limiting inside observability middleware.
- `app/domain/schemas.py`, `app/domain/intake.py`: enforce field and serialized
  JSON limits.
- `.gitignore`, `README.md`, `docs/runbooks/live-provider-demo.md`: track and
  document the implemented runtime contract.
- Existing `tests/test_*.py` files: add focused regressions next to their
  current fixtures and fakes.

## Specification coverage

- Migration consistency and CI database evidence: Task 0.
- Credential separation and tenant lifecycle: Task 1.
- Typed tool failure and public error projection: Task 2.
- Complete repeated-call identity: Task 3.
- Tenant and attempt fencing: Task 4.
- Sequential worker heartbeat and concurrency-setting removal: Task 5.
- Transport and domain input bounds: Task 6.
- Job-route observability, tracked runbook, and runtime documentation: Task 7.
- Clean database, integration tests, offline suite, and freight gate: Task 8.
- Security and architecture review: Task 9.

## Task 0: Repair the migration chain and make CI execute it

**Files:**
- Edit: `alembic/versions/004_phase8_approval_workflow.py`
- Edit: `.github/workflows/ci.yml`
- Edit: `tests/test_migrations.py`

- [ ] **Step 1: Add the migration ownership regression**

Use the unit-test-writer skill in the main agent. Add paths for revisions `004`
and `006`, then add this source-level regression to `tests/test_migrations.py`:

```python
APPROVAL_WORKFLOW_MIGRATION_PATH = (
    PROJECT_ROOT / "alembic" / "versions" / "004_phase8_approval_workflow.py"
)
APPROVAL_STATUS_MIGRATION_PATH = (
    PROJECT_ROOT / "alembic" / "versions" / "006_phase8_approval_status_check.py"
)


def test_approval_status_constraint_has_one_migration_owner() -> None:
    workflow = APPROVAL_WORKFLOW_MIGRATION_PATH.read_text(encoding="utf-8")
    status = APPROVAL_STATUS_MIGRATION_PATH.read_text(encoding="utf-8")

    assert "ck_approvals_status" not in workflow
    assert status.count('"ck_approvals_status"') == 2
```

- [ ] **Step 2: Prove the regression fails**

Run: `pytest tests/test_migrations.py::test_approval_status_constraint_has_one_migration_owner -q`

Expected: FAIL because revision `004` still contains
`ck_approvals_status`.

- [ ] **Step 3: Move constraint ownership to revision `006`**

Remove only these blocks from revision `004`:

```python
op.create_check_constraint(
    "ck_approvals_status",
    "approvals",
    "status IN ('pending', 'approved', 'rejected', 'expired')",
)
```

```python
op.drop_constraint("ck_approvals_status", "approvals", type_="check")
```

Keep revision IDs and dependencies unchanged. Do not edit revision `006`.

- [ ] **Step 4: Add PostgreSQL to GitHub Actions**

Add a PostgreSQL 16 service and job-level connection URL to the `test` job:

```yaml
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_USER: integra
          POSTGRES_PASSWORD: integra
          POSTGRES_DB: integra_test
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U integra -d integra_test"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 10
    env:
      DATABASE_URL: postgresql+asyncpg://integra:integra@localhost:5432/integra_test
```

Keep `LLM_API_KEY` absent and retain `contents: read`.

- [ ] **Step 5: Verify migration ownership and graph**

Run:

```powershell
pytest tests/test_migrations.py::test_approval_status_constraint_has_one_migration_owner -q
python -m alembic heads
python -m alembic history
```

Expected: the test passes and `007_phase12_job_trace_id` remains the single
head.

## Task 1: Enforce service credential and active-tenant boundaries

**Files:**
- Edit: `app/auth/api_keys.py`
- Edit: `scripts/seed_demo.py`
- Edit: `tests/test_auth.py`
- Edit: `tests/test_seed.py`

- [ ] **Step 1: Add failing service-auth regressions**

Use the unit-test-writer skill in the main agent. Extend the existing fake
session so the service query evaluates tenant status, principal type, and
capability. Add tests with these assertions:

```python
def test_operator_key_cannot_authenticate_as_service(
    client_factory, seeded_credentials
) -> None:
    _, record, raw_key = seeded_credentials
    record.principal_type = "operator"
    record.capability = "approval_decider"
    record.actor_ref = "ops-alice"

    response = client_factory({record.key_hash: record}).get(
        "/v1/cases/00000000-0000-0000-0000-000000000001",
        headers={"X-API-Key": raw_key},
    )

    assert response.status_code == 401


def test_inactive_tenant_service_key_is_rejected(
    client_factory, seeded_credentials
) -> None:
    tenant, record, raw_key = seeded_credentials
    tenant.status = "inactive"

    response = client_factory({record.key_hash: record}).get(
        "/v1/cases/00000000-0000-0000-0000-000000000001",
        headers={"X-API-Key": raw_key},
    )

    assert response.status_code == 401
```

Add equivalent signed-webhook coverage proving the operator and inactive
tenant keys receive the generic `401 Invalid inbound webhook` response.

- [ ] **Step 2: Prove service-auth tests fail**

Run: `pytest tests/test_auth.py -q`

Expected: the new operator-as-service and inactive-tenant tests fail.

- [ ] **Step 3: Add the missing query predicates**

Change `_resolve_active_tenant` to use this predicate set:

```python
.where(
    ApiKey.key_hash == hash_api_key(raw_key),
    ApiKey.is_active.is_(True),
    ApiKey.principal_type == "service",
    ApiKey.capability == "none",
    Tenant.status == "active",
)
```

Do not duplicate this logic in the webhook dependency; keep using the shared
resolver.

- [ ] **Step 4: Add seed lifecycle tests**

Use the existing `SeedDatabase` and `SeedSession`. Add one test proving service
seeding changes an inactive existing tenant to `active`, and one proving
`seed_operator_key` raises `ValueError("tenant must be active before an operator key")`
for an inactive tenant.

- [ ] **Step 5: Implement the seed lifecycle contract**

In `seed_demo_tenant`, set `tenant.status = "active"` for both new and reused
tenants before rotating the service key. In `seed_operator_key`, use:

```python
if tenant is None:
    raise ValueError("tenant must be seeded before an operator key")
if tenant.status != "active":
    raise ValueError("tenant must be active before an operator key")
```

- [ ] **Step 6: Verify auth and seed behavior**

Run: `pytest tests/test_auth.py tests/test_seed.py tests/test_inbound_webhook.py -q`

Expected: PASS; service and operator credentials work only in their declared
boundaries.

## Task 2: Type tool terminal failures and preserve their public error code

**Files:**
- Edit: `app/agent/models.py`
- Edit: `app/agent/__init__.py`
- Edit: `app/agent/loop.py`
- Edit: `app/tools/executor.py`
- Edit: `app/workers/agent_worker.py`
- Edit: `app/observability/events.py`
- Edit: `tests/test_agent_loop.py`
- Edit: `tests/test_tools.py`
- Edit: `tests/test_agent_worker.py`
- Edit: `tests/test_jobs_api.py`

- [ ] **Step 1: Add failing model and loop tests**

Use the unit-test-writer skill in the main agent. Define tests around this new
public contract:

```python
class ToolExecutionDisposition(str, Enum):
    CONTINUE = "continue"
    COMPLETED = "completed"
    FAILED = "failed"
```

Test that a `FAILED` result produces `RunStatus.FAILED`,
`StopReason.TOOL_EXECUTION_FAILED`, and the sanitized final response. Test that
`COMPLETED` still produces `RunStatus.COMPLETED` and
`StopReason.EXECUTOR_STOPPED`.

- [ ] **Step 2: Prove the loop test fails**

Run: `pytest tests/test_agent_loop.py -q`

Expected: FAIL because `ToolExecutionDisposition` and the stop reason do not
exist.

- [ ] **Step 3: Implement the typed result contract**

Replace boolean continuation control with:

```python
class ToolExecutionDisposition(str, Enum):
    CONTINUE = "continue"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    data: ToolData = None
    disposition: ToolExecutionDisposition = ToolExecutionDisposition.CONTINUE
    final_response: str | None = None
```

Add `TOOL_EXECUTION_FAILED = "tool_execution_failed"` to `StopReason`. Export
the new enum from `app.agent`.

In `AgentLoop`, branch on `execution.disposition`: append the tool message once,
return failed for `FAILED`, completed for `COMPLETED`, and continue only for
`CONTINUE`.

- [ ] **Step 4: Classify executor outcomes explicitly**

Update every `ToolExecutionResult` construction in `app/tools/executor.py`:

- normal handler result: `CONTINUE`;
- policy denial, invalid arguments, approval request, not found, and trusted
  source stop: `COMPLETED`;
- broad backend exception: `FAILED` with data and final response equal to the
  stable string `tool_execution_failed`.

Update test fakes and scenario runners to use `disposition` instead of
`continue_run`.

- [ ] **Step 5: Add failing worker persistence tests**

Test both branches:

```python
result = AgentRunResult(
    status=RunStatus.FAILED,
    reason=StopReason.TOOL_EXECUTION_FAILED,
    messages=(),
    steps=1,
    final_response="tool_execution_failed",
)
```

Without a marker, expect `mark_failed(..., "tool_execution_failed", ...)`.
With a marker, expect `mark_failed_uncertain(...,
"agent_failed_after_side_effect", ...)`. Assert neither durable payload
contains the final response or exception text.

- [ ] **Step 6: Persist and expose the stable code**

Add `TOOL_EXECUTION_FAILED` to `WorkerErrorCode`. Map
`StopReason.TOOL_EXECUTION_FAILED` to its value in `_persist_result`; keep other
failed agent runs on `agent_failed`. `project_job_read` already filters through
`WorkerErrorCode`, so add an API projection test that expects
`error_code="tool_execution_failed"`.

- [ ] **Step 7: Verify the full failure path**

Run:

```powershell
pytest tests/test_tools.py tests/test_agent_loop.py tests/test_agent_worker.py tests/test_jobs_api.py -q
```

Expected: PASS; no tool backend exception produces a succeeded job.

## Task 3: Compare complete tool calls in the repetition guard

**Files:**
- Edit: `app/agent/loop.py`
- Edit: `tests/test_agent_loop.py`
- Edit: `tests/test_freight_eval_runner.py`

- [ ] **Step 1: Add the argument-sensitive regression**

Use the unit-test-writer skill in the main agent. Add a test that supplies
three consecutive `find_customer` calls with distinct email arguments followed
by a final proposal. Assert four steps complete and all three tools execute.
Keep the existing equal-call test and change it to use the same non-empty
arguments three times.

- [ ] **Step 2: Prove the new test fails**

Run: `pytest tests/test_agent_loop.py -q`

Expected: FAIL with `StopReason.REPEATED_TOOL` after the third same-name call.

- [ ] **Step 3: Track the immutable `ToolCall`**

Replace `previous_action_key: str | None` with
`previous_tool_call: ToolCall | None`. Compare `tool_call == previous_tool_call`
and store a deep copy when the sequence changes. Keep the threshold and
nonconsecutive reset behavior unchanged.

- [ ] **Step 4: Verify loop and freight guards**

Run:

```powershell
pytest tests/test_agent_loop.py tests/test_freight_eval_runner.py -q
```

Expected: PASS for both equal and different-argument sequences.

## Task 4: Make job transitions tenant- and attempt-fenced

**Files:**
- Edit: `app/domain/job_repository.py`
- Edit: `app/workers/agent_worker.py`
- Edit: `tests/test_job_repository.py`
- Edit: `tests/test_agent_worker.py`

- [ ] **Step 1: Add stale-transition repository tests**

Use the unit-test-writer skill in the main agent. Extend `_Session` so it can
evaluate tenant, attempt, status, and lease predicates. Add tests that require
`JobLeaseLostError` from terminal and retry transitions when tenant or attempt
does not match. Add a positive test for the owned running attempt.

- [ ] **Step 2: Prove stale transitions are silently accepted**

Run: `pytest tests/test_job_repository.py -q`

Expected: FAIL because methods return `None` without signalling ownership loss.

- [ ] **Step 3: Require ownership arguments and a typed error**

Add:

```python
class JobLeaseLostError(RuntimeError):
    """The worker no longer owns the running job attempt."""
```

Make `tenant_id` and `attempt_count` required keyword-only arguments on
`mark_succeeded`, `mark_failed`, `mark_failed_uncertain`, `schedule_retry`, and
`get_side_effect_marker`. `_get_job` must always include:

```python
AgentJob.id == job_id,
AgentJob.tenant_id == tenant_id,
AgentJob.attempt_count == attempt_count,
```

Terminal and retry methods must raise `JobLeaseLostError` unless the row exists
with `status == "running"`. Remove the optional-predicate branches.

- [ ] **Step 4: Update every worker call site**

Pass `claimed.tenant_id` and `claimed.attempt_count` to each repository method,
including `_has_side_effect`. Catch `JobLeaseLostError` separately from
persistence failures so a stale worker does not emit a successful terminal
event or overwrite the current attempt.

- [ ] **Step 5: Verify fencing**

Run: `pytest tests/test_job_repository.py tests/test_agent_worker.py -q`

Expected: PASS; stale attempts have an explicit typed outcome.

## Task 5: Renew running leases with a heartbeat

**Files:**
- Edit: `app/domain/job_repository.py`
- Edit: `app/workers/agent_worker.py`
- Edit: `app/core/config.py`
- Edit: `app/observability/events.py`
- Edit: `tests/test_job_repository.py`
- Edit: `tests/test_agent_worker.py`
- Edit: `tests/test_health.py`

- [ ] **Step 1: Add repository renewal tests**

Use the unit-test-writer skill in the main agent. Add tests for:

- owned, unexpired running attempt updates `lease_expires_at` and returns
  `True`;
- expired lease, different tenant, different attempt, or non-running status
  returns `False` without mutation.

- [ ] **Step 2: Prove `renew_lease` is missing**

Run: `pytest tests/test_job_repository.py -q`

Expected: FAIL because `JobRepository.renew_lease` does not exist.

- [ ] **Step 3: Implement atomic renewal**

Add this interface:

```python
async def renew_lease(
    self,
    job_id: UUID,
    *,
    tenant_id: UUID,
    attempt_count: int,
    now: datetime,
    lease_until: datetime,
) -> bool:
```

Inside `session.begin()`, select the exact tenant/job/attempt with
`status="running"`, `lease_expires_at IS NOT NULL`, and
`lease_expires_at > now`, using `FOR UPDATE`. Return `False` if no row matches;
otherwise set `lease_expires_at = lease_until` and return `True`.

- [ ] **Step 4: Add heartbeat lifecycle tests**

Use an injected wait seam rather than wall-clock sleeps. Test that:

- a long-running `to_thread` operation causes at least one renewal;
- finishing the thread stops the heartbeat without waiting for the full
  interval;
- `renew_lease=False` prevents terminal persistence and emits a safe lease-loss
  event;
- a renewal exception follows the persistence-error path without exception
  text in events.

- [ ] **Step 5: Implement the worker heartbeat**

Add a helper with this control flow:

```python
async def _maintain_lease(
    self,
    claimed: ClaimedJob,
    stopped: asyncio.Event,
) -> bool:
    interval = max(0.1, self._lease_seconds / 3)
    while True:
        try:
            await asyncio.wait_for(stopped.wait(), timeout=interval)
            return True
        except TimeoutError:
            pass
        now = self._now()
        async with self._session_factory() as session:
            renewed = await JobRepository(session).renew_lease(
                claimed.id,
                tenant_id=claimed.tenant_id,
                attempt_count=claimed.attempt_count,
                now=now,
                lease_until=now + timedelta(seconds=self._lease_seconds),
            )
        if not renewed:
            return False
```

Start this task immediately before `to_thread`. Set the stop event in a
`finally` block, await the heartbeat task, and treat `False` as lost ownership.
Do not try to cancel or kill the agent thread.

Add `LEASE_LOST` to the closed observation outcome and worker error enums.

- [ ] **Step 6: Remove the unused concurrency setting**

Delete `worker_concurrency` from `Settings` and all test constructors. Add an
assembly test that `Settings.model_fields` does not contain
`worker_concurrency`. Do not add an in-process semaphore or task pool.

- [ ] **Step 7: Verify worker ownership behavior**

Run:

```powershell
pytest tests/test_job_repository.py tests/test_agent_worker.py tests/test_health.py -q
```

Expected: PASS; a live attempt renews its lease and a stale attempt cannot
persist a result.

## Task 6: Reject oversized request bodies before JSON parsing

**Files:**
- Create: `app/api/body_limit.py`
- Edit: `app/main.py`
- Edit: `app/domain/intake.py`
- Edit: `app/domain/schemas.py`
- Create: `tests/test_body_limit.py`
- Edit: `tests/test_cases_api.py`
- Edit: `tests/test_intake.py`

- [ ] **Step 1: Add ASGI boundary tests**

Use the unit-test-writer skill in the main agent. Test a minimal FastAPI app
wrapped in the new middleware. Cover:

```python
MAX_API_REQUEST_BODY_BYTES = 1024 * 1024
```

- a declared non-webhook body above 1 MiB returns `413` before the handler;
- a chunked non-webhook body crossing 1 MiB returns `413`;
- a body at exactly 1 MiB reaches the handler;
- `/v1/inbound/email/webhook` retains
  `MAX_INBOUND_WEBHOOK_BODY_BYTES`;
- a `GET` request remains unaffected;
- the response body is exactly `{"detail":"Request body too large"}`.

- [ ] **Step 2: Prove the middleware test fails**

Run: `pytest tests/test_body_limit.py -q`

Expected: FAIL because `app.api.body_limit` does not exist.

- [ ] **Step 3: Implement the streaming ASGI limit**

Create `RequestBodyLimitMiddleware` with:

```python
MAX_API_REQUEST_BODY_BYTES = 1024 * 1024


def _request_limit(scope: MutableMapping[str, Any]) -> int | None:
    if scope.get("type") != "http" or scope.get("method") != "POST":
        return None
    if scope.get("path") == "/v1/inbound/email/webhook":
        return MAX_INBOUND_WEBHOOK_BODY_BYTES
    return MAX_API_REQUEST_BODY_BYTES
```

Reject an oversized valid `Content-Length` before calling the application.
Wrap `receive` and count each `http.request` body chunk for absent, malformed,
or bypassed length headers. Catch only the middleware's private
`RequestBodyTooLarge` exception and send a `JSONResponse` with status `413`.
Do not store body chunks or include their content in errors.

- [ ] **Step 4: Install middleware in the correct order**

Register `RequestBodyLimitMiddleware` before `ObservabilityMiddleware` in
`app/main.py`. Starlette applies the last added middleware as the outer layer,
so observability must remain outermost and record rejected requests.

- [ ] **Step 5: Add domain field-limit tests**

Test `CreateIntakeRequest` and `CreateCaseRequest` at and above these limits:

```python
MAX_CHANNEL_CHARS = 100
MAX_SUBJECT_CHARS = 500
MAX_BODY_CHARS = 100_000
MAX_EXTRACTED_FIELDS_BYTES = 100_000
```

Serialize extracted fields with deterministic JSON using UTF-8 and
`allow_nan=False`; reject values above the byte limit or non-finite JSON.

- [ ] **Step 6: Implement Pydantic limits**

Put shared constants in `app/domain/schemas.py`. Apply `Field` bounds to direct
case and intake models. Add a `field_validator("extracted_fields")` that checks
the encoded deterministic JSON length and returns the original mapping.

- [ ] **Step 7: Verify body and domain boundaries**

Run:

```powershell
pytest tests/test_body_limit.py tests/test_cases_api.py tests/test_intake.py tests/test_inbound_webhook.py tests/test_observability_api.py -q
```

Expected: PASS; oversized bodies are rejected without source-bearing events.

## Task 7: Complete job observability and tracked documentation

**Files:**
- Edit: `app/observability/events.py`
- Edit: `tests/test_observability_api.py`
- Edit: `.gitignore`
- Edit: `README.md`
- Edit: `docs/runbooks/live-provider-demo.md`
- Edit: `tests/test_observability.py`
- Edit: `tests/test_demo_compose.py`

- [ ] **Step 1: Add the job-route regression**

Use the unit-test-writer skill in the main agent. Add a `GET /v1/jobs/{job_id}`
request to the observed app and assert the completion event uses
`RouteName.JOB`, `method="GET"`, and contains no source or credential fields.

- [ ] **Step 2: Prove the route test fails**

Run: `pytest tests/test_observability_api.py -q`

Expected: FAIL because the route resolves to `RouteName.UNKNOWN`.

- [ ] **Step 3: Add the closed route**

Add:

```python
JOB = "/v1/jobs/{job_id}"
```

to `RouteName`. Do not add arbitrary route strings to observation events.

- [ ] **Step 4: Track only the runbook under `docs/`**

Replace the broad ignore-only rule with narrow exceptions:

```gitignore
docs/*
!docs/runbooks/
docs/runbooks/*
!docs/runbooks/live-provider-demo.md
```

Keep `docs/specs/` and `docs/plans/` ignored. Verify with:

```powershell
git check-ignore docs/runbooks/live-provider-demo.md
git check-ignore docs/specs/2026-09-30-architecture-hardening-design.md
```

Expected: the first command prints nothing and returns 1; the second reports an
ignore rule.

- [ ] **Step 5: Update README and the runbook**

Document these exact contracts:

- service keys cannot act as operators and operator keys cannot call service
  APIs;
- ordinary POST bodies are limited to 1 MiB, intake/case body text to 100,000
  characters, and inbound webhook envelopes to 8 MiB;
- one worker process handles one job at a time;
- multiple worker containers scale horizontally through `SKIP LOCKED`, lease
  heartbeat, and attempt fencing;
- remove `WORKER_CONCURRENCY` from configuration tables and examples;
- backend tool failures produce `tool_execution_failed` and never
  `succeeded`;
- the repaired migration chain requires recreating the disposable local
  PostgreSQL volume before validation.

Update documentation contract tests to assert these phrases and the absence of
`WORKER_CONCURRENCY`.

- [ ] **Step 6: Verify observability and documentation contracts**

Run:

```powershell
pytest tests/test_observability.py tests/test_observability_api.py tests/test_demo_compose.py -q
```

Expected: PASS and the runbook appears as an untracked file in `git status`
until committed.

## Task 8: Run clean PostgreSQL and full quality verification

**Files:**
- No source changes expected.

- [ ] **Step 1: Record and confirm the destructive local target**

Run:

```powershell
docker compose ps --format json
docker volume ls --filter name=integra_intake_pgdata
```

Expected: only the disposable `integra_intake` Compose database and its
`pgdata` volume are targeted. Stop if the resolved project or volume differs.

- [ ] **Step 2: Recreate the user-approved local database volume**

Run:

```powershell
docker compose down -v
docker compose up -d db
```

Expected: Docker removes the local Compose containers and `pgdata` volume,
then starts a healthy empty PostgreSQL service. Report that this deletes local
database contents and cannot be recovered from the removed volume.

- [ ] **Step 3: Apply the complete migration chain**

Run:

```powershell
$env:DATABASE_URL = 'postgresql+asyncpg://integra:integra@localhost:5432/integra'
python -m alembic upgrade head
python -m alembic current
```

Expected: upgrade succeeds and current revision is
`007_phase12_job_trace_id (head)`.

- [ ] **Step 4: Run focused integration tests**

Run:

```powershell
pytest tests/test_migrations.py tests/test_postgres_integration.py tests/test_postgres_approvals.py -q
```

Expected: PASS with no PostgreSQL skips.

- [ ] **Step 5: Run the full project suite**

Run:

```powershell
pytest -q
pytest evals/test_freight.py -q
git diff --check
```

Expected: all tests and 50 freight cases pass; `git diff --check` reports no
errors. The project has no configured formatter, linter, or type checker, so do
not invent additional commands.

- [ ] **Step 6: Inspect the final repository state**

Run:

```powershell
git status --short
git diff --stat
git diff -- . ':!docs/specs/**' ':!docs/plans/**'
```

Expected: only intended architecture-hardening changes plus the user's existing
live-demo work appear. Do not discard or overwrite unrelated changes.

## Task 9: Review the complete change set

**Files:**
- Review all files changed by Tasks 0-8.

- [ ] **Step 1: Call the `code-review` skill in the main agent**

Review the final diff against
`docs/specs/2026-09-30-architecture-hardening-design.md` and this plan. Do not
use a review subagent. Check migration order, auth privilege separation, lease
races, transaction fencing, source/secret leakage, stable public error codes,
ASGI response correctness, and documentation accuracy.

- [ ] **Step 2: Discuss review findings before corrections**

Do not make corrections from the review silently. Present substantive findings
to the user with file and line references. If the review has no substantive
findings, state the residual risks: live-provider behavior remains opt-in and
thread execution cannot be forcibly cancelled after lease loss.

- [ ] **Step 3: Re-run verification after any approved correction**

Run the focused test for each correction, followed by:

```powershell
pytest -q
pytest evals/test_freight.py -q
git diff --check
```

Expected: all commands pass on the final diff.
