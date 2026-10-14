# Implementation plan: Phase 7 worker runtime, queue, idempotency, and async persistence

> **For agents:** Use the executing-plans skill to implement this plan task by task. Steps use checkbox syntax for tracking.

**Purpose:** Accept idempotent tenant-authenticated intake jobs, run the existing synchronous agent loop in a separate worker, and persist allowed case operations through async PostgreSQL without putting LLM/provider work in FastAPI requests.

**Architecture:** POST /v1/intake atomically creates or reuses a PostgreSQL AgentJob and IdempotencyRecord. A separate async worker claims jobs with FOR UPDATE SKIP LOCKED, executes the unchanged synchronous AgentLoop through asyncio.to_thread, and uses one explicit gateway to run async SQLAlchemy tool work on the worker-owned event loop. A mutation marks its job in its own database transaction; a recovered lease after such a marker becomes failed_uncertain rather than being replayed.

**Key files/stack:** Python 3.12, FastAPI, Pydantic v2, SQLAlchemy 2 async, asyncpg/PostgreSQL, Alembic, httpx, pytest/pytest-asyncio, existing AgentLoop, PolicyGatedToolExecutor, and TenantToolPort.

**Approved design:** docs/specs/2026-09-21-phase-7-worker-runtime-design.md

---

## File map

| File | Responsibility |
| --- | --- |
| app/db/models.py | Add AgentJob and make IdempotencyRecord able to reference a queued job before a case exists. |
| alembic/versions/003_phase7_worker_runtime.py | Append-only schema migration. |
| app/runtime/profiles.py | Trusted slug-to-YAML resolver, canonical snapshot, and revision hash. |
| app/domain/intake.py | Closed inbound request/command/result models, request hash, and enqueue service. |
| app/domain/job_repository.py | Idempotency lookup, job enqueue, lease/recovery, and terminal-job persistence. |
| app/api/intake.py | New authenticated POST /v1/intake endpoint. |
| app/tools/postgres.py | Async tenant-scoped case/customer persistence and its sync job facade. |
| app/runtime/gateway.py | The explicit thread-to-owner-loop bridge; it never creates a loop. |
| app/runtime/factory.py | Reconstruct trusted context and build one thread-owned agent runtime. |
| app/runtime/retry.py | Generic retry primitive for safe GET / idempotent POST only. |
| app/workers/agent_worker.py, app/workers/__main__.py | Separate worker orchestration and its process entry point. |
| app/core/config.py, Dockerfile, docker-compose.yml, scripts/seed_demo.py | Worker settings and local startup alignment. |
| tests/test_intake.py, test_job_repository.py, test_runtime.py, test_agent_worker.py, test_postgres_tool_port.py | New unit/worker/PostgreSQL coverage. |
| tests/test_migrations.py, test_postgres_integration.py, test_cases_api.py, test_tools.py, test_seed.py, test_health.py | Focused regression extensions. |

Do not edit applied Alembic revisions, app/agent/loop.py, app/agent/models.py, the provider structured-output boundary, tenant routing precedence, or existing case-route behavior.

## Task 1: Add the durable schema and migration

**Files:**

- Edit: app/db/models.py
- Create: alembic/versions/003_phase7_worker_runtime.py
- Test: tests/test_migrations.py

- [ ] **Step 1: Call unit-test-writer for failing schema-contract tests.**

Use the unit-test-writer skill with this exact scope; it may edit only tests/test_migrations.py:

~~~text
Add Phase-7 migration tests. Assert AgentJob is tenant-owned and has UUID id and
tenant_id; JSONB source_snapshot, tenant_config_snapshot, risk_signals, and
nullable result; tenant_config_sha256; status default queued; attempt_count
default 0; available_at, lease_expires_at, started_at, finished_at,
side_effect_committed_at, error_code, timestamps; tenant/id unique/index and
status/available_at plus running/lease indexes.

Assert idempotency_records preserves non-null request_hash and unique
(tenant_id, key), adds nullable job_id with composite tenant/job FK to
agent_jobs, and changes case_id to nullable while preserving the existing
tenant/case composite FK. Extend information_schema and SQL integrity tests:
tenant A cannot reference tenant B's agent job from its idempotency row.
Do not edit product files or an existing migration.
~~~

- [ ] **Step 2: Run the migration contract tests before implementation.**

