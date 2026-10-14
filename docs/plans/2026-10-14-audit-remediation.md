# Implementation plan: Audit remediation

> **For agents:** Use the executing-plans skill to implement this plan task by task. Steps use checkbox syntax (`- [ ]`) for tracking. Execute each phase as a separate reviewable change set; do not combine schema, policy, and worker-runtime changes in one commit.

**Purpose:** Correct the current-code findings from `docs/specs/IntegraIntake_Audit_EN.md` without weakening tenant isolation, immutable profile snapshots, transactional side-effect fencing, or source-free telemetry.

**Architecture:** Apply hard authorization and value validation before risk routing, give terminal proposals a deterministic business outcome, preserve source provenance, and make approval/job state one atomic lifecycle. Expose only typed tenant-scoped result references. Add bounded worker supervision, cancellation, database budgets, and per-tenant concurrency after correctness changes are complete.

**Key files/stack:** Python 3.12+, FastAPI, Pydantic v2, SQLAlchemy 2 async, PostgreSQL, Alembic, httpx, pytest/pytest-asyncio, Docker Compose.

## Implementation status

The source changes for F01–F10 and F12 are implemented. F03/F04 now include
supervised worker tasks, tenant-aware bounded admission, cooperative
cancellation, gateway/database/parser budgets, and Compose readiness. F09/F10
include durable tenant-scoped artifacts and typed job result references. F11 is
covered by `scripts/verify_release_artifact.py` and release tests. Provider-free
acceptance is complete: `pytest -q` reports 662 passed and 15 environment-gated
PostgreSQL skips; `pytest evals/test_freight.py -q` reports 50 passed; the
release verifier, `docker compose config --quiet`, `compileall`, and
`git diff --check` complete successfully. PostgreSQL lifecycle/concurrency
acceptance and clean-image acceptance still require the deployment environment;
skipped database tests are not treated as evidence.

## Current-state assessment

| Finding | Status in current checkout | Required response |
| --- | --- | --- |
| F01 risk bypasses hard deny | Confirmed. `TenantRouter.route` checks risk before intake, completeness, registry, execution mode, and `allowed`. Existing tests preserve this precedence. | Fix in Phase 1. |
| F02 approval leaves job parked | Confirmed. Approval creation sets `finished_at`; approve, reject, and expiry never transition the job. | Fix in Phase 2. |
| F03 persistence failure exits worker | Confirmed. `serve` does not supervise exceptions from `serve_once`; Compose has no restart policy. | Fix in Phase 4. |
| F04 lease heartbeat is not a deadline | Confirmed. The worker awaits the thread before heartbeat outcome; gateway waits without timeout; worker concurrency is one. | Fix in Phase 4. |
| F05 profile field constraints are not enforced | Confirmed. `_known_proposal_fields` checks names/presence/provenance only. | Fix in Phase 1. |
| F06 terminal proposal bypasses policy | Confirmed. `AgentLoop` returns `COMPLETED/FINAL` before policy and can persist an empty execution path. | Fix in Phase 1. |
| F07 provider catalog is incomplete | Confirmed. Runtime sends only names. | Fix in Phase 1. |
| F08 attachment body is discarded | Confirmed. Document enqueue persists `Document received.` although the hash includes the real body. | Fix in Phase 3. |
| F09 draft/review results are not durable | Confirmed. Both normal and approved handlers return `persisted=False`. | Fix in Phase 3. |
| F10 normal intake does not expose its case | Confirmed. Job projection has no case/result reference. | Fix in Phase 3. |
| F11 supplied archive was incomplete | Not reproducible in this checkout. All named files are present and the distribution tests pass. | No source fix. Validate the release artifact in Phase 5. |
| F12 failed-job read rebuilds executable profile | Confirmed. `project_job_read` resolves the full profile and the API maps failure to 503. | Fix in Phase 3. |

The verified provider-free baseline on 2026-10-14 is `662 passed, 15 skipped`
for `pytest -q` and `50 passed` for `pytest evals/test_freight.py -q`. The 15
skips mean PostgreSQL behavior is not established by this baseline.

## Decisions applied by this plan

