# Implementation plan: Phase 8 human approval workflow

> **For agents:** Use the `executing-plans` skill to implement this plan task by task. Steps use checkbox syntax for tracking.

**Purpose:** Add a tenant-scoped, audited approval workflow with operator API keys, policy-held actions, idempotent local PostgreSQL execution, and explicit timeout-to-reject handling.

**Architecture:** A Phase-7 worker terminates an agent job as `awaiting_approval` after a deterministic policy result and stores a closed pending command. An operator-only API endpoint locks that approval and commits its decision, the eligible database action, and audit records in one transaction. The existing worker explicitly expires approvals; no in-process background scheduler or external delivery is added.

**Key files/stack:** Python 3.12, FastAPI, Pydantic v2, async SQLAlchemy/PostgreSQL, Alembic, pytest/pytest-asyncio, existing synchronous `AgentLoop` bridged through `WorkerAsyncGateway`.

---

## File map

| Path | Change | Responsibility |
| --- | --- | --- |
| `app/agent/models.py` | Edit | Add closed `critical` priority value. |
| `app/tenants/routing.py` | Edit | Keep precedence and add critical at the existing approval stage. |
| `app/policy/models.py`, `app/policy/engine.py` | Edit | Carry proposal priority into pure policy routing. |
| `app/db/models.py` | Edit | Extend API-key, job, approval, and approval-audit ORM mappings. |
| `alembic/versions/004_phase8_approval_workflow.py` | Create | Append-only PostgreSQL migration and downgrade. |
| `app/auth/api_keys.py` | Edit | Add immutable authenticated-operator dependency without changing tenant dependency. |
| `app/domain/approvals.py` | Create | Strict decision/request models and transactional approval service. |
| `app/domain/approval_repository.py` | Create | Tenant-scoped locks, audit writes, timeout sweep, and row transitions. |
| `app/domain/job_repository.py` | Edit | Persist `awaiting_approval` safely and retain terminal-result guards. |
| `app/tools/models.py`, `app/tools/ports.py` | Edit | Define a closed deferred command and request-approval port shape. |
| `app/tools/executor.py`, `app/tools/in_memory.py` | Edit | Freeze a validated policy-held action and keep the test double deterministic. |
| `app/tools/postgres.py` | Edit | Persist a pending approval through the gateway and expose reusable session-local action primitives. |
| `app/api/approvals.py`, `app/main.py` | Create/Edit | Operator-only decision route and router registration. |
| `app/workers/agent_worker.py` | Edit | Explicitly sweep expiry and preserve `awaiting_approval` terminal state. |
| `app/core/config.py`, `.env.example` | Edit | Add a positive `approval_timeout_seconds` setting with a 24-hour default. |
| `scripts/seed_demo.py` | Edit | Provide a one-time operator-key provisioning path with an actor reference. |
| `tests/test_policy.py`, `tests/test_tools.py`, `tests/test_agent_worker.py`, `tests/test_migrations.py` | Edit | Preserve regressions and add focused unit/migration coverage. |
| `tests/test_approvals.py`, `tests/test_approvals_api.py`, `tests/test_postgres_approvals.py` | Create | Service, HTTP/auth, concurrency, expiry, and PostgreSQL isolation evidence. |

## Task 1: Establish closed policy behavior for `critical`

**Files:**
- Edit: `app/agent/models.py`
- Edit: `app/tenants/routing.py`
- Edit: `app/policy/models.py`
- Edit: `app/policy/engine.py`
- Test: `tests/test_policy.py`

- [ ] **Step 1: Delegate failing policy tests through `unit-test-writer`.**

Request tests that construct a normal registered `create_case` action and assert all of the following exact results:

```python
assert critical.status is RoutingStatus.PENDING_APPROVAL
assert critical.decision is RoutingDecision.NEEDS_APPROVAL
assert critical.reason == "approval_required"

assert incomplete_critical.status is RoutingStatus.AWAITING_INPUT
assert incomplete_critical.decision is RoutingDecision.DENY
assert risk_critical.status is RoutingStatus.URGENT
assert risk_critical.reason == "safety_or_legal_risk"
```