Run:

~~~powershell
pytest tests/test_migrations.py -q
~~~

Expected: metadata assertions fail because AgentJob and idempotency job linkage do not exist. Live database tests may skip only when DATABASE_URL is absent.

- [ ] **Step 3: Add the ORM model.**

In app/db/models.py, add this model before IdempotencyRecord, using the current TimestampedModel, UUID, JSONB, FK, unique-constraint, and index conventions:

~~~python
class AgentJob(TimestampedModel, Base):
    __tablename__ = "agent_jobs"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_agent_jobs_tenant_id_id"),
        Index("ix_agent_jobs_tenant_id_id", "tenant_id", "id"),
        Index("ix_agent_jobs_status_available_at", "status", "available_at"),
        Index("ix_agent_jobs_running_lease", "status", "lease_expires_at"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(ForeignKey("tenants.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False,
                                        default="queued", server_default="queued")
    source_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    tenant_config_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    tenant_config_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    risk_signals: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    attempt_count: Mapped[int] = mapped_column(nullable=False, default=0, server_default="0")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                     nullable=False, server_default=func.now())
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    side_effect_committed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict | None] = mapped_column(JSONB)
    error_code: Mapped[str | None] = mapped_column(String(100))
~~~

Make IdempotencyRecord.case_id nullable, add nullable job_id, and add:

~~~python
ForeignKeyConstraint(
    ["tenant_id", "job_id"],
    ["agent_jobs.tenant_id", "agent_jobs.id"],
    name="fk_idempotency_records_tenant_job",
)
~~~

Retain the present tenant/case FK and unique tenant/key rule.

- [ ] **Step 4: Add the append-only migration.**

Create revision 003_phase7_worker_runtime with down_revision "002_domain_and_tenancy". Its upgrade creates agent_jobs, both job indexes, and the composite tenant/job FK. It adds nullable job_id, drops/recreates fk_idempotency_records_tenant_case only to alter case_id nullable, then recreates the same case FK. Its downgrade reverses precisely those changes: remove job FK/index/column, restore non-null case_id and original case FK, then drop agent_jobs. Do not edit revision 002.

- [ ] **Step 5: Verify the schema.**

Run:

~~~powershell
pytest tests/test_migrations.py -q
~~~

Expected: metadata passes; live migration tests pass with PostgreSQL or skip only under their established fixture.

## Task 2: Implement trusted snapshots, closed input, and atomic enqueue

**Files:**

- Create: app/runtime/__init__.py
- Create: app/runtime/profiles.py
- Create: app/domain/intake.py
- Create: app/domain/job_repository.py
- Create: app/api/intake.py
- Edit: app/main.py
- Edit: app/policy/models.py
- Edit: app/core/config.py
- Test: tests/test_runtime.py
- Test: tests/test_intake.py
- Test: tests/test_postgres_integration.py

- [ ] **Step 1: Call unit-test-writer for profile and enqueue tests.**

Use the unit-test-writer skill with this exact scope; it may edit only tests/test_runtime.py, tests/test_intake.py, and tests/test_postgres_integration.py:

~~~text
Write failing Phase-7 tests. TenantProfileResolver must load only
<tenant.slug>.yaml via load_tenant_config, return a TenantConfig plus a
canonical SHA-256 snapshot, and reject a missing slug with no fallback.
Canonical JSON uses sort_keys=True, separators=(',', ':'), and ensure_ascii=False.

CreateIntakeRequest accepts only non-blank channel, subject, body and rejects
tenant_id, risk_signals, case_id, profile, extracted_fields, and all extras.
canonical_intake_hash is stable for key order and changes for any source change.
TrustedToolRuntimeContext keeps existing callers working with job_id=None and
preserves a supplied UUID as trusted data.

Test IntakeEnqueueService: fresh key creates one job; same tenant/key/hash
returns its job_id without another job; changed hash raises IdempotencyConflict;
another tenant may reuse the key; a missing profile writes neither job nor key.
For POST /v1/intake, missing/blank Idempotency-Key is 422, valid input is 202
with job_id/status queued, same body/key returns same ID, changed body/key is
409, and forbidden source fields are 422. Add one PostgreSQL concurrent-enqueue
test that leaves exactly one job and one idempotency record for a tenant/key.
Keep /v1/cases unchanged.
~~~