- Approval is a single-action terminal workflow. An approved action completes the job; rejection completes it with a rejected business outcome; expiry fails it with `approval_expired`. This plan does not resume the model after approval.
- Awaiting-input correction creates a new intake with a new idempotency key. Reusing the original key with changed content remains a conflict. No hidden continuation or replay is added.
- Body values take precedence over attachment values for the same field. The provider must identify `body` or `document` provenance, and the server verifies the excerpt against that source before choosing the value.
- Drafts and review flags become durable tenant-scoped artifacts. Reporting an operational result without an artifact is removed.
- The existing tenant-slug-to-profile mapping remains unchanged. Shared scenario profiles need a separate approved onboarding design; the audit identifies a scaling constraint, not a current isolation defect.
- Retention duration is not invented here. The implementation must expose business records to the existing access boundary and document that retention remains an owner policy decision.

## File map

- `app/tenants/field_values.py`: new pure profile-specific field validation and source precedence.
- `app/tenants/routing.py`: hard-gate-before-risk routing order and action execution-mode checks.
- `app/policy/engine.py`: validated-present-field and terminal-policy entry points.
- `app/agent/models.py`: source provenance and deterministic terminal outcome contracts.
- `app/agent/loop.py`: evaluate terminal proposals through the executor instead of accepting model stop as business success.
- `app/tools/registry.py`: provider-facing action descriptions and schemas.
- `app/tools/executor.py`: shared validated field set, terminal evaluation, approval guards, and safe result references.
- `app/runtime/factory.py`: bounded provider catalog and preserved body/document source labels.
- `app/domain/approval_repository.py`: atomic job transitions for request, decision, and expiry.
- `app/domain/approvals.py`: immutable profile authorization recheck and single-action job completion.
- `app/tools/postgres.py`: durable approved artifacts and result references.
- `app/db/models.py`: durable artifact model and constraints.
- `alembic/versions/<new_revision>_add_job_artifacts.py`: append-only artifact migration.
- `app/domain/artifacts.py`, `app/domain/artifact_repository.py`, `app/api/artifacts.py`: typed tenant-scoped artifact projection and retrieval.
- `app/domain/jobs.py`, `app/domain/job_repository.py`, `app/api/jobs.py`: minimal failure-safe job projection and typed result references.
- `app/domain/intake.py`: preserve attachment message body in the trusted snapshot.
- `app/workers/agent_worker.py`, `app/runtime/gateway.py`: supervision, concurrency, deadlines, and cooperative cancellation.
- `app/db/session.py`, `app/core/config.py`, `.env.example`, `docker-compose.yml`: runtime budgets and process supervision.
- `app/documents/normalizer.py`: incremental extraction limits and parser deadline integration.
- `README.md`, `docs/runbooks/live-provider-demo.md`: corrected public lifecycle and operating contract.

---

## Phase 1: Authorization, values, terminal outcomes, and provider contract

### Task 1: Enforce profile field types and provenance before completeness

**Files:**
- Create: `app/tenants/field_values.py`
- Edit: `app/agent/models.py`
- Edit: `app/policy/models.py`
- Edit: `app/policy/engine.py`
- Edit: `app/tools/executor.py`
- Test: `tests/test_field_values.py`
- Test: `tests/test_policy.py`
- Test: `tests/test_tools.py`

- [x] **Step 1: Add failing table-driven tests for every `FieldType`**

Cover exact booleans, finite non-boolean numbers, RFC 3339 timezone-aware datetimes, bounded nonblank text/address/phone, valid email, exact single-select membership, unique multi-select membership, blank values, wrong JSON types, out-of-option values, oversized strings, and `NaN`/infinity. Add mixed-source tests proving that an excerpt must occur in the declared source and that a verified body value wins over a verified document value.

Use this public contract in the tests:

```python
class FieldSource(str, Enum):
    BODY = "body"
    DOCUMENT = "document"


@dataclass(frozen=True, slots=True)
class ValidatedProposalFields:
    values: Mapping[str, ToolData]
    invalid_fields: tuple[str, ...]


def validate_proposal_fields(
    proposal: AgentProposal,
    runtime: TrustedToolRuntimeContext,
) -> ValidatedProposalFields: ...
```

Expected: invalid values never appear in `values`; if both sources provide a valid value for one field, `body` is selected; `invalid_fields` contains sorted unique profile field names only.

- [x] **Step 2: Run the new tests and confirm current acceptance of invalid values**

Run:

```powershell
pytest tests/test_field_values.py tests/test_policy.py tests/test_tools.py -q
```

Expected: the new cases fail because profile-specific validation and `FieldSource` do not exist.

- [x] **Step 3: Add explicit source provenance to proposal values**

Add `source: FieldSource | None = None` to `ProposalValue`. Change uniqueness from field name to `(name, source)`. Require `source` and `source_excerpt` together when a document is present; body-only intake may default an omitted source to `body` for compatibility. Keep raw source and excerpts out of execution evidence and durable summaries.