Also retain the existing test that an unregistered `send_reply` is denied as `action_not_configured`.

- [ ] **Step 2: Run the focused tests and confirm the intended initial failure.**

Run: `pytest tests/test_policy.py -q`

Expected: the new `critical` case fails because `ProposalPriority` does not accept `critical` or because routing still returns `allow`.

- [ ] **Step 3: Add the enum and routing input without changing prior precedence.**

Add this enum member in `app/agent/models.py`:

```python
class ProposalPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"
    CRITICAL = "critical"
```

Add a boolean to `RoutingAssessment`, populate it only from
`value.proposal.priority is ProposalPriority.CRITICAL`, and perform the check
only beside the current YAML approval condition:

```python
if (
    assessment.requested_action in self.config.routing.always_approval_actions
    or rule.requires_approval
    or assessment.is_critical
):
    return RoutingOutcome(
        status=RoutingStatus.PENDING_APPROVAL,
        decision=RoutingDecision.NEEDS_APPROVAL,
        missing_required_fields=(),
        reason="approval_required",
    )
```

Do not move the trusted-risk, intake, missing-field, registered-action, or
disallowed-action checks. Do not reinterpret `urgent` as `critical`.

- [ ] **Step 4: Run focused policy and structured-output regressions.**

Run: `pytest tests/test_policy.py tests/test_openai_compatible_provider.py -q`

Expected: all tests pass; the provider schema accepts the additional enum value and old values remain valid.

## Task 2: Add the durable schema and migration

**Files:**
- Edit: `app/db/models.py`
- Create: `alembic/versions/004_phase8_approval_workflow.py`
- Test: `tests/test_migrations.py`

- [ ] **Step 1: Delegate migration-model tests through `unit-test-writer`.**

Require tests that inspect SQLAlchemy metadata and PostgreSQL information
schema for these invariants:

```python
assert approvals.c.case_id.nullable is True
assert "job_id" in approvals.c
assert "pending_action" in approvals.c
assert "expires_at" in approvals.c
assert "approval_events" in Base.metadata.tables
assert "principal_type" in Base.metadata.tables["api_keys"].c
assert "actor_ref" in Base.metadata.tables["api_keys"].c
```

The PostgreSQL test must reject an approval whose `(tenant_id, job_id)` or
`(tenant_id, case_id)` refers to another tenant, and reject a duplicate
approval for one job.

- [ ] **Step 2: Run the migration tests and confirm their initial failure.**

Run: `pytest tests/test_migrations.py -q`

Expected: failure because the new ORM columns/table and revision do not yet exist. PostgreSQL-specific cases may skip only when `DATABASE_URL` is absent.

- [ ] **Step 3: Extend ORM mappings with closed lifecycle fields.**

Implement these database-owned fields, using `String` plus `CheckConstraint`
where appropriate rather than a PostgreSQL enum. The Phase-8 fields are
nullable at the database level only to preserve pre-Phase-8 legacy approval
rows; the Phase-8 repository must reject any new row missing them:

```python
# ApiKey
principal_type: Mapped[str] = mapped_column(String(30), nullable=False, server_default="service")
capability: Mapped[str] = mapped_column(String(50), nullable=False, server_default="none")
actor_ref: Mapped[str | None] = mapped_column(String(255))

# AgentJob: permit the additional stored state value "awaiting_approval".

# Approval
job_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
case_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True))
policy_reason: Mapped[str | None] = mapped_column(String(100))
pending_action: Mapped[dict | None] = mapped_column(JSONB)
tenant_config_sha256: Mapped[str | None] = mapped_column(String(64))
expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
decided_by_actor_ref: Mapped[str | None] = mapped_column(String(255))
decision_reason: Mapped[str | None] = mapped_column(Text)
executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
execution_result: Mapped[dict | None] = mapped_column(JSONB)
```

