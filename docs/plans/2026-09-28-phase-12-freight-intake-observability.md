# Implementation plan: freight intake observability

> **Status:** Superseded by
> `docs/plans/2026-09-29-phase-12-universal-intake-observability.md`.
> Retained as implementation history; do not execute remaining checkboxes from
> this document.

> **For agents:** Use the `executing-plans` skill to implement this plan task by
> task. Tests must be authored through the `unit-test-writer` skill and the
> final diff must be reviewed through the `code-review` skill. Steps use
> checkbox syntax (`- [ ]`) for tracking.

**Purpose:** Repair the existing CI whitespace-gate false positive for binary
PDF fixtures, then add a server-owned trace across accepted freight intake,
safe typed JSON events, and locally derivable measurements without persisting
source content or connecting an external observability service.

**Architecture:** A small `app/observability` package owns the closed event
schema, immutable correlation context, observer protocol, fail-open emission,
JSON rendering, and HTTP middleware. `AgentJob.trace_id` is the only new
durable observability field; existing business audits remain authoritative for
case and approval history. API, worker, loop, provider, policy, tool, document,
and approval seams emit allowlisted metadata through an injected observer.

**Key files/stack:** Python 3.12+, FastAPI/Starlette ASGI, structlog already in
`pyproject.toml`, Pydantic v2, async SQLAlchemy 2, PostgreSQL/Alembic, pytest,
pytest-asyncio, `httpx.MockTransport`, and the existing provider-free freight
eval runner. No new package, SaaS, exporter, public metrics endpoint, or
trace-event table is required.

**Approved design:**
`docs/specs/2026-09-28-phase-12-freight-intake-observability-design.md`

---

## File map

| Path | Responsibility |
| --- | --- |
| `app/observability/events.py` | Closed event names, safe enums, and strict `ObservationEvent`. |
| `app/observability/context.py` | Immutable server-owned trace/request/entity correlation. |
| `app/observability/observer.py` | Observer protocol, no-op/recording-safe boundary, structlog adapter, fail-open emission. |
| `app/observability/logging.py` | Idempotent JSON logging setup for API and worker processes. |
| `app/observability/middleware.py` | Server request/trace creation, request timing, safe route/status event, additive response header. |
| `app/observability/__init__.py` | Narrow public observability exports. |
| `alembic/versions/007_phase12_job_trace_id.py` | Append-only trace-ID backfill, non-null column, and uniqueness constraint. |
| `app/db/models.py` | `AgentJob.trace_id` ORM field. |
| `app/domain/job_repository.py` | Persist/load traces and expose claimed-job lifecycle timestamps. |
| `app/domain/intake.py` | Carry server trace, observe idempotency outcomes, and return authoritative trace. |
| `app/main.py` | Configure JSON logging, observer state, and HTTP middleware. |
| `app/api/intake.py` | Pass request context and bind authoritative job trace. |
| `app/api/inbound.py` | Pass request context through webhook mapping and bind authoritative trace. |
| `app/api/approvals.py` | Pass request ID/observer into approval decision flow. |
| `app/auth/api_keys.py` | Safe API-key/operator authentication outcome events. |
| `app/auth/inbound_webhook.py` | Closed signature outcome and safe verification event. |
| `app/inbound/service.py` | Carry observation context and observe document normalization. |
| `app/documents/normalizer.py` | Optional observer/context/clock at the pure normalization boundary. |
| `app/providers/openai_compatible.py` | Provider call latency, validation retry/failure, HTTP outcome, optional usage counts. |
| `app/agent/loop.py` | Step/run events without transcript content. |
| `app/runtime/factory.py` | Build one trace-bound loop/provider/executor. |
| `app/policy/engine.py` | Preserve pure policy API; no global logger or session dependency. |
| `app/tools/executor.py` | Policy and tool events around the existing deterministic execution path. |
| `app/tools/postgres.py` | Preserve gateway/session ownership; no direct payload logging. |
| `app/domain/approval_repository.py` | Resolve original job trace for expiry/decision audit emission. |
| `app/domain/approvals.py` | Approval decision, expiry, and action-completion events. |
| `app/workers/agent_worker.py` | Job claim/run/retry/lease/final/persistence events and latency. |
| `app/workers/__main__.py` | Configure JSON logging for the worker process. |
| `.gitattributes` | Classify committed PDF fixtures as binary so Git neither line-ending converts them nor checks their binary bytes for source-code whitespace. |
| `evals/freight/runner.py` | Inject a fixed trace and recording observer without changing dataset/report contracts. |
| `tests/test_observability.py` | Core schemas, JSON rendering, fail-open behavior, and redaction. |
| `tests/test_observability_api.py` | HTTP trace creation, client correlation, auth/signature, response headers. |
| `tests/test_migrations.py` | Revision/head/schema/backfill contract. |
| Existing focused tests | Trace propagation and safe event assertions at each production seam. |
| `README.md` | Product-facing local observability behavior and honest boundaries. |
| `AGENTS.md` | Current architecture, commands, privacy rules, and extension guidance. |

Do not edit `pyproject.toml`, the existing Phase-11 dataset contents, fixture
contents, report shape, GitHub Actions workflow, applied Alembic revisions
001-006, tenant YAML, or public response models. Task 0 may add only the
repository-level `.gitattributes` classification; it must not rewrite PDF bytes
in the repository.

## Contracts fixed for all tasks

### Event and context shape

Use these public names consistently:

```python
@dataclass(frozen=True, slots=True)
class ObservationContext:
    trace_id: UUID
    request_id: UUID | None = None
    tenant_id: UUID | None = None
    job_id: UUID | None = None
    case_id: UUID | None = None
    approval_id: UUID | None = None

    def bind(self, **changes: UUID | None) -> "ObservationContext": ...


class Observer(Protocol):
    def emit(self, event: "ObservationEvent") -> None: ...


def safe_emit(observer: Observer, event: "ObservationEvent") -> None: ...
```

`safe_emit` catches observer failures, emits only the fixed standard-library
message `observability_emit_failed` without `exc_info`, and never changes the
business result.

`ObservationEvent` uses `ConfigDict(extra="forbid", frozen=True, strict=True)`,
`event_version: Literal[1] = 1`, UUID entity fields, non-negative integer
durations/counts, and enums for event/outcome/tool/policy/document/status data.
It contains no mapping for arbitrary metadata.

Required event names:

```python
class EventName(str, Enum):
    HTTP_REQUEST_COMPLETED = "http.request.completed"
    AUTH_COMPLETED = "auth.completed"
    INBOUND_SIGNATURE_COMPLETED = "inbound.signature.completed"
    INTAKE_ENQUEUE_COMPLETED = "intake.enqueue.completed"
    DOCUMENT_NORMALIZATION_COMPLETED = "document.normalization.completed"
    WORKER_JOB_CLAIMED = "worker.job.claimed"
    WORKER_JOB_RUN_STARTED = "worker.job.run_started"
    WORKER_JOB_RETRY_SCHEDULED = "worker.job.retry_scheduled"
    WORKER_JOB_LEASE_RECOVERED = "worker.job.lease_recovered"
    WORKER_JOB_FINISHED = "worker.job.finished"
    AGENT_STEP_COMPLETED = "agent.step.completed"
    AGENT_RUN_FINISHED = "agent.run.finished"
    PROVIDER_CALL_COMPLETED = "provider.call.completed"
    PROVIDER_STRUCTURED_OUTPUT_FAILED = "provider.structured_output.failed"
    POLICY_EVALUATED = "policy.evaluated"
    TOOL_EXECUTION_COMPLETED = "tool.execution.completed"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    APPROVAL_EXPIRED = "approval.expired"
    APPROVAL_ACTION_COMPLETED = "approval.action_completed"
    PERSISTENCE_OPERATION_FAILED = "persistence.operation_failed"
```