- [x] **Step 4: Implement the pure validator**

Create `app/tenants/field_values.py` with fixed server bounds and no coercion:

```python
MAX_SHORT_TEXT_CHARS = 500
MAX_LONG_TEXT_CHARS = 10_000
MAX_ADDRESS_CHARS = 1_000
MAX_PHONE_CHARS = 50
MAX_EMAIL_CHARS = 320


def validate_proposal_fields(
    proposal: AgentProposal,
    runtime: TrustedToolRuntimeContext,
) -> ValidatedProposalFields:
    """Validate profile values and source excerpts, then apply body precedence."""
```

Use `email-validator` through Pydantic `TypeAdapter(EmailStr)` and add `email-validator>=2.2` to `pyproject.toml`. Parse datetimes with `datetime.fromisoformat(value.replace("Z", "+00:00"))` and require `tzinfo`. Reject normalization that changes business values; return the original accepted JSON value.

- [x] **Step 5: Make all policy/tool paths consume the same validated result**

Replace `_known_proposal_fields`, `_verified_present_fields`, and `_recomputed_missing_required_fields` with the shared validator. Count only `ValidatedProposalFields.values` toward completeness. Before any handler or approval freeze, stop with `invalid_field_values` and list only invalid field names in `missing_required_fields`; never persist invalid values.

- [x] **Step 6: Run focused tests**

Run:

```powershell
pytest tests/test_field_values.py tests/test_policy.py tests/test_tools.py tests/test_documents.py -q
```

Expected: all pass, including body/document provenance and every configured field type.

### Task 2: Put hard authorization gates before risk escalation

**Files:**
- Edit: `app/tenants/routing.py`
- Edit: `app/policy/engine.py`
- Edit: `app/tools/executor.py`
- Edit: `app/domain/approvals.py`
- Test: `tests/test_tenant_config.py`
- Test: `tests/test_policy.py`
- Test: `tests/test_tools.py`
- Test: `tests/test_approvals.py`

- [x] **Step 1: Replace tests that preserve the unsafe precedence**

Add a matrix with risk enabled for unknown intake, missing/invalid fields, absent action, policy-only action, `allowed: false`, unregistered executable action, and a genuinely allowed executable action. The first six must be `DENY` and must create no executable approval. The last must remain `NEEDS_APPROVAL`.

- [x] **Step 2: Run the matrix and confirm the current failures**

Run:

```powershell
pytest tests/test_tenant_config.py tests/test_policy.py tests/test_tools.py -q
```

Expected: failures show that risk currently returns `needs_approval` before the hard gates.

- [x] **Step 3: Reorder `TenantRouter.route`**

Apply this precedence exactly:

```text
unreadable source
-> unknown intake
-> invalid/missing required values
-> unknown action
-> non-executable/policy-only action
-> allowed=false
-> risk or critical priority
-> configured approval
-> allow
```

Extend `RoutingAssessment` with `executable_actions: frozenset[str]`. Return stable reasons `action_not_configured`, `action_not_executable`, and `action_not_allowed` before evaluating risk.

- [x] **Step 4: Recheck approval authorization from the immutable snapshot**

After `resolve_persisted_profile(...)` and before executing a frozen command, require:

```python
compiled = resolved_profile.compiled
if compiled is None or action.name not in compiled.available_actions:
    raise ApprovalDecisionConflict("approval action is no longer authorized")
```

Validate the frozen known fields with the same profile validator contract used at request time. Do not use the current filesystem profile; authorization is against the hash-verified immutable job snapshot.

- [x] **Step 5: Run focused tests**

Run:

```powershell
pytest tests/test_tenant_config.py tests/test_policy.py tests/test_tools.py tests/test_approvals.py -q
```

Expected: hard denies never become executable approvals; allowed risky actions still create one approval.

### Task 3: Give terminal proposals a server-owned business outcome

**Files:**
- Edit: `app/agent/models.py`
- Edit: `app/agent/loop.py`
- Edit: `app/tools/executor.py`
- Edit: `app/workers/agent_worker.py`
- Test: `tests/test_agent_loop.py`
- Test: `tests/test_tools.py`
- Test: `tests/test_agent_worker.py`

- [x] **Step 1: Add failing terminal-proposal tests**

Cover missing fields, invalid values, unknown intake, risk, and complete intake with no tool. Assert a non-empty deterministic execution-path entry and one of these stable outcomes:

```text
awaiting_input / missing_required_fields
awaiting_input / invalid_field_values
rejected / unknown_intake_type
urgent / safety_or_legal_risk
ready / no_action_requested
```

No case is created for `no_action_requested`, but the durable job result explicitly records `routing_only`; it must not use `FINAL` as the business reason.

- [x] **Step 2: Extend the executor protocol with terminal evaluation**

Add:

```python
class ToolExecutor(Protocol):
    def execute(self, proposal: AgentProposal, tool_call: ToolCall) -> ToolExecutionResult: ...
    def evaluate_terminal(self, proposal: AgentProposal) -> ToolExecutionResult: ...
```

`PolicyGatedToolExecutor.evaluate_terminal` must use the same field validator and policy hard gates and return typed evidence with `action_key=None`. A risky terminal proposal records `review_required` as a routing-only business outcome; it must not create an `Approval` because there is no executable command to authorize. Task 8 later materializes that outcome as a durable `review_flag` artifact.

- [x] **Step 3: Replace the early `proposal.is_terminal` return**

In `AgentLoop.run`, call `self.tools.evaluate_terminal(proposal)`, append its evidence, and finish with `StopReason.EXECUTOR_STOPPED`. Keep `StopReason.FINAL` only for historical deserialization; new successful jobs must have a deterministic routing reason.

- [x] **Step 4: Persist the terminal business outcome**

Extend `_result_summary` with `business_outcome: Literal["routing_only", "tool_result", "approval_requested"]`. Reject completed run persistence when neither execution evidence nor deterministic preflight is present.

- [x] **Step 5: Run focused tests**

Run:

```powershell
pytest tests/test_agent_loop.py tests/test_tools.py tests/test_agent_worker.py -q
```

Expected: every terminal proposal has a policy-derived outcome; model stop alone cannot produce `succeeded`.

### Task 4: Send the compiled tenant contract to the provider

**Files:**
- Edit: `app/tools/registry.py`
- Edit: `app/runtime/factory.py`
- Test: `tests/test_runtime.py`
- Test: `tests/test_openai_compatible_provider.py`

- [x] **Step 1: Add failing provider-catalog contract tests**

For freight, repair, and education, parse the actual system message and assert intake descriptions/required fields, field type/options, and action argument schemas. Assert denied, policy-only, and unavailable capabilities are absent.

- [x] **Step 2: Extend immutable action metadata**

Add `description: str` to `ActionCapability` and populate every built-in action. Reject blank or overlong descriptions in `__post_init__`.

- [x] **Step 3: Build a bounded structured catalog**

Replace the names-only catalog with:

```python
catalog = {
    "intake_types": [
        {
            "name": item.name,
            "description": item.description,
            "required_fields": list(item.required_fields),
        }
        for item in config.intake_types
    ],
    "fields": {
        name: {
            "type": definition.type.value,
            "label": definition.label,
            "options": list(definition.options) if definition.options else None,
        }
        for name, definition in config.fields.items()
    },
    "actions": [
        {
            "name": capability.key,
            "description": capability.description,
            "arguments_schema": capability.arguments_model.model_json_schema(),
        }
        for capability in available_capabilities
    ],
}
```

Serialize with the existing bounded JSON helper. Do not move authorization into the prompt.

- [x] **Step 4: Run provider/runtime tests**

Run:

```powershell
pytest tests/test_runtime.py tests/test_openai_compatible_provider.py -q
```

Expected: all three scenario catalogs expose the exact immutable contracts and no forbidden action.

---

## Phase 2: Approval and job lifecycle

### Task 5: Resolve approval and job atomically

**Files:**
- Edit: `app/domain/approval_repository.py`
- Edit: `app/domain/approvals.py`
- Edit: `app/domain/jobs.py`
- Edit: `app/observability/events.py`
- Test: `tests/test_approvals.py`
- Test: `tests/test_postgres_approvals.py`
- Test: `tests/test_jobs_api.py`

- [x] **Step 1: Add failing lifecycle tests**

Using PostgreSQL, cover approve, reject, expire, repeated identical decision, conflicting repeated decision, and approval-versus-timeout race. Assert approval and job status/result/timestamps in the same transaction and exactly one mutation/event. A skipped integration test is not acceptance evidence.

- [x] **Step 2: Correct the parked-job timestamp contract**

In `create_for_running_job`, set:

```python
job.status = "awaiting_approval"
job.finished_at = None
job.lease_expires_at = None
```

Keep the safe approval identity in `job.result`.

- [x] **Step 3: Add one repository transition for final decisions**