- [ ] **Step 2: Run the new tests before implementation.**

Run:

~~~powershell
pytest tests/test_runtime.py tests/test_intake.py -q
~~~

Expected: collection/import failure for the new runtime, service, and router contracts.

- [ ] **Step 3: Implement profile resolution and request contracts.**

In app/runtime/profiles.py define:

~~~python
class TenantProfileUnavailableError(RuntimeError):
    pass

@dataclass(frozen=True, slots=True)
class ResolvedTenantProfile:
    config: TenantConfig
    snapshot: dict[str, object]
    sha256: str

class TenantProfileResolver:
    def __init__(self, directory: Path) -> None: ...
    def resolve(self, tenant_slug: str) -> ResolvedTenantProfile: ...

def canonical_json_bytes(value: Mapping[str, object]) -> bytes: ...
~~~

resolve constructs exactly directory / f"{tenant_slug}.yaml", calls load_tenant_config, serializes config.model_dump(mode="json") with canonical JSON, deep-copies the mapping, and hashes its UTF-8 bytes. Translate loader errors to TenantProfileUnavailableError; do not fall back to any profile.

In app/domain/intake.py add frozen/closed CreateIntakeRequest with min/max lengths:
channel 1..100, subject 1..500, body at least 1. Add EnqueueIntakeCommand
(tenant_id, tenant_slug, TrustedSource, RiskSignals default, idempotency_key),
EnqueueResult(job_id, created), IdempotencyConflict, and
canonical_intake_hash(source), which hashes exactly channel/subject/body canonical JSON.

Add job_id: UUID | None = None as the final defaulted field in
TrustedToolRuntimeContext. It must never be accepted from AgentProposal, tool
arguments, or the HTTP model.

- [ ] **Step 4: Implement repository and race-safe enqueue service.**

JobRepository accepts AsyncSession. Its public enqueue methods must have tenant-scoped signatures:

~~~python
async def get_idempotency_for_tenant(
    self, tenant_id: UUID, key: str, *, for_update: bool = False
) -> IdempotencyRecord | None: ...

async def create_job(
    self, command: EnqueueIntakeCommand, profile: ResolvedTenantProfile
) -> AgentJob: ...

async def create_idempotency_record(
    self, tenant_id: UUID, key: str, request_hash: str, job_id: UUID
) -> IdempotencyRecord: ...
~~~

IntakeEnqueueService first rejects a blank or over-255-character header key,
then resolves the trusted profile. Within session.begin(), select an existing
record by tenant/key with FOR UPDATE. Equal hash returns its non-null job_id;
different hash raises IdempotencyConflict. With no record, use
session.begin_nested() to create/flush the job then key record. If the unique
insert raises IntegrityError, query the record in the outer transaction and
return equal-hash job or raise conflict. Do not translate profile/database
errors into idempotency outcomes.

- [ ] **Step 5: Add the shared trusted-profile directory setting.**

Before constructing the route's resolver, add this existing-settings field in
app/core/config.py so the API and future worker share the same trusted profile
directory without changing any current setting:

~~~python
tenant_profiles_directory: str = "examples"
~~~

- [ ] **Step 6: Add only the new asynchronous API boundary.**

Create app/api/intake.py with POST "" under APIRouter(prefix="/intake", tags=["intake"]):

~~~python
@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def enqueue_intake(
    request: CreateIntakeRequest,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_db_session, use_cache=False),
) -> IntakeAcceptedResponse: ...
~~~

Build TrustedSource only from the request and EnqueueIntakeCommand only from
the authenticated tenant with default RiskSignals. Resolve profiles from a
Settings directory. Map IdempotencyConflict to sanitized HTTP 409 and missing/
invalid profile to sanitized HTTP 503. Return only job_id and "queued".
Register the router in app/main.py with prefix "/v1"; do not alter app/api/cases.py.

- [ ] **Step 7: Verify enqueue plus existing case routes.**

Run:

~~~powershell
pytest tests/test_runtime.py tests/test_intake.py tests/test_cases_api.py tests/test_postgres_integration.py -q
~~~

Expected: closed input, profile snapshots, idempotency, API response, and current case tests pass; live tests skip only with no DATABASE_URL.

## Task 3: Implement real tenant-scoped PostgreSQL tools