Use closed enums for `EventLevel`, `Component`, `OutcomeCode`, `RouteName`,
`ToolName`, `PolicyReason`, `PersistenceOperation`, and `WorkerErrorCode`.
Reuse existing `StopReason`, `RoutingDecision`, `RoutingStatus`,
`DocumentMediaType`, and `DocumentExtractionError` types instead of copying
their values. `missing_fields` may contain only sorted field-catalog names
matching `^[a-z][a-z0-9_]{0,99}$`.

The new enums are fixed to these exact values so an implementer does not add
free-text codes while instrumenting:

```python
class EventLevel(str, Enum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"

class Component(str, Enum):
    API = "api"
    AUTH = "auth"
    INBOUND = "inbound"
    INTAKE = "intake"
    DOCUMENT = "document"
    WORKER = "worker"
    AGENT = "agent"
    PROVIDER = "provider"
    POLICY = "policy"
    TOOL = "tool"
    APPROVAL = "approval"
    PERSISTENCE = "persistence"

class OutcomeCode(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    AUTHENTICATED = "authenticated"
    REJECTED = "rejected"
    VERIFIED = "verified"
    MISSING = "missing"
    MALFORMED = "malformed"
    STALE = "stale"
    MISMATCH = "mismatch"
    CREATED = "created"
    REUSED = "reused"
    CONFLICT = "conflict"
    CLAIMED = "claimed"
    STARTED = "started"
    RETRY_SCHEDULED = "retry_scheduled"
    RECOVERED = "recovered"
    SUCCEEDED = "succeeded"
    FAILED_UNCERTAIN = "failed_uncertain"
    COMPLETED = "completed"
    TERMINAL = "terminal"
    TOOL_REQUESTED = "tool_requested"
    INVALID_SCHEMA = "invalid_schema"
    TIMEOUT = "timeout"
    NETWORK_ERROR = "network_error"
    HTTP_ERROR = "http_error"
    ALLOW = "allow"
    DENY = "deny"
    NEEDS_APPROVAL = "needs_approval"
    APPROVED = "approved"
    EXPIRED = "expired"
    EXECUTED = "executed"
    NOT_FOUND = "not_found"
    INVALID_ARGUMENTS = "invalid_arguments"
    BACKEND_FAILURE = "backend_failure"
    NO_RESULT = "no_result"

class RouteName(str, Enum):
    HEALTH = "/health"
    CASES = "/v1/cases"
    CASE = "/v1/cases/{case_id}"
    INTAKE = "/v1/intake"
    INBOUND_EMAIL = "/v1/inbound/email/webhook"
    APPROVAL_DECIDE = "/v1/approvals/{approval_id}/decide"
    UNKNOWN = "unknown"

class CredentialKind(str, Enum):
    SERVICE_API_KEY = "service_api_key"
    OPERATOR_API_KEY = "operator_api_key"
    INBOUND_WEBHOOK = "inbound_webhook"

class ToolName(str, Enum):
    FIND_CUSTOMER = "find_customer"
    CREATE_CASE = "create_case"
    UPDATE_CASE_FIELDS = "update_case_fields"
    CREATE_REPLY_DRAFT = "create_reply_draft"
    FLAG_FOR_REVIEW = "flag_for_review"

class PolicyReason(str, Enum):
    SAFETY_OR_LEGAL_RISK = "safety_or_legal_risk"
    UNKNOWN_INTAKE_TYPE = "unknown_intake_type"
    MISSING_REQUIRED_FIELDS = "missing_required_fields"
    ACTION_NOT_CONFIGURED = "action_not_configured"
    ACTION_NOT_ALLOWED = "action_not_allowed"
    APPROVAL_REQUIRED = "approval_required"
    ACTION_ALLOWED = "action_allowed"
    DOCUMENT_UNREADABLE = "document_unreadable"

class PersistenceOperation(str, Enum):
    ENQUEUE_JOB = "enqueue_job"
    CLAIM_JOB = "claim_job"
    RECOVER_LEASE = "recover_lease"
    SCHEDULE_RETRY = "schedule_retry"
    MARK_SUCCEEDED = "mark_succeeded"
    MARK_FAILED = "mark_failed"
    MARK_FAILED_UNCERTAIN = "mark_failed_uncertain"
    CREATE_APPROVAL = "create_approval"
    DECIDE_APPROVAL = "decide_approval"

class WorkerErrorCode(str, Enum):
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    RETRY_EXHAUSTED = "retry_exhausted"
    SIDE_EFFECT_COMMITTED = "side_effect_committed"
    UNEXPECTED_AFTER_SIDE_EFFECT = "unexpected_after_side_effect"
    WORKER_ERROR = "worker_error"
    AGENT_FAILED = "agent_failed"
    AGENT_FAILED_AFTER_SIDE_EFFECT = "agent_failed_after_side_effect"
    LEASE_EXPIRED = "lease_expired"
    LEASE_EXPIRED_AFTER_SIDE_EFFECT = "lease_expired_after_side_effect"
    PERSISTENCE_ERROR = "persistence_error"
```

### Forbidden event data

No event constructor or observer accepts source body/subject/sender, provider
ID, idempotency key, document digest/text/bytes/base64, source excerpt,
proposal/rationale, tool arguments/results, approval reason/action payload,
authorization/signature headers, API keys/digests/prefixes, database URL, SQL,
exception message/traceback, or transcript messages.

### Trace semantics

- Middleware creates a server UUID `request_id` and provisional `trace_id`.
- A new job persists that trace.
- Duplicate intake returns the existing job's trace as authoritative.
- `X-Correlation-ID` is optional, request-only, and valid only when it matches
  `^[A-Za-z0-9._:-]{1,128}$`; invalid raw values are never logged.
- `X-Trace-ID` is an additive response header containing only a server UUID.
- `job_id` remains a correlated entity ID and is not overloaded as trace ID.
- Approval decision events resolve the original trace through the tenant-owned
  job; unknown/cross-tenant approvals retain their existing 404 behavior.

### Timing and metrics

Use `time.perf_counter_ns()` and convert with integer floor division:

```python
duration_ms = max(0, (clock() - started_ns) // 1_000_000)
```

Do not sleep in timing tests. Provider token fields are absent unless the
response supplies non-negative integer `prompt_tokens`, `completion_tokens`,
or `total_tokens`. Do not calculate money.

### Compatibility invariants

Preserve API bodies/statuses, server-derived tenancy, idempotency, sync
`AgentLoop`, async SQLAlchemy ownership, `MAX_STEPS=8`, third-identical-tool
guard, explicit `None` results, strict `AgentProposal`, one schema retry,
deterministic policy precedence, approval authority, no Markdown parsing, no
LangChain, and the current safe eval report.

---

## Task 0: Repair the CI whitespace gate for binary PDF fixtures

**Root cause fixed:** on the commit immediately before this plan, the existing
`Check whitespace` step executes `git diff --check HEAD^ HEAD`. Because the
repository has no `.gitattributes`, Git treats the three valid PDF fixtures as
text and reports their fixed-width xref rows as trailing whitespace, returning
exit code `2`. On a Windows checkout with `core.autocrlf=true`, the same missing
classification also materializes index-LF PDFs as working-tree CRLF and breaks
their xref offsets, producing local `pdf_malformed` test failures. The workflow
is correct to keep checking code/YAML whitespace; do not exclude paths or
weaken that command.