Implement a method that receives the already locked approval/job and applies exactly one mapping:

```text
approved -> job.status=succeeded, error_code=None, finished_at=now
rejected -> job.status=succeeded, error_code=None, finished_at=now
expired  -> job.status=failed, error_code=approval_expired, finished_at=now
```

Persist `business_outcome`, `approval_id`, `approval_status`, and safe result references. Never queue a continuation and never clear `side_effect_committed_at`.

- [x] **Step 4: Use the transition from decision and expiry paths**

Call the shared transition from explicit approve/reject, deadline-past decision, and `expire_due`. Lock the job by `(tenant_id, job_id)` after locking the approval. Repeated decisions return the stored terminal response without executing or emitting new business events.

- [ ] **Step 5: Run approval tests with PostgreSQL**

Run:

```powershell
$env:DATABASE_URL = '<reachable test PostgreSQL URL>'
pytest tests/test_approvals.py tests/test_postgres_approvals.py tests/test_jobs_api.py -q
```

Expected: no skips; job and approval lifecycle assertions pass; one case/artifact and one approved-action event exist after repeated approval.

### Task 6: Add tenant-scoped approval review projections

**Files:**
- Edit: `app/domain/approval_repository.py`
- Edit: `app/domain/approvals.py`
- Edit: `app/api/approvals.py`
- Test: `tests/test_approvals_api.py`

- [x] **Step 1: Add failing detail/list API tests**

Cover `GET /v1/approvals/{approval_id}` and `GET /v1/approvals?status=pending`. Assert same-tenant access, cross-tenant 404, bounded pagination, and a closed response containing action, target case, validated fields, policy reason, expiry, status, and execution result reference. Assert no source snapshot, rationale, credential, or arbitrary metadata.

- [x] **Step 2: Add closed Pydantic read models**

Define `ApprovalReviewRead` and `ApprovalReviewPage` with `extra="forbid"`. Project only validated `PendingAction.known_fields`; reject malformed legacy payloads with a safe `executable=False` projection rather than exposing raw JSON.

- [x] **Step 3: Add tenant predicates to both queries**

Every lookup and list query must include `Approval.tenant_id == authenticated_tenant.id`. Order pending approvals by `(expires_at, id)` and cap `limit` at 100.

- [x] **Step 4: Run API tests**

Run:

```powershell
pytest tests/test_approvals_api.py tests/test_approvals.py -q
```

Expected: authorized operators can inspect the approval contract; another tenant receives the same 404 as an unknown ID.

---

## Phase 3: Source preservation, durable artifacts, and readable results

### Task 7: Preserve body and document as separate untrusted sources

**Files:**
- Edit: `app/domain/intake.py`
- Edit: `app/runtime/factory.py`
- Edit: `app/inbound/service.py`
- Test: `tests/test_intake.py`
- Test: `tests/test_inbound_webhook.py`
- Test: `tests/test_runtime.py`
- Test: `tests/test_tools.py`

- [x] **Step 1: Replace the marker-body tests**

Assert that document enqueue persists the original bounded message body, the normalized document remains separate, and the provider receives labelled `body` and `document_text`. Cover contact only in body, freight fields only in document, a body correction conflicting with the attachment, and invalid/missing excerpts.

- [x] **Step 2: Preserve the body in both document snapshot paths**

Replace both `body="Document received."` assignments with `body=command.document.body`. Keep the body in the canonical hash and bounded by the existing inbound model limit.

- [x] **Step 3: Send labelled sources and enforce precedence**

Use:

```python
source_data = {
    "channel": source.channel,
    "subject": source.subject,
    "sources": {
        "body": source.body,
        "document": source.document.text,
    },
    "field_precedence": ["body", "document"],
}
```

The field validator from Task 1, not provider compliance, remains authoritative.

- [x] **Step 4: Run intake/runtime tests**

Run:

```powershell
pytest tests/test_intake.py tests/test_inbound_webhook.py tests/test_runtime.py tests/test_tools.py -q
```

Expected: body and document both reach runtime; verified body values override verified document values.

### Task 8: Persist drafts/review flags and expose typed job references

**Files:**
- Edit: `app/db/models.py`
- Create: `alembic/versions/<revision>_add_job_artifacts.py`
- Create: `app/domain/artifacts.py`
- Create: `app/domain/artifact_repository.py`
- Create: `app/api/artifacts.py`
- Edit: `app/main.py`
- Edit: `app/tools/ports.py`
- Edit: `app/tools/in_memory.py`
- Edit: `app/tools/postgres.py`
- Edit: `app/tools/executor.py`
- Edit: `app/agent/models.py`
- Edit: `app/domain/jobs.py`
- Edit: `app/domain/job_repository.py`
- Test: `tests/test_migrations.py`
- Test: `tests/test_tools.py`
- Test: `tests/test_postgres_tool_port.py`
- Test: `tests/test_artifacts_api.py`
- Test: `tests/test_jobs_api.py`