Add composite foreign keys from approvals to `(tenant_id, id)` of jobs and
cases, plus `UniqueConstraint("tenant_id", "id",
name="uq_approvals_tenant_id_id")` so approval audit rows can use a composite
foreign key. Add `UniqueConstraint("job_id", name="uq_approvals_job_id")` for
non-null Phase-8 jobs and a tenant-first index covering pending expiry. Create `ApprovalEvent` with its
tenant/composite approval foreign key, `event_type`, nullable `actor_ref`,
JSON payload, and timestamp.

- [ ] **Step 4: Write revision `004_phase8_approval_workflow.py`.**

The `upgrade()` must: add API-key fields with server defaults; backfill/retain
existing keys as `service`/`none`; make `approvals.case_id` nullable; add the
new approval columns nullable so existing rows remain readable; add a check
that a new Phase-8 row is complete when it enters `pending`/execution states;
add the composite job foreign key and unique index; create
`approval_events`; and add lifecycle/operator check constraints. The
completeness constraint must be `job_id IS NULL OR (job_id IS NOT NULL AND
pending_action IS NOT NULL AND tenant_config_sha256 IS NOT NULL AND
expires_at IS NOT NULL)`, so historical pending rows remain readable without
becoming executable. Do not invent a job snapshot or copy untrusted data to
backfill a legacy row. The decision service must reject legacy rows with a
stable conflict instead of approving or executing them.
The `downgrade()` must drop dependent indexes/foreign keys/table/columns in
reverse order. Do not edit `001`–`003`.

- [ ] **Step 5: Apply and verify the migration.**

Run: `alembic upgrade head`

Expected: Alembic reports revision `004_phase8_approval_workflow` at `head`.

Run: `pytest tests/test_migrations.py -q`

Expected: all available migration checks pass.

## Task 3: Add explicit operator authentication and provisioning

**Files:**
- Edit: `app/auth/api_keys.py`
- Edit: `app/auth/__init__.py`
- Edit: `scripts/seed_demo.py`
- Test: `tests/test_auth.py`
- Test: `tests/test_approvals_api.py`

- [ ] **Step 1: Delegate authentication tests through `unit-test-writer`.**

Cover an active operator key, an active service key, inactive operator key,
malformed key, and a cross-tenant approval ID. Assert that only an operator
credential with `capability == "approval_decider"` yields:

```python
AuthenticatedOperator(
    tenant_id=tenant.id,
    actor_ref="ops-alice",
    credential_id=key.id,
)
```

The old `get_current_tenant` tests must continue to pass unchanged.

- [ ] **Step 2: Run the focused tests and confirm they initially fail.**

Run: `pytest tests/test_auth.py tests/test_approvals_api.py -q`

Expected: new operator dependency/API tests fail because no operator model or route exists.

- [ ] **Step 3: Add a narrow operator dependency.**

Keep `get_current_tenant` byte-for-byte compatible in behavior. Add:

```python
@dataclass(frozen=True, slots=True)
class AuthenticatedOperator:
    tenant_id: UUID
    actor_ref: str
    credential_id: UUID

async def get_current_operator(... ) -> AuthenticatedOperator:
    # validate existing ik_ format
    # join ApiKey and Tenant; require active/operator/approval_decider/nonblank actor_ref
    # return 401 "Invalid operator API key" for every failure
```

Never return a raw API key or hash. Do not add a user/password/JWT model.

- [ ] **Step 4: Add explicit operator key provisioning.**

Extend `scripts/seed_demo.py` with an opt-in flag such as
`--operator-ref <nonblank-value>`. In that mode create an active `ApiKey` with
`principal_type="operator"`, `capability="approval_decider"`, and the actor
reference; print the raw key exactly once using the existing provisioning
discipline. The default demo tenant/intake-key behavior remains unchanged.

- [ ] **Step 5: Run focused auth tests.**

Run: `pytest tests/test_auth.py tests/test_seed.py tests/test_approvals_api.py -q`

Expected: all tests pass, including original API-key secrecy and rotation cases.

## Task 4: Define closed pending commands and persist an approval from the worker