**Files:**
- Create: `.gitattributes`
- Edit: `tests/test_freight_eval_models.py`
- Verify unchanged: `.github/workflows/ci.yml`
- Verify unchanged: `evals/fixtures/docs/03-complete.pdf`
- Verify unchanged: `evals/fixtures/docs/04-complete-two-page.pdf`
- Verify unchanged: `evals/fixtures/docs/06-complete-injection.pdf`
- Verify unchanged: `evals/fixtures/docs/08-malformed.pdf`

- [x] **Step 1: Add a failing repository-hygiene contract through `unit-test-writer`.**

In `tests/test_freight_eval_models.py`, next to the existing CI workflow
contracts, add this test and the `ATTRIBUTES_PATH` constant based on the
existing `PROJECT_ROOT`:

```python
ATTRIBUTES_PATH = PROJECT_ROOT / ".gitattributes"


def test_pdf_fixtures_are_declared_binary_for_git() -> None:
    assert ATTRIBUTES_PATH.is_file(), "missing .gitattributes"
    rules = ATTRIBUTES_PATH.read_text(encoding="utf-8").splitlines()
    assert "*.pdf binary" in rules
```

The contract is intentionally narrow: `binary` is Git's built-in macro that
sets `-diff`, `-merge`, and `-text`; it leaves the existing CI `git diff
--check` intact for all source-like files.

- [x] **Step 2: Prove the current repository lacks the classification.**

Run:

```powershell
pytest tests/test_freight_eval_models.py -q
git diff --check HEAD^ HEAD
Write-Output "git_diff_check_exit=$LASTEXITCODE"
git ls-files --eol evals/fixtures/docs/03-complete.pdf evals/fixtures/docs/04-complete-two-page.pdf evals/fixtures/docs/06-complete-injection.pdf
```

Expected before the implementation: the new test fails because
`.gitattributes` is absent; `git diff --check` reports only PDF xref rows and
prints `git_diff_check_exit=2`; Windows worktrees may report `i/lf w/crlf`.
Record the output as baseline evidence, but do not upload it or add its binary
lines to an artifact.

- [x] **Step 3: Add the binary classification without changing CI behavior.**

Create `.gitattributes` with exactly this rule and a final newline:

```gitattributes
*.pdf binary
```

Do not add a path exclusion to `.github/workflows/ci.yml`, disable
`git diff --check`, or add a `working-tree-encoding` rule. Those alternatives
would either hide real whitespace errors or risk changing fixture bytes.

- [x] **Step 4: Safely refresh only locally line-ending-converted fixtures when needed.**

First prove the four committed PDF fixture paths have no user edits:

```powershell
git diff --quiet -- evals/fixtures/docs/03-complete.pdf evals/fixtures/docs/04-complete-two-page.pdf evals/fixtures/docs/06-complete-injection.pdf evals/fixtures/docs/08-malformed.pdf
if ($LASTEXITCODE -ne 0) { throw "PDF fixtures have local edits; stop and ask the user before refreshing them." }
```

Only when `git ls-files --eol` shows a `w/crlf` value, restore these exact
tracked blobs. Because `.gitattributes` is deliberately not staged by this
workflow, `checkout-index` can still use the old global text attribute on a
Windows checkout; materialize each blob through a temporary file and verify
its object ID before replacing the clean working-tree path:

```powershell
$paths = @('evals/fixtures/docs/03-complete.pdf','evals/fixtures/docs/04-complete-two-page.pdf','evals/fixtures/docs/06-complete-injection.pdf','evals/fixtures/docs/08-malformed.pdf')
$tempRoot = Join-Path (Get-Location) '.pdf-fixture-blob-temp'
if (Test-Path -LiteralPath $tempRoot) { throw "Temporary path already exists: $tempRoot" }
New-Item -ItemType Directory -Path $tempRoot | Out-Null
try {
  foreach ($path in $paths) {
    $tempFile = Join-Path $tempRoot ([IO.Path]::GetFileName($path))
    $spec = 'HEAD:' + $path
    cmd.exe /c "git cat-file blob $spec > `"$tempFile`""
    if ($LASTEXITCODE -ne 0) { throw "Could not materialize $path" }
    if ((git hash-object -- $tempFile).Trim() -ne (git rev-parse $spec).Trim()) {
      throw "Blob mismatch for $path"
    }
    Move-Item -LiteralPath $tempFile -Destination (Join-Path (Get-Location) $path) -Force
  }
}
finally {
  if (Test-Path -LiteralPath $tempRoot) { Remove-Item -LiteralPath $tempRoot -Force -Recurse }
}
git ls-files --eol evals/fixtures/docs/03-complete.pdf evals/fixtures/docs/04-complete-two-page.pdf evals/fixtures/docs/06-complete-injection.pdf evals/fixtures/docs/08-malformed.pdf
```

Expected: the command only re-materializes clean tracked bytes; PDF fixture
content remains unchanged in the index and the working-tree column is `w/lf`.
If no `w/crlf` is present, skip the refresh rather than overwriting files.

- [x] **Step 5: Prove the existing CI command and local PDF tests now pass.**

Run:

```powershell
pytest tests/test_freight_eval_models.py tests/test_documents.py -q
git diff --check HEAD^ HEAD
Write-Output "git_diff_check_exit=$LASTEXITCODE"
git diff -- .github/workflows/ci.yml evals/fixtures
```

Expected: tests pass; `git diff --check` has no output and prints
`git_diff_check_exit=0`; the workflow and fixture contents have no diff. This
is the required reproduction of the previously failing GitHub Actions step,
without credentials, network calls, or a real Actions run.

---

## Task 1: Add the typed observability core

**Files:**
- Create: `app/observability/events.py`
- Create: `app/observability/context.py`
- Create: `app/observability/observer.py`
- Create: `app/observability/logging.py`
- Create: `app/observability/__init__.py`
- Create: `tests/test_observability.py`

- [ ] **Step 1: Delegate failing core-contract tests through `unit-test-writer`.**

Ask the skill to create `tests/test_observability.py` with these exact tests:

```python
def test_observation_event_is_frozen_closed_and_json_safe() -> None: ...
def test_event_rejects_unknown_fields_negative_counts_and_free_text_codes() -> None: ...
def test_context_bind_returns_a_copy_without_mutating_server_trace() -> None: ...
def test_safe_emit_swallows_sink_failure_without_logging_exception_text(caplog) -> None: ...
def test_structlog_observer_outputs_one_json_object_with_only_model_fields(capsys) -> None: ...
def test_serialized_event_excludes_secret_source_and_transcript_sentinels() -> None: ...
def test_json_logging_configuration_is_idempotent() -> None: ...
```

Use sentinels `ik_SECRET`, `Bearer SECRET`, `SOURCE_BODY_SECRET`,
`DOCUMENT_TEXT_SECRET`, `RATIONALE_SECRET`, and `TOOL_RESULT_SECRET`. Assert
that none appears in `model_dump_json()`, captured output, `repr(event)`, or the
fixed fallback log.

- [ ] **Step 2: Run the core tests and confirm the missing-package failure.**

Run:

```powershell
pytest tests/test_observability.py -q
```

Expected: collection fails with `ModuleNotFoundError: app.observability`.

- [ ] **Step 3: Implement immutable context and closed event models.**

Implement the contracts above. The concrete event model must have this field
surface and no generic `data`, `payload`, `metadata`, or `exception` field:

```python
class ObservationEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    event: EventName
    event_version: Literal[1] = 1
    level: EventLevel = EventLevel.INFO
    trace_id: UUID
    request_id: UUID | None = None
    tenant_id: UUID | None = None
    job_id: UUID | None = None
    case_id: UUID | None = None
    approval_id: UUID | None = None
    component: Component
    outcome: OutcomeCode | None = None
    duration_ms: Annotated[int, Field(ge=0)] | None = None
    queue_latency_ms: Annotated[int, Field(ge=0)] | None = None
    total_latency_ms: Annotated[int, Field(ge=0)] | None = None
    route: RouteName | None = None
    method: Literal["GET", "POST"] | None = None
    status_code: Annotated[int, Field(ge=100, le=599)] | None = None
    request_bytes: Annotated[int, Field(ge=0)] | None = None
    attempt_count: Annotated[int, Field(ge=0)] | None = None
    retry_delay_ms: Annotated[int, Field(ge=0)] | None = None
    step: Annotated[int, Field(ge=1, le=8)] | None = None
    steps: Annotated[int, Field(ge=0, le=8)] | None = None
    stop_reason: StopReason | None = None
    tool_name: ToolName | None = None
    policy_decision: RoutingDecision | None = None
    routing_status: RoutingStatus | None = None
    policy_reason: PolicyReason | None = None
    missing_fields: tuple[SafeFieldName, ...] = ()
    document_media_type: DocumentMediaType | None = None
    document_error: DocumentExtractionError | None = None
    document_bytes: Annotated[int, Field(ge=0)] | None = None
    document_pages: Annotated[int, Field(ge=0)] | None = None
    document_text_chars: Annotated[int, Field(ge=0)] | None = None
    provider_call_index: Annotated[int, Field(ge=1, le=2)] | None = None
    validation_retry: bool | None = None
    prompt_tokens: Annotated[int, Field(ge=0)] | None = None
    completion_tokens: Annotated[int, Field(ge=0)] | None = None
    total_tokens: Annotated[int, Field(ge=0)] | None = None
    result_is_none: bool | None = None
    side_effect_committed: bool | None = None
    credential_kind: CredentialKind | None = None
    client_correlation_id: ClientCorrelationId | None = None
    client_correlation_invalid: bool | None = None
    persistence_operation: PersistenceOperation | None = None
    worker_error_code: WorkerErrorCode | None = None