- [x] **Step 1: Add failing artifact and result-reference tests**

Cover normal and approved draft/review actions, exactly-once persistence, same-tenant retrieval, cross-tenant 404, and `POST intake -> poll job -> GET referenced case/artifact`. Assert diagnostic events and job summaries contain IDs/kinds only, never draft bodies or review text.

- [x] **Step 2: Add an append-only artifact migration**

Create `job_artifacts` with UUID `id`, `tenant_id`, `job_id`, `kind IN ('reply_draft','review_flag')`, JSONB `payload`, timestamps, unique `(tenant_id, job_id, kind)`, and composite `(tenant_id, job_id)` foreign key to `agent_jobs`. Add tenant/id and tenant/job indexes. Implement a complete downgrade that drops only objects created by this revision.

- [x] **Step 3: Add typed artifact and result-reference models**

Use a closed reference contract:

```python
class ResultReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: Literal["case", "reply_draft", "review_flag"]
    id: UUID
```

Create separate response payloads for reply drafts and review flags. Keep their business content in the authenticated artifact endpoint, not in logs or job summary JSON.

- [x] **Step 4: Persist artifacts in normal and approval handlers**

Replace `persisted=False` returns with artifact creation through tenant-scoped ports. Return `ResultReference` from the handler and attach it to typed tool evidence. Use the unique constraint to return the existing artifact on replay.

- [x] **Step 5: Project result references from durable relations**

Join `IdempotencyRecord.case_id` and `JobArtifact` in `JobRepository.get_read_for_tenant`. Add `result_references: tuple[ResultReference, ...]` to `JobRead`. Validate tenant ownership in the query; do not trust arbitrary IDs stored in `agent_jobs.result`.

- [ ] **Step 6: Run migration, tool, API, and job tests with PostgreSQL**

Run:

```powershell
$env:DATABASE_URL = '<reachable test PostgreSQL URL>'
alembic upgrade head
pytest tests/test_migrations.py tests/test_tools.py tests/test_postgres_tool_port.py tests/test_artifacts_api.py tests/test_jobs_api.py -q
```

Expected: no skips; every operational draft/review result is retrievable; normal case creation exposes a tenant-checked case reference.

### Task 9: Make failed jobs readable without executable profile reconstruction

**Files:**
- Edit: `app/domain/jobs.py`
- Edit: `app/domain/job_repository.py`
- Edit: `app/api/jobs.py`
- Test: `tests/test_jobs_api.py`

- [x] **Step 1: Add failing invalid-snapshot/capability tests**

Create failed rows with `snapshot_incompatible` and `capability_unavailable`, then corrupt the profile snapshot or remove the runtime capability. Assert HTTP 200 with identity, status, timestamps, and allowlisted error code. Assert scenario/action enrichment is absent and no snapshot content is returned.

- [x] **Step 2: Split base status from optional enrichment**

Make `scenario_key: SafeIdentifier | None` optional. Build the base `JobRead` before attempting `resolve_persisted_profile`. On profile failure, return base fields, approval identity, result references, and `_safe_error_code`; omit execution-path/action enrichment.

- [x] **Step 3: Remove the 503 mapping**

`GET /v1/jobs/{job_id}` should return 404 only for unknown/cross-tenant IDs. Profile reconstruction failure is data in a failed job, not endpoint unavailability.

- [x] **Step 4: Run job API tests**

Run:

```powershell
pytest tests/test_jobs_api.py tests/test_phase12_review_fixes.py -q
```

Expected: failed jobs remain readable after snapshot or capability incompatibility.

---

## Phase 4: Worker and document-resource resilience

### Task 10: Supervise transient persistence failures and add process restart/readiness

**Files:**
- Edit: `app/core/config.py`
- Edit: `.env.example`
- Edit: `app/workers/agent_worker.py`
- Create: `app/workers/healthcheck.py`
- Edit: `docker-compose.yml`
- Test: `tests/test_agent_worker.py`
- Test: `tests/test_config.py`

- [x] **Step 1: Add failing supervisor tests**