**Files:**
- Create: `app/domain/approval_repository.py`
- Create: `app/domain/approvals.py`
- Edit: `app/domain/job_repository.py`
- Edit: `app/tools/models.py`
- Edit: `app/tools/ports.py`
- Edit: `app/tools/in_memory.py`
- Edit: `app/tools/postgres.py`
- Edit: `app/tools/executor.py`
- Test: `tests/test_tools.py`
- Test: `tests/test_postgres_approvals.py`

- [ ] **Step 1: Delegate policy-held-action tests through `unit-test-writer`.**

Add a spy approval port and assert that a valid registered `create_case` with
risk/YAML/critical approval:

```python
assert result.data == {
    "decision": "needs_approval",
    "status": "pending_approval",
    "reason": "approval_required",
    "approval_id": str(approval_id),
}
assert result.continue_run is False
assert create_case_handler_calls == []
```

Also assert invalid tool arguments create no approval, model fields outside the
tenant catalog are omitted from the pending command, and `send_reply` remains
the existing unregistered denial.

- [ ] **Step 2: Run focused executor tests and confirm initial failure.**

Run: `pytest tests/test_tools.py -q`

Expected: policy-held-action tests fail because `TenantToolPort` has no approval method.

- [ ] **Step 3: Add immutable action/response models.**

In `app/tools/models.py`, define strict frozen Pydantic models rather than
persisting `AgentProposal` or `ToolCall` directly:

```python
class PendingAction(StrictToolModel):
    version: Literal[1] = 1
    name: Literal["find_customer", "create_case", "update_case_fields", "create_reply_draft", "flag_for_review"]
    arguments: dict[str, ToolData]
    known_fields: dict[str, ToolData]

class ApprovalRequested(StrictToolModel):
    id: UUID
    expires_at: datetime
```

Build only JSON-compatible deep copies. Do not include rationale, confidence,
source body/subject, raw proposal, credentials, or exception text.

- [ ] **Step 4: Extend the synchronous port at its existing boundary.**

Add this protocol method and implement it in the in-memory and sync/Postgres
adapters:

```python
def request_approval(
    self,
    tenant_id: UUID,
    *,
    action: PendingAction,
    policy_reason: str,
) -> ApprovalRequested: ...
```

The in-memory adapter records requests for executor unit tests but never
executes the original action. `SyncTenantToolPort` routes to the async
PostgreSQL method through `WorkerAsyncGateway`; it does not call
`asyncio.run()`.

- [ ] **Step 5: Implement one short Postgres approval-creation transaction.**

`ApprovalRepository.create_for_running_job(...)` must lock `AgentJob` by job
ID, tenant ID, `status="running"`, and claimed attempt count; reject any other
state. It then inserts one approval/event, updates the job as follows, and
commits:

```python
job.status = "awaiting_approval"
job.finished_at = now
job.lease_expires_at = None
job.result = {
    "status": "awaiting_approval",
    "approval_id": str(approval.id),
    "policy_reason": policy_reason,
}
job.error_code = None
```

Set `expires_at = now + timedelta(seconds=settings.approval_timeout_seconds)`.
Do not set `side_effect_committed_at`: creating an approval is not the pending
business action. If the unique job constraint reports a concurrent duplicate,
lock and return the existing tenant-owned approval instead of adding another
event.

- [ ] **Step 6: Change executor flow only after typed argument validation.**

Refactor `PolicyGatedToolExecutor.execute()` to preserve deny behavior, then
validate the definition's argument model before treating a `NEEDS_APPROVAL`
result as persistable. Its relevant branch is:

```python
if policy.decision is RoutingDecision.NEEDS_APPROVAL:
    requested = self._port.request_approval(
        self._runtime.tenant_id,
        action=self._pending_action(definition, arguments, proposal),
        policy_reason=policy.reason,
    )
    data = policy.as_tool_data() | {"approval_id": str(requested.id)}
    return ToolExecutionResult(
        data=data,
        continue_run=False,
        final_response="approval_requested",
    )
```

`_pending_action()` must use the same trusted-source and known-field filtering
as the normal handlers. It must never execute `definition.handler` in this
branch.

- [ ] **Step 7: Verify unit and PostgreSQL creation behavior.**