**Files:**

- Create: app/tools/postgres.py
- Edit: app/domain/repositories.py
- Edit: app/domain/service.py
- Test: tests/test_postgres_tool_port.py
- Test: tests/test_tools.py

- [ ] **Step 1: Call unit-test-writer for failing persistence-port tests.**

Use the unit-test-writer skill with this exact scope; it may edit only tests/test_postgres_tool_port.py and tests/test_tools.py:

~~~text
Add live PostgreSQL tests for PostgresTenantToolPort. With two tenants,
customers/cases/jobs/idempotency records, assert find_customer and case_exists
hide other-tenant rows. Valid create_case writes a received case, one created
event, idempotency_records.case_id, and agent_jobs.side_effect_committed_at in
one transaction. A repeat for the same job returns the first case and creates
no second case/event. Another tenant's customer raises CustomerNotFoundError
without mutation.

Assert update_case_fields locks/scopes a tenant case, merges only its patch,
creates one fields_updated event whose payload has sorted field names, and marks
the job atomically. Other-tenant update returns None and does not mutate.
Inject an event failure and prove case/update and side-effect marker roll back.
Add one regression that PolicyGatedToolExecutor still depends on unchanged
synchronous TenantToolPort signatures. Do not change AgentLoop or product code.
~~~

- [ ] **Step 2: Run focused tests before implementation.**

Run:

~~~powershell
pytest tests/test_postgres_tool_port.py tests/test_tools.py -q
~~~

Expected: missing adapter import fails while current in-memory tool tests stay green.

- [ ] **Step 3: Add narrow reusable async repository/service methods.**

In app/domain/repositories.py add only tenant-scoped helpers:

~~~python
async def get_customer_for_tenant(self, customer_id: UUID, tenant_id: UUID) -> Customer | None: ...
async def find_customer_for_tenant(
    self, tenant_id: UUID, *, email: str | None, external_id: str | None
) -> Customer | None: ...
async def get_case_for_tenant_for_update(
    self, case_id: UUID, tenant_id: UUID
) -> IntakeCase | None: ...
~~~

Every statement includes tenant_id; the last adds with_for_update(). In
app/domain/service.py add helpers that create a received IntakeCase from
TrustedSource/fields and append CaseEvent to an already-owned session. Do not
change CaseService.create_case() transaction behavior for existing API callers.

- [ ] **Step 4: Implement the async database port and synchronous facade.**

PostgresTenantToolPort receives an async session factory and a trusted job_id.
Read methods create a short session. Each write creates a session and exactly
one session.begin() transaction.

create_case locks the IdempotencyRecord selected by tenant_id/job_id. If its
case_id exists, re-read the case with tenant predicate and return it. Otherwise
validate customer_id through the tenant-scoped query, create from TrustedSource
and known-field mapping, flush, append CaseEvent(event_type="created"), set the
record case_id and AgentJob.side_effect_committed_at, and commit once.

update_case_fields selects/locks by case ID and tenant. Missing returns None.
For a match, replace extracted_fields with a copied JSON-compatible merge, add:

~~~python
CaseEvent(
    tenant_id=tenant_id,
    case_id=case.id,
    event_type="fields_updated",
    actor=None,
    payload={"updated_fields": sorted(fields)},
)
~~~

Then mark the job side effect in the same transaction. find_customer and
case_exists always filter tenant. Draft/review actions remain transient.

Also define SyncTenantToolPort, whose methods exactly match TenantToolPort and
only call gateway.call(async_port.method(...)). It has no AsyncSession, SQL,
or asyncio.run.

- [ ] **Step 5: Verify real-port and Phase-6 behavior.**

Run:

~~~powershell
pytest tests/test_postgres_tool_port.py tests/test_tools.py tests/test_policy.py -q
~~~

Expected: port tests pass/skip only under the standard PostgreSQL fixture; all Phase-6 policy/tool assertions remain green.

## Task 4: Implement the explicit bridge, runtime factory, and retry primitive

**Files:**

- Create: app/runtime/gateway.py
- Create: app/runtime/factory.py
- Create: app/runtime/retry.py
- Edit: app/core/config.py
- Test: tests/test_runtime.py

- [ ] **Step 1: Call unit-test-writer for gateway/factory/retry tests.**