Inject transient failures during expiry, recovery, claim, and finalization. Assert `serve` logs a closed persistence outcome, waits bounded exponential backoff with jitter, and resumes. Assert `CancelledError`, configuration errors, and programming errors are not swallowed.

- [x] **Step 2: Add explicit settings**

Add positive bounded settings:

```python
worker_concurrency: int = Field(default=4, ge=1, le=32)
worker_persistence_backoff_initial_seconds: float = Field(default=0.5, gt=0)
worker_persistence_backoff_max_seconds: float = Field(default=30.0, gt=0)
worker_job_timeout_seconds: float = Field(default=120.0, gt=0)
worker_gateway_timeout_seconds: float = Field(default=15.0, gt=0)
worker_shutdown_grace_seconds: float = Field(default=10.0, gt=0)
database_statement_timeout_ms: int = Field(default=10_000, ge=100)
database_lock_timeout_ms: int = Field(default=5_000, ge=100)
document_parse_timeout_seconds: float = Field(default=15.0, gt=0)
document_parse_concurrency: int = Field(default=2, ge=1, le=8)
```

Document the environment names in `.env.example`.

- [x] **Step 3: Classify and supervise persistence failures**

Wrap SQLAlchemy/asyncpg operational failures from repository operations in `TransientPersistenceError`. Catch only that type in `serve`; reset backoff after any successful polling cycle. Re-raise cancellation and unexpected exceptions.

- [x] **Step 4: Add Docker restart and readiness**

Set `restart: unless-stopped` for `worker`. Add a healthcheck that executes `python -m app.workers.healthcheck` and performs a bounded `SELECT 1` with the worker database settings. It must not print credentials or exception text.

- [x] **Step 5: Run supervisor tests**

Run:

```powershell
pytest tests/test_agent_worker.py tests/test_config.py -q
docker compose config
```

Expected: transient persistence errors do not end the serve loop; Compose renders a worker restart policy and healthcheck.

### Task 11: Add job/gateway/database deadlines and fair bounded concurrency

**Files:**
- Edit: `app/runtime/gateway.py`
- Edit: `app/policy/models.py`
- Edit: `app/tools/postgres.py`
- Edit: `app/domain/job_repository.py`
- Edit: `app/workers/agent_worker.py`
- Edit: `app/db/session.py`
- Test: `tests/test_runtime_gateway.py`
- Test: `tests/test_agent_worker.py`
- Test: `tests/test_job_repository.py`
- Test: `tests/test_postgres_integration.py`

- [x] **Step 1: Add failing deadline/fairness tests**

Cover gateway timeout cancellation, whole-job timeout, immediate lease loss while a tool is blocked, PostgreSQL lock timeout, stale-attempt finalization, maximum concurrency, and one slow tenant beside a fast tenant. Verify no stale attempt commits after lease loss or timeout.

- [x] **Step 2: Add cooperative cancellation**

Put a `threading.Event` cancellation token in the trusted runtime context. Check it before scheduling a gateway call, after return, and immediately before every database mutation. On lease loss, job timeout, or shutdown, set the token and cancel pending gateway futures.

- [x] **Step 3: Bound gateway and database waits**

Use `future.result(timeout=worker_gateway_timeout_seconds)` and cancel the future on timeout. Apply PostgreSQL `statement_timeout` and `lock_timeout` for worker transactions. Map timeout errors to stable retryable codes without exposing backend text.

- [x] **Step 4: Observe run, heartbeat, and deadline concurrently**

Replace the sequential wait with `asyncio.wait(..., return_when=FIRST_COMPLETED)`. A lost heartbeat or deadline sets cancellation immediately. Await the thread only for the configured shutdown grace; preserve attempt fencing and mark uncertain only if the durable side-effect marker exists.

- [x] **Step 5: Add fair bounded concurrency**

Maintain at most `worker_concurrency` job tasks and at most one active job per tenant. Extend `claim_next` with `excluded_tenant_ids`; apply the exclusion in the locked PostgreSQL claim query so queued work from another tenant can use free capacity.

- [ ] **Step 6: Run worker tests with PostgreSQL**

Run:

```powershell
$env:DATABASE_URL = '<reachable test PostgreSQL URL>'
pytest tests/test_runtime_gateway.py tests/test_agent_worker.py tests/test_job_repository.py tests/test_postgres_integration.py -q
```

Expected: no skips; blocked work terminates within configured budgets, fast-tenant work completes beside a slow tenant, and stale attempts cannot commit.

### Task 12: Bound PDF extraction work during parsing