Run: `pytest tests/test_tools.py tests/test_postgres_approvals.py -q`

Expected: exactly one pending approval exists for one job; handler calls stay at zero before a decision; tenant/privacy payload assertions pass.

## Task 5: Implement idempotent decision and local action execution

**Files:**
- Edit: `app/domain/approvals.py`
- Edit: `app/domain/approval_repository.py`
- Edit: `app/tools/postgres.py`
- Test: `tests/test_approvals.py`
- Test: `tests/test_postgres_approvals.py`

- [ ] **Step 1: Delegate decision-service tests through `unit-test-writer`.**

Require tests for: reject does not execute; first approve executes a deferred
`create_case` exactly once; a same-decision retry returns the stored result;
an opposite decision raises `ApprovalDecisionConflict`; and a forced failure
inside the transaction leaves the approval `pending`, creates no case, and
leaves no execution event.

The concurrency test must await two approvals against the same database row
and assert:

```python
assert approved_count == 1
assert executed_event_count == 1
assert case_count == 1
assert {first.status, second.status} == {"approved"}
```

- [ ] **Step 2: Run focused service tests and confirm initial failure.**

Run: `pytest tests/test_approvals.py tests/test_postgres_approvals.py -q`

Expected: failure because no decision service or action executor exists.

- [ ] **Step 3: Add strict decision input/output and stable domain exceptions.**

Use a closed request contract:

```python
class ApprovalDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def require_non_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reason must not be blank")
        return value
```

Define `ApprovalNotFound`, `ApprovalDecisionConflict`, and
`ApprovalExpired` without leaking database text. The result object contains
only ID, final status, action name, decision time, optional case ID, and
`executed`.

- [ ] **Step 4: Implement the locked decision state machine.**

In one `async with session.begin()` transaction, look up by `(approval_id,
operator.tenant_id)` with `FOR UPDATE`. Apply these rules in this order:

```python
if approval.status in {"approved", "rejected", "expired"}:
    if approval.status == requested_terminal_status:
        return stored_result  # no event, no mutation, no action
    raise ApprovalDecisionConflict
if approval.expires_at <= now:
    expire_and_append_event(approval, now)
    if request.decision == "reject":
        return stored_result
    raise ApprovalExpired
if request.decision == "reject":
    reject_once(approval, operator, request.reason, now)
    return stored_result
approve_and_execute_once(approval, operator, request.reason, now)
```

`expire_and_append_event()` writes status `expired`, decision metadata with
`actor_ref="system_timeout"`, and `approval_expired`; it never invokes an
action.

- [ ] **Step 5: Reuse session-local deterministic action primitives.**

Refactor the current case creation/update internals in `app/tools/postgres.py`
into methods that accept an existing `AsyncSession` and a trusted job/approval
context. The approval path validates the `PendingAction` discriminant and
uses only its frozen typed arguments plus the linked job's source/profile
snapshot. It executes supported database actions without creating a nested
independent session/transaction.

For a successful approve, write all of the following before the outer commit:

```python
approval.status = "approved"
approval.decided_at = now
approval.decided_by_actor_ref = operator.actor_ref
approval.decision_reason = request.reason
approval.executed_at = now
approval.execution_result = safe_result
# set approval.case_id for create_case when newly created
append_approval_event("approval_approved", operator.actor_ref, {})
append_approval_event("approved_action_executed", operator.actor_ref, safe_metadata)
```

The action's existing case event is part of this transaction. Never make a
provider call, an email call, TMS/Odoo call, or `asyncio.run()` call here.

- [ ] **Step 6: Verify exact-once, rollback, and isolation.**

Run: `pytest tests/test_approvals.py tests/test_postgres_approvals.py -q`

Expected: repeated same decision is a no-op; a different final decision is a
conflict; failure before commit leaves no partial state; same-tenant and
cross-tenant results match the service contract.

## Task 6: Expose the operator-only HTTP endpoint

**Files:**
- Create: `app/api/approvals.py`
- Edit: `app/main.py`
- Test: `tests/test_approvals_api.py`