```

Validators sort/deduplicate `missing_fields`, forbid a client-correlation value
when `client_correlation_invalid=True`, and require `total_tokens ==
prompt_tokens + completion_tokens` only when all three are present.

- [ ] **Step 4: Implement observer and JSON logging adapters.**

`NullObserver.emit` is a no-op. `StructlogObserver.emit` must call
`event.model_dump(mode="json", exclude_none=True)`, remove the `event` and
`level` keys, and dispatch to the matching bound logger method. Configure UTC
ISO timestamp, log level, `event`, deterministic JSON rendering, and stdout.
Do not configure stack or exception rendering and do not pass arbitrary
objects to structlog.

- [ ] **Step 5: Run core observability tests.**

Run:

```powershell
pytest tests/test_observability.py -q
```

Expected: all core schema, fail-open, JSON, and redaction tests pass.

---

## Task 2: Persist the root trace on agent jobs

**Files:**
- Create: `alembic/versions/007_phase12_job_trace_id.py`
- Edit: `app/db/models.py`
- Edit: `app/domain/job_repository.py`
- Edit: `tests/test_migrations.py`
- Edit: `tests/test_job_repository.py`

- [ ] **Step 1: Delegate trace-persistence tests through `unit-test-writer`.**

Require tests that assert:

```python
assert AgentJob.__table__.c.trace_id.nullable is False
assert AgentJob.__table__.c.trace_id.type.as_uuid is True
assert "uq_agent_jobs_trace_id" in unique_constraint_names
assert claimed.trace_id == persisted.trace_id
assert claimed.created_at == persisted.created_at
assert claimed.started_at == persisted.started_at
```

Migration tests must require revision `007_phase12_job_trace_id`, down revision
`006_phase8_approval_status_check`, nullable-add/backfill/non-null sequence,
unique constraint creation, and the reverse downgrade. They must reject edits
to revisions 001-006.

- [ ] **Step 2: Run focused persistence tests and confirm the missing-column failure.**

Run:

```powershell
pytest tests/test_migrations.py tests/test_job_repository.py -q
```

Expected: only new trace-column/revision/claimed-timestamp assertions fail.

- [ ] **Step 3: Add the ORM column and append-only migration.**

Use this migration sequence:

```python
revision = "007_phase12_job_trace_id"
down_revision = "006_phase8_approval_status_check"