**Files:**
- Edit: `app/documents/normalizer.py`
- Edit: `app/domain/intake.py`
- Test: `tests/test_documents.py`
- Test: `tests/test_intake.py`

- [x] **Step 1: Add failing parser-budget tests**

Use fake readers/pages to verify extraction stops as soon as cumulative text exceeds `MAX_DOCUMENT_TEXT_CHARS`, parsing concurrency never exceeds the configured semaphore, and a timeout returns a typed `document_processing_timeout` without source data.

- [x] **Step 2: Enforce the text bound incrementally**

Replace the unbounded join with a bounded accumulator:

```python
parts: list[str] = []
text_chars = 0
for page in reader.pages:
    part = page.extract_text() or ""
    text_chars += len(part) + (1 if parts else 0)
    if text_chars > MAX_DOCUMENT_TEXT_CHARS:
        return self._error(..., DocumentExtractionError.DOCUMENT_TEXT_TOO_LARGE)
    parts.append(part)
extracted = "\n".join(parts)
```

- [x] **Step 3: Bound normalization admission and time**

Guard `asyncio.to_thread(normalizer.normalize, ...)` with one process-local semaphore sized by `document_parse_concurrency` and `asyncio.timeout(document_parse_timeout_seconds)`. Map timeout to a new allowlisted extraction error; never persist partial text.

- [x] **Step 4: Run document tests**

Run:

```powershell
pytest tests/test_documents.py tests/test_intake.py -q
```

Expected: parser work is bounded by bytes, pages, cumulative text, concurrency, and wall-clock time.

---

## Phase 5: Contract documentation and release evidence

### Task 13: Update public contracts and verify the delivered artifact

**Files:**
- Edit: `README.md`
- Edit: `docs/runbooks/live-provider-demo.md`
- Edit: `tests/test_observability.py`
- Edit: `tests/test_freight_eval_models.py`
- Create: `scripts/verify_release_artifact.py`
- Test: `tests/test_release_artifact.py`

- [x] **Step 1: Document the final lifecycle**

Document approval terminal mappings, `finished_at`, awaiting-input resubmission with a new idempotency key, source precedence, result/artifact retrieval, timeout/error codes, and worker readiness. State that business source/artifact retention is deployment policy and is not controlled by telemetry redaction.

- [x] **Step 2: Add a release-artifact verifier**

The script must build from an explicit allowlist rooted at the repository, reject Python caches/secrets, and verify presence of `pyproject.toml`, `Dockerfile`, `alembic.ini`, `.github/workflows/ci.yml`, `.gitattributes`, `docs/runbooks/live-provider-demo.md`, `app/`, `alembic/`, `examples/`, and tests. It must never read `.env` into output.

- [x] **Step 3: Test the archive contract**

Create a temporary artifact in the test, inspect its member list, and verify a clean extracted copy can run package metadata discovery. Keep Docker build and live migration as CI/release jobs rather than unit tests.

- [x] **Step 4: Run the full provider-free gates**

Run:

```powershell
pytest -q
pytest evals/test_freight.py -q
git diff --check
```

Expected: all unit/API tests pass; PostgreSQL tests show no skips when `DATABASE_URL` is configured; freight reports `50 passed`; `git diff --check` prints nothing.

- [ ] **Step 5: Run clean release acceptance**

From the generated artifact in a clean environment:

```powershell
pip install -e ".[dev]"
docker compose build api worker
docker compose up -d db
alembic upgrade head
pytest -q
```

Expected: install, both images, empty-database migration, and the complete suite succeed without reconstructing missing files.

- [x] **Step 6: Review the completed implementation**

Call the `code-review` skill on the final diff with this plan and the audit as context. Prioritize authorization, tenant predicates, transaction atomicity, source/secret leakage, cancellation, and concurrency. Do not make review corrections until they are discussed with the user.

## Self-review

- Every confirmed finding maps to at least one task: F01/F05/F06/F07 to Tasks 1–4; F02 to Task 5; F08 to Task 7; F09/F10 to Task 8; F12 to Task 9; F03 to Task 10; F04 to Tasks 11–12.
- F11 is handled as release acceptance because the missing files are present in the current checkout.
- The tenant/profile binding and retention-duration questions are explicitly excluded because the audit does not supply product requirements for them.
- No task weakens tenant predicates, immutable snapshot verification, attempt fencing, idempotency, or source-free diagnostics.
- New schema work is append-only and has PostgreSQL acceptance; skipped integration tests are not treated as evidence.
- The plan contains no `TBD`, `TODO`, or unnamed validation/error-handling steps.