- [ ] **Step 1: Delegate API contract tests through `unit-test-writer`.**

Test `POST /v1/approvals/{id}/decide` with an operator key for successful
approve/reject, repeat same decision, opposite final decision, expired
approval, service-key rejection, malformed request, missing ID, and an
approval belonging to tenant B. Require:

```python
assert cross_tenant.status_code == 404
assert service_key.status_code == 401
assert malformed.status_code == 422
assert repeated_approve.status_code == 200
assert opposite.status_code == 409
```

Also run the existing case/intake tests unchanged to prove no response contract
was altered.

- [ ] **Step 2: Run focused HTTP tests and confirm initial failure.**

Run: `pytest tests/test_approvals_api.py -q`

Expected: route-not-found failure.

- [ ] **Step 3: Implement a narrow router.**

Use `APIRouter(prefix="/approvals", tags=["approvals"])`,
`get_current_operator`, and a fresh `AsyncSession` dependency. Translate only
stable domain errors:

```python
@router.post("/{approval_id}/decide", response_model=ApprovalDecisionResponse)
async def decide_approval(
    approval_id: UUID,
    request: ApprovalDecisionRequest,
    operator: AuthenticatedOperator = Depends(get_current_operator),
    session: AsyncSession = Depends(get_db_session, use_cache=False),
) -> ApprovalDecisionResponse:
    ...
```

Map `ApprovalNotFound` to `404`, `ApprovalDecisionConflict` and
`ApprovalExpired` to `409`, and rely on FastAPI/Pydantic for `422`. Do not add
listing, HTML, background tasks, or an endpoint that exposes source bodies or
approval payloads.

- [ ] **Step 4: Register and verify the route without changing old contracts.**

Add exactly one router registration:

```python
from app.api.approvals import router as approvals_router
app.include_router(approvals_router, prefix="/v1")
```

Run: `pytest tests/test_approvals_api.py tests/test_cases_api.py tests/test_intake.py -q`

Expected: new endpoint tests pass and all old API contracts stay green.

## Task 7: Add explicit expiry processing and worker terminal handling

**Files:**
- Edit: `app/core/config.py`
- Edit: `.env.example`
- Edit: `app/domain/approval_repository.py`
- Edit: `app/workers/agent_worker.py`
- Test: `tests/test_agent_worker.py`
- Test: `tests/test_approvals.py`

- [ ] **Step 1: Delegate expiry/worker tests through `unit-test-writer`.**

Cover a due pending approval, a not-yet-due row, and an already final row.
Assert each due row becomes `expired` with exactly one `approval_expired`
event and no executed action. Add a worker test proving a job transitioned by
the approval port is not later marked succeeded/failed by `_persist_result`.

- [ ] **Step 2: Run the focused tests and confirm initial failure.**

Run: `pytest tests/test_agent_worker.py tests/test_approvals.py -q`

Expected: expiry and `awaiting_approval` assertions fail before the repository/worker changes.

- [ ] **Step 3: Add configuration and bounded repository sweep.**

Add:

```python
approval_timeout_seconds: int = Field(default=86_400, ge=1)
```

to `Settings` and document `APPROVAL_TIMEOUT_SECONDS=86400` in `.env.example`.
Implement `expire_due(now, limit=100) -> int` as one short transaction using:

```python
select(Approval).where(
    Approval.status == "pending",
    Approval.expires_at <= now,
).order_by(Approval.expires_at, Approval.id).limit(limit).with_for_update(skip_locked=True)
```

For every locked row, set terminal expiry metadata and append one event. Never
execute a pending action in this method.

- [ ] **Step 4: Call the sweep only from the explicit worker loop.**

In `AgentWorker.serve_once()`, open the same short pre-claim session already
used for lease recovery, call `ApprovalRepository(session).expire_due(now)`,
then recover leases and claim. Do not create `BackgroundTasks`,
`asyncio.create_task`, another event loop, or a thread.

Ensure every `JobRepository` terminal mutation keeps its current
`status == "running"` guard, so a job already set to `awaiting_approval` is a
durable no-op at worker finalization.