Use the unit-test-writer skill with this exact scope; it may edit only tests/test_runtime.py:

~~~text
Add offline tests. From a separate thread, WorkerAsyncGateway schedules a
coroutine on its owner loop and returns a value that identifies that loop.
Calling it from the owner loop raises RuntimeError rather than deadlocking.
Source-inspect or behavior-test that app/runtime/gateway.py and the tool facade
do not call asyncio.run.

With fake LLM/port factories and a claimed job snapshot, AgentRuntimeFactory
rebuilds TenantConfig from the stored mapping, puts only trusted tenant/source/
risk/job values into TrustedToolRuntimeContext, and builds messages in fixed
order: runtime system, three existing skill systems, safe profile-catalog
system, user source JSON. Invalid snapshot fails before loop/provider use.

For RetryPolicy, a fake eligible GET and protected POST returning 500,500,200
calls three times and records deterministic injected 0.5 then 1.0 base delays.
An unprotected POST calls once; 4xx and exhausted retries do not become success.
~~~

- [ ] **Step 2: Run tests before implementation.**

Run:

~~~powershell
pytest tests/test_runtime.py -q
~~~

Expected: gateway, factory, retry, and worker-setting imports fail.

- [ ] **Step 3: Implement the sole bridge.**

Create WorkerAsyncGateway:

~~~python
class WorkerAsyncGateway:
    def __init__(self, owner_loop: asyncio.AbstractEventLoop) -> None:
        self._owner_loop = owner_loop

    def call(self, awaitable: Coroutine[object, object, T]) -> T:
        try:
            current = asyncio.get_running_loop()
        except RuntimeError:
            current = None
        if current is self._owner_loop:
            raise RuntimeError(
                "gateway must be called from the synchronous agent thread"
            )
        return asyncio.run_coroutine_threadsafe(
            awaitable, self._owner_loop
        ).result()
~~~

Use a module TypeVar for T. No code path in this file or SyncTenantToolPort may call asyncio.run.

- [ ] **Step 4: Build one trusted thread-owned runtime.**

AgentRuntimeFactory validates TenantConfig.model_validate(job.tenant_config_snapshot),
converts source/risk JSON to TrustedSource/RiskSignals, supplies trusted job_id,
builds PostgresTenantToolPort + SyncTenantToolPort + unchanged
PolicyGatedToolExecutor, and returns AgentLoop plus messages plus a closeable
thread-owned httpx.Client.

Message order is fixed: runtime safety system message; the three existing skill
build_system_message() values; one system JSON catalog containing configured
intake names, field names, action names; one user canonical JSON source
message. Never include tenant UUID, API key, DB error, authorization decision,
or arbitrary unknown profile content in a prompt.

- [ ] **Step 5: Implement bounded generic HTTP retry.**

Create RetryPolicy(max_retries=4, delays=(0.5, 1.0, 2.0, 4.0)) and
run_http_with_retry(operation, method, idempotency_key, sleep, random_uniform).
Only GET, or POST with a non-blank stable key, retries a declared transient
failure. Before a retry sleep delay + random_uniform(0.0, delay * 0.1).
Unprotected POST/other method executes once; preserve the last original
exception after exhaustion. Do not wrap the provider's ordinary POST and do
not add a TMS/email/Odoo client.

- [ ] **Step 6: Add bounded settings and verify runtime.**

Add Settings fields:

~~~python
worker_poll_interval_seconds: float = Field(default=0.5, gt=0)
worker_lease_seconds: int = Field(default=60, ge=1)
worker_concurrency: int = Field(default=1, ge=1)
worker_max_retries: int = Field(default=4, ge=0)
~~~

Run:

~~~powershell
pytest tests/test_runtime.py tests/test_openai_compatible_provider.py -q
~~~

Expected: gateway/retry pass offline and provider structured-output tests are unchanged.

## Task 5: Implement queue leasing and the separate worker

**Files:**

- Edit: app/domain/job_repository.py
- Create: app/workers/__init__.py
- Create: app/workers/agent_worker.py
- Create: app/workers/__main__.py
- Test: tests/test_job_repository.py
- Test: tests/test_agent_worker.py

- [ ] **Step 1: Call unit-test-writer for worker lifecycle tests.**

Use the unit-test-writer skill with this exact scope; it may edit only tests/test_job_repository.py and tests/test_agent_worker.py:

~~~text
Write offline tests with fake repository/runtime dependencies. Assert claim_next
uses a due queued job, increments attempts, writes running/started_at/lease,
and has a real-query assertion for SKIP LOCKED. Nonexpired running jobs are not
claimed twice. Expired job with no side-effect marker queues with configured
bounded retry delay while budget remains; marker means failed_uncertain with a
stable code and never reruns; exhausted budget is failed.

For AgentWorker.serve_once, prove loop execution goes through an injected
to_thread seam, not the test event loop. Completed and executor-stopped results
atomically persist succeeded and a sanitized summary containing only status,
reason, steps, final_response. Failed loop result persists failed. Typed
retryable pre-side-effect error schedules retry; marker plus error is
failed_uncertain. No FastAPI request, dependency, or API key may appear.
~~~

- [ ] **Step 2: Run worker tests before implementation.**

Run:

~~~powershell
pytest tests/test_job_repository.py tests/test_agent_worker.py -q
~~~

Expected: lifecycle and worker imports/assertions fail.

- [ ] **Step 3: Add explicit job transitions.**

JobRepository needs these transactionally explicit methods:

~~~python
async def recover_expired_leases(
    self, *, now: datetime, max_retries: int, retry_delay: timedelta
) -> int: ...
async def claim_next(
    self, *, now: datetime, lease_until: datetime
) -> ClaimedJob | None: ...
async def mark_succeeded(
    self, job_id: UUID, result: dict[str, ToolData], *, now: datetime
) -> None: ...
async def mark_failed(self, job_id: UUID, error_code: str, *, now: datetime) -> None: ...
async def schedule_retry(
    self, job_id: UUID, error_code: str, *, available_at: datetime
) -> None: ...
async def mark_failed_uncertain(
    self, job_id: UUID, error_code: str, *, now: datetime
) -> None: ...
~~~

Recovery/claim use SELECT FOR UPDATE SKIP LOCKED. Claim commits running and
lease before provider work and returns only copied scalars/JSON. Recovery
requeues only unmarked jobs below the initial-plus-four-attempt budget; marked
jobs become failed_uncertain. mark_succeeded writes result and terminal status
in one transaction.

- [ ] **Step 4: Implement AgentWorker.**

AgentWorker receives session factory, runtime factory, settings/clock, and an
injectable to_thread. serve_once first recovers leases, then claims a job. If
none is due return False. Create WorkerAsyncGateway from
asyncio.get_running_loop(), then await to_thread(run_claimed_job, claimed,
gateway). The synchronous function creates runtime, calls unchanged
AgentLoop.run(initial_messages), and closes its httpx.Client in finally.

Convert only AgentRunResult.status, reason, steps, and final_response to the
stored result. Completed includes executor-stopped outcomes and marks succeeded;
explicit failed result marks failed. A typed retryable error without marker
uses schedule_retry. Any error with marker marks failed_uncertain. Unexpected
errors use a stable code, never exception text.

serve loops serve_once and sleeps worker_poll_interval_seconds only if no job
was claimed. Cancellation stops new claims; it never moves AsyncSession to the
thread.

- [ ] **Step 5: Add the process entry point and verify.**

app/workers/__main__.py is the only Phase-7 process entry point containing:

~~~python
async def main() -> None:
    await AgentWorker.from_settings(settings).serve()

if __name__ == "__main__":
    asyncio.run(main())
~~~

Do not import this module from FastAPI.

Run:

~~~powershell
pytest tests/test_job_repository.py tests/test_agent_worker.py tests/test_agent_loop.py tests/test_policy.py -q
~~~

Expected: worker tests pass and all bounded-loop/policy precedence regressions remain green.

## Task 6: Wire local startup, add vertical evidence, and inspect boundaries

**Files:**

- Edit: Dockerfile
- Edit: docker-compose.yml
- Edit: scripts/seed_demo.py
- Test: tests/test_seed.py
- Test: tests/test_health.py
- Test: tests/test_postgres_integration.py

- [ ] **Step 1: Call unit-test-writer for seed/app/vertical tests.**

Use the unit-test-writer skill with this exact scope; it may edit only
tests/test_seed.py, tests/test_health.py, and tests/test_postgres_integration.py:

~~~text
Assert default seed slug is freight-broker while an explicit custom slug still
works and key rotation remains digest/prefix only. Assert /health is unchanged
and app assembly has cases/intake routers without importing workers or starting
LLM/provider work.

Add one live Phase-7 vertical test: authenticated freight-broker POST
/v1/intake with a key returns 202; AgentWorker with fake runtime/LLM claims it;
a valid create_case proposal creates exactly one tenant-owned case plus created
event and succeeds with no raw source body in job result. Repeating same intake
key and worker run creates no second case. A second tenant cannot retrieve/update
that case through the real port. No approval, send, or external client.
~~~

- [ ] **Step 2: Run tests before operational wiring.**

Run:

~~~powershell
pytest tests/test_seed.py tests/test_health.py tests/test_postgres_integration.py -q
~~~

Expected: new default-seed/vertical assertions fail until wiring exists; current health assertions pass.

- [ ] **Step 3: Make worker startup explicit.**

In Dockerfile add COPY examples ./examples before CMD; retain the API command.
In docker-compose.yml add a worker service using the same build and database
environment as api, with:

~~~yaml
command: ["python", "-m", "app.workers"]
depends_on:
  db:
    condition: service_healthy
~~~

Do not expose a worker port or commit a provider key. Change only
DEMO_TENANT_SLUG in scripts/seed_demo.py to "freight-broker"; explicit slug
arguments and API-key handling remain unchanged.

- [ ] **Step 4: Run focused end-to-end suites.**

Run:

~~~powershell
pytest tests/test_migrations.py tests/test_intake.py tests/test_job_repository.py tests/test_runtime.py tests/test_agent_worker.py tests/test_postgres_tool_port.py tests/test_postgres_integration.py tests/test_cases_api.py tests/test_tools.py tests/test_seed.py tests/test_health.py -q
~~~

Expected: offline suites pass; PostgreSQL cases pass when configured and otherwise skip only through the existing fixture.

- [ ] **Step 5: Inspect the scope boundary.**

Run:

~~~powershell
git diff -- app alembic tests Dockerfile docker-compose.yml scripts/seed_demo.py
~~~

Expected: no modification to applied migration, AgentLoop, provider schema validation, router precedence, existing case behavior, approval endpoints, send_reply, or real outbound integration. Confirm every synchronous database call uses WorkerAsyncGateway and no tool handler calls asyncio.run.

## Task 7: Required quality, review, and verification gates

**Files:**

- Review: all changes from Tasks 1–6

- [ ] **Step 1: Run full evidence checks.**

Run:

~~~powershell
pytest -q
git diff --check
~~~

Expected: all offline tests pass; PostgreSQL tests only skip in their current no-DATABASE_URL condition; no whitespace diagnostics.

- [ ] **Step 2: Call the code-review skill on the final diff.**

Supply the approved spec, this plan, focused/full test output, and final diff.
Require review of trusted tenant/source/risk construction; no FastAPI LLM work;
event-loop/session ownership; no hidden asyncio.run bridge; atomic create/update
and idempotency linkage; queue lease/crash semantics; retained Phase-6 loop/
policy behavior; and absence of approval/outbound scope leakage. Do not make
corrections from findings before discussing them with the user.

- [ ] **Step 3: Call verification-before-completion with evidence.**

Give it the specification, this plan, focused test output, full pytest output,
git diff --check, final diff, and code-review result. Do not claim Phase 7
complete until it confirms all accepted requirements.

## Plan self-review

| Approved requirement | Task |
| --- | --- |
| PostgreSQL queue and inbound idempotency | 1, 2, 5 |
| Trusted tenant/profile/source/risk context | 2, 4 |
| Async worker plus isolated synchronous loop | 4, 5, 6 |
| No hidden asyncio.run or session crossing | 3, 4, 5, 6 |
| Tenant-scoped real case/customer persistence | 3 |
| Atomic create/update plus audit/idempotency marker | 1, 3 |
| Lease, retry, and failed_uncertain crash rule | 4, 5 |
| Retry primitive with no outbound integration | 4 |
| Existing API/agent/policy contracts retained | 2–6 |
| Test, review, and evidence gates | 1–7 |

The plan deliberately excludes durable transcript resume, approvals, reply
sending, external integrations, and an async rewrite of AgentLoop.