def upgrade() -> None:
    op.add_column(
        "agent_jobs",
        sa.Column("trace_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.execute("UPDATE agent_jobs SET trace_id = gen_random_uuid() WHERE trace_id IS NULL")
    op.alter_column(
        "agent_jobs",
        "trace_id",
        existing_type=postgresql.UUID(as_uuid=True),
        nullable=False,
    )
    op.create_unique_constraint(
        "uq_agent_jobs_trace_id", "agent_jobs", ["trace_id"]
    )

def downgrade() -> None:
    op.drop_constraint("uq_agent_jobs_trace_id", "agent_jobs", type_="unique")
    op.drop_column("agent_jobs", "trace_id")
```

Set `AgentJob.trace_id` to `mapped_column(PG_UUID(as_uuid=True), nullable=False,
default=uuid4)` so in-memory unit models receive a UUID. Do not use a trace
server default for newly created application rows; enqueue supplies it.

- [ ] **Step 4: Extend job creation and claiming.**

Add `trace_id`, `created_at`, `available_at`, and `started_at` to `ClaimedJob`.
`create_job` writes `command.trace_id`; `claim_next` copies the persisted values.
Add this tenant-scoped helper for duplicate resolution and approvals:

```python
async def get_trace_id(
    self, job_id: UUID, *, tenant_id: UUID
) -> UUID | None:
    statement = select(AgentJob.trace_id).where(
        AgentJob.id == job_id,
        AgentJob.tenant_id == tenant_id,
    )
    return (await self.session.execute(statement)).scalar_one_or_none()
```

- [ ] **Step 5: Run persistence tests.**

Run:

```powershell
pytest tests/test_migrations.py tests/test_job_repository.py -q
```

Expected: all focused tests pass; database-only tests may remain skipped without
an explicit reachable `DATABASE_URL`.

---

## Task 3: Add HTTP request correlation and safe authentication events

**Files:**
- Create: `app/observability/middleware.py`
- Edit: `app/main.py`
- Edit: `app/auth/api_keys.py`
- Edit: `app/auth/inbound_webhook.py`
- Create: `tests/test_observability_api.py`
- Edit: `tests/test_auth.py`
- Edit: `tests/test_health.py`

- [ ] **Step 1: Delegate middleware/auth tests through `unit-test-writer`.**

Cover:

- every request gets a valid server UUID `X-Trace-ID`;
- a valid `X-Correlation-ID` appears only in the request event;
- an invalid/oversized correlation value is not echoed or logged and sets only
  `client_correlation_invalid=true`;
- a client `X-Trace-ID` header is ignored;
- route templates, not raw paths/query strings, are logged;
- auth success/rejection emits no raw key, prefix, digest, actor reason, or
  headers;
- signature outcomes distinguish `verified`, `missing`, `malformed`, `stale`,
  and `mismatch` internally while preserving generic 401 responses;
- middleware re-raises application exceptions after a safe 500 event;
- `/health` response body remains exactly `{"status":"ok","env":"..."}`.

- [ ] **Step 2: Run API observability tests and confirm missing middleware/events.**

Run:

```powershell
pytest tests/test_observability_api.py tests/test_auth.py tests/test_health.py -q
```

Expected: new event/header assertions fail; existing auth and health tests pass.

- [ ] **Step 3: Implement request state helpers and ASGI middleware.**

The middleware must:

```python
request_id = uuid4()
trace_id = uuid4()
scope["state"]["observation_context"] = ObservationContext(
    trace_id=trace_id,
    request_id=request_id,
)
scope["state"]["client_correlation_id"] = validated_value_or_none
scope["state"]["client_correlation_invalid"] = invalid_boolean
```

Intercept `http.response.start` to add `X-Trace-ID` from the latest request
state context. Emit one `http.request.completed` event in `finally`, using the
matched route template after routing, method, status, content-length only when
it is a valid non-negative integer, and monotonic duration. Do not read or
buffer the body.

Expose helpers:

```python
def request_observer(request: Request) -> Observer: ...
def request_context(request: Request) -> ObservationContext: ...
def bind_request_context(request: Request, context: ObservationContext) -> None: ...
```

- [ ] **Step 4: Configure API logging and observer state.**

In `app/main.py`, call the idempotent logging setup, set
`app.state.observer = StructlogObserver()`, and register the middleware. Do not
alter router prefixes, application metadata, startup behavior, or health body.

- [ ] **Step 5: Add detailed internal signature outcome without changing the public bool helper.**

Define:

```python
class InboundSignatureOutcome(str, Enum):
    VERIFIED = "verified"
    MISSING = "missing"
    MALFORMED = "malformed"
    STALE = "stale"
    MISMATCH = "mismatch"

def inbound_signature_outcome(...) -> InboundSignatureOutcome: ...

def verify_inbound_webhook_signature(...) -> bool:
    return inbound_signature_outcome(...) is InboundSignatureOutcome.VERIFIED
```

The FastAPI dependency emits only the enum outcome and continues returning the
same generic authentication error. Add optional `Request | None = None`
parameters to direct-callable auth dependencies so existing PostgreSQL tests
can still call them without constructing HTTP requests.

- [ ] **Step 6: Run API/auth tests.**

Run:

```powershell
pytest tests/test_observability_api.py tests/test_auth.py tests/test_health.py -q
```

Expected: all pass with unchanged public response bodies.

---

## Task 4: Propagate authoritative traces through intake, idempotency, and documents

**Files:**
- Edit: `app/domain/intake.py`
- Edit: `app/inbound/service.py`
- Edit: `app/documents/normalizer.py`
- Edit: `app/api/intake.py`
- Edit: `app/api/inbound.py`
- Edit: `tests/test_intake.py`
- Edit: `tests/test_inbound_webhook.py`
- Edit: `tests/test_documents.py`
- Edit: `tests/test_observability_api.py`

- [ ] **Step 1: Delegate failing intake/document trace tests through `unit-test-writer`.**

Require tests for:

- a new command persists its server trace and returns it in `EnqueueResult`;
- duplicate reuse returns the stored job trace rather than the new request's
  provisional trace;
- changed-payload conflict emits `conflict` without logging the key/body;
- route state is rebound so the response header uses the authoritative trace;
- direct and webhook document normalization emit duration, media type, byte or
  character count, and closed extraction outcome;
- malformed PDF events contain no bytes, digest, parser exception, source text,
  or base64;
- persistence failure emits `persistence.operation_failed` with only
  `enqueue_job` and a closed code, then preserves the original exception.

- [ ] **Step 2: Run focused tests and confirm trace fields/events are absent.**

Run:

```powershell
pytest tests/test_intake.py tests/test_inbound_webhook.py tests/test_documents.py tests/test_observability_api.py -q
```

Expected: only new trace/event assertions fail.

- [ ] **Step 3: Extend internal enqueue contracts.**

Use these signatures:

```python
@dataclass(frozen=True, slots=True)
class EnqueueIntakeCommand:
    tenant_id: UUID
    tenant_slug: str
    source: TrustedSource
    idempotency_key: str
    trace_id: UUID = field(default_factory=uuid4)
    risk_signals: RiskSignals = field(default_factory=RiskSignals)

@dataclass(frozen=True, slots=True)
class EnqueueResult:
    job_id: UUID
    created: bool
    trace_id: UUID
```

`IntakeEnqueueService` accepts keyword-only `observer: Observer = NULL_OBSERVER`
and `context: ObservationContext | None = None`. On duplicate, call
`JobRepository.get_trace_id(existing.job_id, tenant_id=...)`; missing job/trace
remains a runtime consistency error. Emit one enqueue event with the
authoritative trace for `created` or `reused`. On conflict, emit only the
provisional trace and closed outcome before re-raising.

- [ ] **Step 4: Instrument document normalization without changing normalized data.**

Add optional keyword-only `observer`, `context`, and `clock` to
`DocumentNormalizer`. Wrap the existing exact normalization algorithm in a
timed public method and move the unchanged logic to `_normalize`. Emit only
media type, input byte count, safe page/text counts when known, duration, and
`success` or the existing extraction enum. Never add observability data to
`NormalizedRateConfirmationDocument`.

`InboundIntakeService` accepts observer/context and passes them through its
`asyncio.to_thread` call. Context objects are immutable and safe to cross the
thread boundary.

- [ ] **Step 5: Bind request context in the two intake routes.**

Both routes obtain observer/context from the HTTP request, construct services
with them, and after enqueue call:

```python
bind_request_context(
    http_request,
    context.bind(tenant_id=tenant.id, job_id=result.job_id, trace_id=result.trace_id),
)
```

Do not add `trace_id` to `IntakeAcceptedResponse`; the response body remains
`job_id` plus `status="queued"`.

- [ ] **Step 6: Run intake/document/webhook regressions.**

Run:

```powershell
pytest tests/test_intake.py tests/test_inbound_webhook.py tests/test_documents.py tests/test_observability_api.py -q
```

Expected: all pass, including privacy and duplicate/conflict tests.

---

## Task 5: Observe provider calls and agent-loop control flow

**Files:**
- Edit: `app/providers/openai_compatible.py`
- Edit: `app/agent/loop.py`
- Edit: `app/runtime/factory.py`
- Edit: `tests/test_openai_compatible_provider.py`
- Edit: `tests/test_agent_loop.py`
- Edit: `tests/test_runtime.py`

- [ ] **Step 1: Delegate provider/loop observability tests through `unit-test-writer`.**

Tests must prove:

- one valid response emits one provider call with duration and call index 1;
- first invalid then valid emits invalid-schema call 1 and valid call 2 with
  `validation_retry=true`;
- two invalid responses emit the two call events plus one exhausted structured
  failure event, without response content or validation input;
- optional usage integers are recorded and malformed/negative usage is ignored;
- HTTP 429/5xx/network/timeout outcomes never log URL, headers, body, or key;
- terminal, executor-stopped, repeated-tool, max-step, and invalid-proposal
  paths emit exact step/run events;
- explicit `None` is represented only by `result_is_none=true` on the tool
  event later, not by serializing a result;
- observer failure never changes loop status/messages/steps;
- runtime factory gives loop, provider, and executor the same trace context.

- [ ] **Step 2: Run focused tests and confirm missing observation arguments.**

Run:

```powershell
pytest tests/test_openai_compatible_provider.py tests/test_agent_loop.py tests/test_runtime.py -q
```

Expected: new event assertions fail; existing strict schema and loop tests pass.

- [ ] **Step 3: Instrument the provider adapter.**

Add keyword-only `observer`, `context`, and `clock` constructor arguments with
safe defaults. Refactor the duplicated validation attempt into a two-attempt
loop while preserving exactly one retry and the existing sanitized exception.
`_complete_once` returns a private typed pair of proposal plus optional usage;
usage parsing accepts only non-boolean non-negative integers.

Every HTTP attempt emits `provider.call.completed` in `finally` with call
index, duration, retry boolean, outcome, safe HTTP status when available, and
usage only after a valid response. The exhausted validation event contains only
the closed outcome `invalid_schema`. Never log `_endpoint`, `_api_key`, request,
response, feedback, or exception values.

- [ ] **Step 4: Instrument every AgentLoop return path.**

Add keyword-only observer/context/clock defaults. Use private helpers
`_step_event(...)` and `_finish(...)` so every existing return creates exactly
one run-finished event without duplicating control logic. Step events contain
only step number, terminal flag represented by outcome, and requested tool
name. Catch provider exceptions only to emit a failed run event and re-raise;
do not classify retry policy in the loop.

Keep the current order: provider proposal, assistant snapshot, terminal check,
tool validation, repeated-tool guard, executor call, tool snapshot, executor
stop, max-step stop.

- [ ] **Step 5: Bind one context in AgentRuntimeFactory.**

Construct:

```python
context = ObservationContext(
    trace_id=claimed_job.trace_id,
    tenant_id=claimed_job.tenant_id,
    job_id=claimed_job.id,
)
```

Pass the same observer/context to `OpenAICompatibleLLMClient`,
`PolicyGatedToolExecutor`, and `AgentLoop`. Preserve the custom `llm_factory`
signature and unreadable-document local fallback so tests/evals do not gain a
provider dependency.

- [ ] **Step 6: Run provider/loop/runtime tests.**

Run:

```powershell
pytest tests/test_openai_compatible_provider.py tests/test_agent_loop.py tests/test_runtime.py -q
```

Expected: all pass; API-key/raw-provider redaction tests remain green.

---

## Task 6: Observe deterministic policy, tools, and approval requests

**Files:**
- Edit: `app/tools/executor.py`
- Edit: `app/tools/models.py`
- Edit: `tests/test_policy.py`
- Edit: `tests/test_tools.py`

- [ ] **Step 1: Delegate policy/tool event tests through `unit-test-writer`.**

Cover allow, deny, needs-approval, unknown action, invalid arguments,
customer/case not found, backend exception, unreadable document, handler
`None`, approval request, and successful side effect. Assert:

```python
assert policy_event.policy_decision == expected_decision
assert policy_event.routing_status == expected_status
assert policy_event.policy_reason == expected_reason
assert tool_event.tool_name == expected_tool
assert tool_event.result_is_none is expected_none
assert "arguments" not in tool_event.model_fields
assert "result" not in tool_event.model_fields
```

Use source/rationale/tool-result sentinels and assert they are absent from all
captured events and logs. Unknown or policy-denied actions emit a policy event
but no tool-completion event because no registered handler executed.

- [ ] **Step 2: Run focused tests and confirm no policy/tool events exist.**

Run:

```powershell
pytest tests/test_policy.py tests/test_tools.py -q
```

Expected: new recording-observer assertions fail; existing policy behavior
passes.

- [ ] **Step 3: Add observer/context/timing to PolicyGatedToolExecutor.**

Keep `PolicyEngine.evaluate` pure. Time it at the executor boundary and emit
one `policy.evaluated` event immediately after it returns. Convert reason via
the closed `PolicyReason` enum; if a future unknown reason appears, drop the
event safely rather than log free text.

Wrap handler execution so every recognized tool produces one
`tool.execution.completed` event. Use the returned stable outcome or a closed
fallback; record `result_is_none`, duration, and side-effect boolean. The
backend `except Exception` branch continues returning
`tool_execution_failed`, emits no exception type/text, and does not escape the
loop.

- [ ] **Step 4: Emit approval request metadata only after durable creation.**

After `request_approval` returns, bind its ID into a copied context and emit
`approval.requested` with tool name and policy reason. Never serialize
`PendingAction`, known fields, proposal, expiry timestamp, or policy source.

- [ ] **Step 5: Run policy/tool tests.**

Run:

```powershell
pytest tests/test_policy.py tests/test_tools.py -q
```

Expected: all pass while current decision precedence and transcript assertions
remain unchanged.

---

## Task 7: Observe approval decisions and preserve their original root trace

**Files:**
- Edit: `app/domain/approval_repository.py`
- Edit: `app/domain/approvals.py`
- Edit: `app/api/approvals.py`
- Edit: `tests/test_approvals.py`
- Edit: `tests/test_approvals_api.py`
- Edit: `tests/test_postgres_approvals.py`

- [ ] **Step 1: Delegate approval trace/event tests through `unit-test-writer`.**

Require tests for requested trace resolution, approve, reject, timeout expiry,
execute-once replay, opposite-decision conflict, cross-tenant 404, malformed
pending action, and action-executor rollback. Assert decision reason,
`PendingAction`, actor ref, execution result, and source values never enter
events.

- [ ] **Step 2: Run approval tests and confirm missing trace lookup/events.**

Run:

```powershell
pytest tests/test_approvals.py tests/test_approvals_api.py tests/test_postgres_approvals.py -q
```

Expected: new trace/event assertions fail; PostgreSQL tests may skip only when
the configured database is unreachable.

- [ ] **Step 3: Add tenant-scoped original-trace lookup.**

Add to `ApprovalRepository`:

```python
async def get_job_trace_id(
    self, approval: Approval
) -> UUID:
    if approval.job_id is None:
        raise ApprovalCreationConflict("approval job is unavailable")
    statement = select(AgentJob.trace_id).where(
        AgentJob.id == approval.job_id,
        AgentJob.tenant_id == approval.tenant_id,
    )
    trace_id = (await self.session.execute(statement)).scalar_one_or_none()
    if trace_id is None:
        raise ApprovalCreationConflict("approval job is unavailable")
    return trace_id
```

Do not expose this value in approval response bodies or event payload JSON.

Change `expire_due` to return a tuple of frozen `ExpiredApprovalObservation`
records for executable job-bound approvals while still expiring legacy rows:

```python
@dataclass(frozen=True, slots=True)
class ExpiredApprovalObservation:
    trace_id: UUID
    tenant_id: UUID
    job_id: UUID
    approval_id: UUID
    created_at: datetime
    decided_at: datetime
```

Resolve `AgentJob.trace_id` with both job and tenant predicates inside the same
transaction. A legacy approval without a job still expires but produces no
trace record because no trustworthy root exists.

- [ ] **Step 4: Instrument ApprovalDecisionService.**

Accept observer, request ID, and monotonic clock. After the tenant-scoped row
is locked, resolve the original trace and construct a context containing
tenant/job/approval. Emit:

- `approval.decided` for approved/rejected/expired final outcomes;
- `approval.expired` for timeout transition;
- `approval.action_completed` only after executor success.

Decision latency is `now - approval.created_at`, clamped to non-negative whole
milliseconds. Replayed same decisions may emit `approval.decided` with outcome
`reused` but must not emit a second action-completed event.

- [ ] **Step 5: Pass request observer/request ID from the route.**

Keep the existing endpoint body/status mapping. The HTTP request's own trace
header remains a request support ID; approval business events use the original
job trace resolved by the service.

- [ ] **Step 6: Run approval tests.**

Run:

```powershell
pytest tests/test_approvals.py tests/test_approvals_api.py tests/test_postgres_approvals.py -q
```

Expected: unit/API tests pass; report PostgreSQL pass or skip separately.

---

## Task 8: Observe worker lifecycle, retries, leases, and persistence failures

**Files:**
- Edit: `app/workers/agent_worker.py`
- Edit: `app/workers/__main__.py`
- Edit: `app/domain/job_repository.py`
- Edit: `tests/test_agent_worker.py`
- Edit: `tests/test_job_repository.py`

- [ ] **Step 1: Delegate worker event tests through `unit-test-writer`.**

Cover no-job polling, claim, successful final, executor-stopped final,
agent-failed, retry scheduled, retry exhausted, failure after side effect,
unexpected failure, lease recovery to queued/failed/failed-uncertain, and a
repository failure while finalizing. Require exact attempt, stop reason, steps,
retry delay, safe worker error code, and non-negative queue/run/total latency.
Use an exception containing `DATABASE_URL_SECRET` and assert it is absent from
events, logs, and persisted codes.

- [ ] **Step 2: Run worker tests and confirm lifecycle events are absent.**

Run:

```powershell
pytest tests/test_agent_worker.py tests/test_job_repository.py -q
```

Expected: new event assertions fail; current worker state tests pass.

- [ ] **Step 3: Inject one observer and clock into AgentWorker.**

Add keyword-only `observer: Observer = NULL_OBSERVER` and
`monotonic_clock: Callable[[], int] = time.perf_counter_ns`. After claim, build
context from the persisted trace/tenant/job and emit `worker.job.claimed` then
`worker.job.run_started`. Measure `_run_claimed_job` around the existing
`to_thread` call.

Emit exactly one `worker.job.finished` for every claimed terminal/scheduled
outcome. Compute:

```python
queue_latency_ms = max(0, int((claimed.started_at - claimed.created_at).total_seconds() * 1000))
total_latency_ms = max(0, int((now - claimed.created_at).total_seconds() * 1000))
```

Use `duration_ms` for the measured worker run, `queue_latency_ms` for the
persisted create-to-start interval, and `total_latency_ms` for create-to-final
time. These three non-negative fields are fixed in the Task-1 event schema and
must not be overloaded.

- [ ] **Step 4: Return typed lease-recovery observations.**

Change `recover_expired_leases` from an integer to a tuple of frozen records
containing only trace ID, tenant ID, job ID, attempt, and resulting closed
status/error code. The worker emits one `worker.job.lease_recovered` per record.
Update existing tests and callers; there is only one production caller.

Consume the `ExpiredApprovalObservation` tuple returned by
`ApprovalRepository.expire_due` before claiming jobs and emit one
`approval.expired` event per record on the original trace. This covers timeout
expiry performed by the worker rather than an operator request.

- [ ] **Step 5: Emit safe persistence failure events without swallowing errors.**

Wrap repository calls in named private helpers. On exception, emit
`persistence.operation_failed` with a closed `PersistenceOperation` such as
`claim_job`, `schedule_retry`, `mark_succeeded`, `mark_failed`, or
`mark_failed_uncertain`; never include exception type/text. Preserve existing
retry/uncertain behavior and re-raise when the current code would re-raise.

- [ ] **Step 6: Configure worker JSON logging.**

Call the idempotent logging setup in `app/workers/__main__.py` before
constructing `AgentWorker.from_settings`. `from_settings` creates one
`StructlogObserver` and supplies it to both runtime factory and worker so events
are not split across independent context owners.

- [ ] **Step 7: Run worker regressions.**

Run:

```powershell
pytest tests/test_agent_worker.py tests/test_job_repository.py tests/test_runtime.py -q
```

Expected: all pass with preserved thread/gateway behavior.

---

## Task 9: Prove end-to-end trace propagation in deterministic freight seams

**Files:**
- Edit: `evals/freight/runner.py`
- Edit: `tests/test_freight_eval_runner.py`
- Edit: `tests/test_freight_eval_models.py`
- Verify unchanged: `evals/freight/models.py`
- Verify unchanged: `evals/freight/reporting.py`
- Verify unchanged: `evals/datasets/freight.v1.yaml`
- Verify unchanged: `.github/workflows/ci.yml`
- Verify unchanged: `evals/fixtures/` contents (the Task-0 `.gitattributes`
  rule is repository metadata, not a fixture change)

- [ ] **Step 1: Delegate eval trace/redaction regressions through `unit-test-writer`.**

Add tests that inject a fixed UUID plus recording observer and assert one trace
across document normalization, loop, policy, tool, and approval events. Add a
malformed-document case and provider-guard failure case proving events contain
only closed codes. Assert event observation does not change the existing
`FreightEvalObservation` or expected outcome.

Add static assertions that report model top-level/case keys are unchanged and
that `trace_id`, `events`, `logs`, source, rationale, and secrets never appear
in JSON or GitHub Markdown.

- [ ] **Step 2: Run eval unit tests and confirm observer injection is absent.**

Run:

```powershell
pytest tests/test_freight_eval_runner.py tests/test_freight_eval_models.py -q
```

Expected: only new recording-observer assertions fail.

- [ ] **Step 3: Thread observer/context through eval-only construction.**

Extend `run_freight_eval_case` with optional keyword-only observer/context.
Default to a deterministic UUID derived from case ID, as current tenant IDs
are. Pass them to `DocumentNormalizer`, `AgentLoop`, and
`PolicyGatedToolExecutor`. Do not include either in observation, comparison,
safe failure message, recorder, or report.

- [ ] **Step 4: Run all freight tests and generate a safe report.**

Run:

```powershell
$report = Join-Path ([System.IO.Path]::GetTempPath()) 'integra-freight-observability-evals.json'
pytest tests/test_freight_eval_runner.py tests/test_freight_eval_models.py -q
pytest evals/test_freight.py -q --freight-eval-report $report
python -m evals.freight.reporting --report $report
```

Expected: focused tests pass; exactly 50 eval cases pass; report is 50/50 with
pass rate 1.0 and contains only current safe keys/counts/IDs/mismatch codes.

- [ ] **Step 5: Check CI, fixture contents, and dataset remained unchanged.**

Run:

```powershell
git diff -- .github/workflows/ci.yml evals/datasets evals/fixtures evals/freight/models.py evals/freight/reporting.py
git diff -- .gitattributes
```

Expected: no changes in the CI workflow, dataset, fixture contents, eval model,
or report projection. The second command shows only the exact `*.pdf binary`
rule from Task 0.

---

## Task 10: Update product and agent documentation after verified behavior exists

**Files:**
- Edit: `README.md`
- Edit: `AGENTS.md`
- Edit: `tests/test_observability.py`

- [ ] **Step 1: Delegate README contract assertions through `unit-test-writer`.**

Require README to mention server-generated trace correlation, JSON logs,
request/job/worker/provider/policy/tool/approval measurements, safe metadata,
and absence of an external backend, dashboard, alerting, or content-bearing
transcript persistence. Continue rejecting phase, roadmap, interview, vacancy,
learning, tutorial, and training language.

- [ ] **Step 2: Run documentation tests and confirm current boundary text fails.**

Run:

```powershell
pytest tests/test_observability.py tests/test_freight_eval_models.py -q
```

Expected: only new README observability assertions fail because README still
says production observability and traces are absent.

- [ ] **Step 3: Update README with stable product-facing behavior.**

Add a concise `Observability` section documenting:

```text
- X-Trace-ID is server generated; X-Correlation-ID is optional and untrusted.
- API and worker emit structured JSON with IDs, closed outcomes, durations,
  stop reasons, policy/tool/approval results, and optional provider usage.
- Logs never contain request/document content, credentials, authorization or
  signature headers, proposals, tool payloads, exception text, or transcripts.
- The first scope has no external backend, dashboard, alerting, trace table,
  or content-bearing transcript persistence.
```

Remove only the blanket claim that observability/traces are absent. Preserve
all other current boundaries and API commands.

- [ ] **Step 4: Apply the `update-md` skill to synchronize AGENTS.md.**

Update overview, architecture, data flow, important files, configuration,
testing, privacy pitfalls, and typical extension tasks. Describe only verified
behavior. Preserve tenant derivation, sync/async ownership, policy precedence,
eval artifact safety, no Markdown parsing, no LangChain, and all excluded
integrations. Do not mention phase numbers or educational context.

- [ ] **Step 5: Re-read both documents and run documentation tests.**

Run:

```powershell
pytest tests/test_observability.py tests/test_freight_eval_models.py -q
rg -n -i "phase 12|roadmap phase|interview|vacancy|learning project|tutorial|training context" README.md AGENTS.md
```

Expected: tests pass and `rg` has no matches. Because `docs/` and `AGENTS.md`
are ignored, inspect them directly rather than claiming they appear in Git
diff.

---

## Task 11: Run full quality, privacy, and compatibility gates

**Files:**
- Verify: every file changed by Tasks 0-10

- [ ] **Step 1: Run the full configured suite with a workspace-local temp directory.**

Use a fresh path after confirming it does not exist:

```powershell
$baseTemp = '.pytest-tmp-phase12-final'
if (Test-Path -LiteralPath $baseTemp) { throw "Temporary test path already exists: $baseTemp" }
pytest -q --basetemp $baseTemp
```

Expected: all non-PostgreSQL tests pass. Report exact pass/skip/warning counts;
do not describe skipped PostgreSQL tests as integration evidence. Remove only
the verified workspace-local temp directory after the run.

- [ ] **Step 2: Run the deterministic freight gate with a new report.**

```powershell
$report = Join-Path ([System.IO.Path]::GetTempPath()) 'integra-freight-observability-final.json'
pytest evals/test_freight.py -q --freight-eval-report $report
python -m evals.freight.reporting --report $report
```

Expected: 50 passed, pass rate 100%, exact category counts, no source/event
payload in output.

- [ ] **Step 3: Run focused privacy scans.**

```powershell
rg -n "LLM_API_KEY|Authorization|X-API-Key|X-Inbound-Signature|content_base64|document_text|source_excerpt|rationale_short|tool_result|pending_action|decision_reason" app/observability
rg -n "trace_id|events|logs|transcript|source_excerpt|rationale_short|content_base64" evals/freight/reporting.py .github/workflows/ci.yml
```

Expected: the first command finds only explicit enum/test documentation or no
matches—never event fields/serialization. The second finds no new artifact or
report serialization path. Inspect every match rather than accepting a raw
match count.

- [ ] **Step 4: Verify migration shape and optional live PostgreSQL evidence.**

Run:

```powershell
alembic heads
pytest tests/test_migrations.py -q
```

Expected: sole head `007_phase12_job_trace_id`; migration tests pass. If an
explicit reachable `DATABASE_URL` is available, additionally run:

```powershell
alembic upgrade head
pytest tests/test_postgres_integration.py tests/test_postgres_approvals.py tests/test_postgres_tool_port.py -q
```

Otherwise report that live PostgreSQL verification was not performed.

- [ ] **Step 5: Check whitespace and exact scope.**

```powershell
git diff --check
git status --short
git diff --stat
git diff -- app alembic tests evals README.md .github pyproject.toml examples .gitattributes
```

Expected: no whitespace error; no change to `pyproject.toml`, `.github`, tenant
examples, eval dataset/fixture contents/report model, or applied migrations.
The only repository-hygiene change is `.gitattributes` containing `*.pdf
binary`. Existing Phase-11 workspace changes remain distinguishable from
Phase-12 work. Stop and ask the user if scope includes a new dependency,
SaaS/exporter, trace table, raw transcript, API body change, policy change,
integration, or production deployment.

---

## Task 12: Review the complete implementation

**Files:**
- Review: complete final diff and ignored documentation files

- [ ] **Step 1: Call the `code-review` skill on the complete changes.**

No project-local review subagent exists. Call the `code-review` skill with the
full diff, direct content of ignored `AGENTS.md`, and this review context:

```text
Review freight intake observability for: server-owned trace propagation across
HTTP/webhook -> job -> worker -> synchronous AgentLoop -> provider -> policy ->
tool/approval -> durable result; duplicate reuse of the original trace; client
correlation remaining untrusted; typed allowlisted JSON events; no source,
document text/bytes/base64, credentials, headers, proposal/rationale, tool
payload, approval reason, SQL, exception text, or transcript in logs; only one
new AgentJob trace column and append-only migration; no trace-event table,
external backend, exporter, dashboard, alert, or new dependency; deterministic
fake-clock/recording-observer/captured-log tests; unchanged API bodies/statuses,
eval report/artifact/CI workflow; PDF fixture contents unchanged and classified
as Git binary through the sole `*.pdf binary` `.gitattributes` rule;
preservation of MAX_STEPS=8, third-identical-tool
guard, explicit None results, strict structured outputs, one retry, policy
precedence, tenant isolation, sync loop, async session ownership, no Markdown
parsing, and no LangChain.
```

Do not apply review corrections automatically. Report findings to the user and
discuss every proposed correction before editing.

---

## Plan self-review

| Approved specification requirement | Plan task(s) |
| --- | --- |
| Server-generated root trace and separate request/client correlation | 2-4 |
| Cross-process job/worker/loop/policy/tool/approval propagation | 2, 5-8 |
| Structured JSON logs with positive field allowlist | 1, 3 |
| No secrets, source, document text/bytes, headers, transcript, or exception text | 1, 3-11 |
| Existing audit records plus logs; no trace-event table | all tasks; checked in 11-12 |
| Request, queue, worker, provider, tool, approval, and document measurements | 3-8 |
| Provider calls, validation retry/failure, optional tokens, no guessed cost | 5 |
| No OpenTelemetry/Langfuse/SaaS/metrics endpoint | file map, 1, 11-12 |
| Preserve sync loop, async sessions, MAX_STEPS, repeated tool, None | 5-8, 11-12 |
| Preserve public API bodies/status/auth and tenant isolation | 3-4, 7, 11-12 |
| Deterministic fakes, clocks, captured logs, and eval seams | 1-9 |
| CI whitespace gate accepts binary PDFs while source whitespace enforcement remains active | 0, 9, 11-12 |
| Eval/CI artifacts remain source-free; workflow and fixture contents remain unchanged | 0, 9, 11-12 |
| Product-facing README and accurate AGENTS without phase/training language | 10 |
| No excluded integrations or production deployment | all tasks; checked in 11-12 |

Names and signatures are consistent across tasks: `ObservationContext`,
`ObservationEvent`, `Observer`, `safe_emit`, `trace_id` on `AgentJob` and
`ClaimedJob`, observer/context keyword injection, and the existing result and
policy enums. The plan contains no unresolved placeholders. Each functional
task starts with a delegated failing test, proves the failure, adds the minimal
implementation, and reruns focused verification before the next seam.