- [ ] **Step 5: Verify expiry semantics.**

Run: `pytest tests/test_agent_worker.py tests/test_approvals.py tests/test_postgres_approvals.py -q`

Expected: expiry is explicit and safe; no test observes execution after timeout; all prior worker retry/lease tests remain green.

## Task 8: Run the Phase-8 integration suite and inspect scope boundaries

**Files:**
- Edit if needed from preceding test failures only: files named in Tasks 1–7
- Test: all Phase-8 and existing regression suites

- [ ] **Step 1: Run all focused Phase-8 suites.**

Run:

```powershell
pytest tests/test_policy.py tests/test_tools.py tests/test_auth.py tests/test_approvals.py tests/test_approvals_api.py tests/test_postgres_approvals.py tests/test_agent_worker.py tests/test_migrations.py -q
```

Expected: all non-integration tests pass; PostgreSQL-only tests either pass
against configured `DATABASE_URL` or report their existing explicit skip.

- [ ] **Step 2: Inspect the diff against the approved scope.**

Run: `git diff -- app alembic scripts tests .env.example`

Expected: changes are limited to operator keys, approval persistence/decision,
the explicit worker expiry sweep, tests, and the schema revision. There are no
email/TMS/Odoo clients, UI templates, MCP/document/eval/observability work,
LangChain imports, Markdown parsing, or hidden `asyncio.run()` calls.

- [ ] **Step 3: Run the project suite.**

Run: `pytest -q`

Expected: all available tests pass; any PostgreSQL skips are reported as skips,
not treated as database verification.

## Plan self-review

Spec coverage is mapped as follows: operator auth (Task 3), policy triggers
and unchanged precedence (Task 1), approval/job/case/action/trusted-context
linkage and audit (Tasks 2 and 4), decision idempotency/crash boundary/local
execution (Task 5), API isolation/unchanged contracts (Task 6), explicit
timeout (Task 7), and full regression/scope checks (Task 8). The plan adds no
external delivery, UI, MCP, documents, evals, observability, or case-status
workflow. All terms used after Task 2 (`awaiting_approval`, `PendingAction`,
`AuthenticatedOperator`, lifecycle statuses) are defined before their use.

## Task 9: Run final quality checks

**Files:**
- Verify: all files changed by Tasks 1–7

- [ ] **Step 1: Run the full project test suite from a clean command invocation.**

Run: `pytest -q`

Expected: all unit/API tests pass. PostgreSQL integration tests pass when
`DATABASE_URL` is configured; otherwise only their existing explicit skip is
acceptable. Do not treat an integration skip as PostgreSQL evidence.

- [ ] **Step 2: Check the final patch for whitespace errors.**

Run: `git diff --check`

Expected: command exits `0` with no output.

- [ ] **Step 3: Inspect the changed-file scope before handoff.**

Run: `git status --short; git diff --stat; git diff -- app alembic scripts tests .env.example`

Expected: the patch contains only the approved Phase-8 approval, operator-key,
worker-expiry, migration, provisioning, and test changes. Stop and ask the
user before adding or retaining any email/TMS/Odoo/UI/MCP/document/eval/
observability change.

## Task 10: Request an independent code review

**Files:**
- Review: final diff from Tasks 1–9

- [ ] **Step 1: Call the `code-review` skill on the changes made by this plan.**

Provide the reviewer the final diff and this plan's safety constraints:
tenant predicates on every approval/job/case lookup; operator-only approval
authorization; no raw key/source/LLM transcript logging; decision/action/audit
atomicity; no duplicate action on retry; explicit timeout rejection; and no
external delivery or hidden event loop.

- [ ] **Step 2: Handle review findings without automatic corrections.**

Do not make corrections from the review automatically. Summarize every finding
with severity and evidence, then discuss it with the user before changing code.

- [ ] **Step 3: Record the review outcome.**

If there are no findings, report the clean review with its commands/evidence.
If findings exist, report the unresolved items and do not claim the Phase-8
implementation is complete until the user directs their disposition.
