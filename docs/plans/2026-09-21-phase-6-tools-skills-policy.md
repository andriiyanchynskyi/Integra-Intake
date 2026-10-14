# Implementation plan: Phase 6 tools, skills, and deterministic policy

> **For agents:** Use the `executing-plans` skill to implement this plan task by task. Steps use checkbox syntax (`- [ ]`) for tracking.

**Purpose:** Add policy-gated, typed internal tools and versioned skills without
coupling the existing provider-independent synchronous agent loop to provider
HTTP, FastAPI, real async persistence, or external side effects.

**Architecture:** `AgentLoop` gains only a generic proposal-aware executor
outcome. `PolicyGatedToolExecutor` owns a trusted runtime context, delegates
to pure `PolicyEngine`/existing `TenantRouter`, and calls a typed registered
tool only after an allow outcome. Tools use tenant-scoped in-memory ports in
this phase. Skills build typed system messages and never execute tools.

**Key files/stack:** Python 3.12, Pydantic v2, pytest, existing immutable agent
models, `TenantRouter`, YAML examples, `httpx.MockTransport`; all tests are
offline.

---

## File map

| File | Responsibility |
| --- | --- |
| `app/agent/models.py` | Generic execution result and system transcript role. |
| `app/agent/loop.py` | Pass proposal to executor; append exact execution data; generically stop when requested. |
| `app/providers/openai_compatible.py` | Serialize a system transcript message only. |
| `app/tenants/routing.py` | Preserve router defaults while accepting registered-action and complete-fields context. |
| `app/policy/models.py`, `engine.py` | Pure trusted-policy input/output and router adapter. |
| `app/tools/models.py`, `ports.py`, `in_memory.py`, `executor.py` | Tool schemas, tenant-scoped test backend, registry, and policy gate. |
| `app/skills/models.py`, `definitions.py` | Versioned skill contracts, prompts, typed system messages, eval-case references. |
| `evals/skills/*.yaml` | Fixed success/adversarial data for the three skills. |
| `examples/*.yaml` | Add the three new internal action names without changing Phase-3 routing invariants. |
| `tests/test_policy.py`, `test_tools.py`, `test_skills.py` | New offline behavior coverage. |
| Existing agent/provider/router tests | Regression updates for generic executor outcome and system role. |

No API, Alembic, ORM, database session, domain service, worker, queue, or
external-integration file changes.

## Task 1: Add generic loop execution and system-message support

**Files:**
- Edit: `app/agent/models.py`
- Edit: `app/agent/loop.py`
- Edit: `app/agent/__init__.py`
- Edit: `app/providers/openai_compatible.py`
- Test: `tests/test_agent_loop.py`
- Test: `tests/test_openai_compatible_provider.py`

- [ ] **Step 1: Add the generic immutable execution contract.**

In `app/agent/models.py`, add `SYSTEM = "system"` to `MessageRole`;
add `EXECUTOR_STOPPED = "executor_stopped"` to `StopReason`; and add:

```python
@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    data: ToolData = None
    continue_run: bool = True
    final_response: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "data", _freeze_data(self.data))
        if self.continue_run and self.final_response is not None:
            raise ValueError("continuing execution cannot set final_response")
```

Export it from `app/agent/__init__.py`. It is deliberately generic: it has
no policy import or policy enum.

- [ ] **Step 2: Make the executor proposal-aware without adding policy to the loop.**

Replace the executor protocol with:

```python
class ToolExecutor(Protocol):
    def execute(
        self, proposal: AgentProposal, tool_call: ToolCall
    ) -> ToolExecutionResult: ...
```

In `AgentLoop.run`, execute and append data as follows:

```python
execution = self.tools.execute(deepcopy(proposal), deepcopy(tool_call))
messages.append(
    AgentMessage(
        role=MessageRole.TOOL,
        tool_call=tool_call,
        tool_result=deepcopy(execution.data),
    )
)
if not execution.continue_run:
    return AgentRunResult(
        status=RunStatus.COMPLETED,
        reason=StopReason.EXECUTOR_STOPPED,
        messages=tuple(deepcopy(messages)),
        steps=steps,
        final_response=execution.final_response,
        proposal=deepcopy(proposal),
    )
```

Keep the existing third-consecutive-name check before this call and keep all
existing max-step/deep-copy branches unchanged.

- [ ] **Step 3: Serialize system messages at the provider boundary.**

At the start of the message-role switch in `_provider_messages`, add:

```python
if message.role is MessageRole.SYSTEM:
    provider_messages.append(
        {"role": "system", "content": message.content or ""}
    )
    continue
```

Do not allow a system message between an assistant tool call and its required
tool result: the existing pending-call check remains first.

- [ ] **Step 4: Invoke `unit-test-writer` for agent/provider regression updates.**

Give the test-writing agent this exact scope: it may edit only
`tests/test_agent_loop.py` and `tests/test_openai_compatible_provider.py`.

```text
Update offline pytest regressions for the generic ToolExecutionResult contract.
Every existing fake ToolExecutor must accept (proposal, tool_call), record the
call, and return ToolExecutionResult(data=<configured result>). Preserve all
existing Phase-4 assertions for MAX_STEPS=8, third consecutive same-tool stop
before execution, immutable snapshots, ordered transcripts, and exact None
tool results. Add a test that an executor result with continue_run=False adds
its data once and produces StopReason.EXECUTOR_STOPPED.

Add one httpx.MockTransport provider test proving a leading
AgentMessage(role=MessageRole.SYSTEM, content='skill prompt') serializes as
the provider message {'role': 'system', 'content': 'skill prompt'}. Preserve
all schema closure, validation-retry, endpoint normalization, and secret
redaction tests; no network service or credential may be used.
```

- [ ] **Step 5: Run focused agent/provider tests.**

Run:

```powershell
pytest tests/test_agent_loop.py tests/test_openai_compatible_provider.py -q
```

Expected: all Phase-4/5 assertions pass with the new generic contract.

## Task 2: Extend deterministic routing only with trusted execution metadata

**Files:**
- Edit: `app/tenants/routing.py`
- Test: `tests/test_tenant_config.py`

- [ ] **Step 1: Extend the assessment with backward-compatible defaults.**

Change the dataclass to include:

```python
@dataclass(frozen=True, slots=True)
class RoutingAssessment:
    intake_type: str
    present_fields: frozenset[str]
    contains_safety_or_legal_risk: bool
    requested_action: str
    requires_complete_fields: bool = True
    registered_actions: frozenset[str] | None = None
```

Existing direct router callers continue to require complete fields and use all
configured actions because both new values have defaults.

- [ ] **Step 2: Preserve the exact seven-check order in `TenantRouter.route`.**

Keep risk and unknown-type branches first. Change missing computation to:

```python
missing = tuple(sorted(set(intake.required_fields) - assessment.present_fields))
if assessment.requires_complete_fields and missing:
    return RoutingOutcome(
        status=RoutingStatus.AWAITING_INPUT,
        decision=RoutingDecision.DENY,
        missing_required_fields=missing,
        reason="missing_required_fields",
    )
```

Immediately after it, make unknown action include the runnable tool catalog:

```python
if (
    assessment.registered_actions is not None
    and assessment.requested_action not in assessment.registered_actions
):
    return RoutingOutcome(
        status=RoutingStatus.REJECTED,
        decision=RoutingDecision.DENY,
        missing_required_fields=(),
        reason="action_not_configured",
    )
rule = self.config.action_policy.get(assessment.requested_action)
```

Retain the existing `rule is None`, `not rule.allowed`, approval, and allow
branches in exactly that order.

- [ ] **Step 3: Invoke `unit-test-writer` for routing regressions.**

Give the test-writing agent this exact scope: it may edit only
`tests/test_tenant_config.py`.

```text
Extend existing TenantRouter pytest coverage without changing its old tests.
Add one case where an incomplete load/request with a configured
find_customer action and requires_complete_fields=False reaches the configured
action outcome rather than missing_required_fields. Add one where send_reply
is configured in YAML but registered_actions=frozenset({'create_case'}), so
the exact result is rejected/deny/action_not_configured. Keep the existing
risk, unknown intake, missing field, unknown action, disallowed, approval, and
allowed precedence assertions. Do not use database, LLM, or network fixtures.
```

- [ ] **Step 4: Run routing tests.**

Run:

```powershell
pytest tests/test_tenant_config.py -q
```

Expected: all prior YAML/routing tests pass plus the two new cases.

## Task 3: Implement pure policy types and engine

**Files:**
- Create: `app/policy/__init__.py`
- Create: `app/policy/models.py`
- Create: `app/policy/engine.py`
- Test: `tests/test_policy.py`

- [ ] **Step 1: Define trusted and untrusted inputs in `app/policy/models.py`.**

Create strict immutable types with these fields:

```python
@dataclass(frozen=True, slots=True)
class RiskSignals:
    safety_or_legal_risk: bool = False

@dataclass(frozen=True, slots=True)
class TrustedSource:
    channel: str
    subject: str
    body: str

@dataclass(frozen=True, slots=True)
class TrustedToolRuntimeContext:
    tenant_id: UUID
    tenant_config: TenantConfig
    source: TrustedSource | None = None
    case_id: UUID | None = None
    risk_signals: RiskSignals = RiskSignals()

@dataclass(frozen=True, slots=True)
class PolicyInput:
    proposal: AgentProposal
    requested_action: str
    requires_complete_fields: bool
    registered_actions: frozenset[str]
    runtime: TrustedToolRuntimeContext
```

Define `PolicyOutcome` with `RoutingDecision`, `RoutingStatus`,
`reason: str`, and `missing_required_fields: tuple[str, ...]`. It exposes
`as_tool_data()`, returning only strings/lists/bools needed by the transcript;
it never serializes tenant ID or raw source body.

- [ ] **Step 2: Implement `PolicyEngine` by adapting to `TenantRouter`.**

Use this core logic in `app/policy/engine.py`:

```python
def evaluate(self, value: PolicyInput) -> PolicyOutcome:
    known_fields = set(value.runtime.tenant_config.fields)
    present = frozenset(
        item.name for item in value.proposal.fields if item.name in known_fields
    )
    assessment = RoutingAssessment(
        intake_type=value.proposal.intake_type or "",
        present_fields=present,
        contains_safety_or_legal_risk=(
            value.runtime.risk_signals.safety_or_legal_risk
            or value.proposal.contains_injection_or_override_attempt
        ),
        requested_action=value.requested_action,
        requires_complete_fields=value.requires_complete_fields,
        registered_actions=value.registered_actions,
    )
    routed = TenantRouter(value.runtime.tenant_config).route(assessment)
    return PolicyOutcome(
        decision=routed.decision,
        status=routed.status,
        reason=routed.reason,
        missing_required_fields=routed.missing_required_fields,
    )
```

A proposal's `missing_required_fields`, priority, confidence, tool arguments,
and tenant-like values must not alter this calculation.

- [ ] **Step 3: Export only typed policy public API.**

Export `PolicyEngine`, `PolicyInput`, `PolicyOutcome`,
`RiskSignals`, `TrustedSource`, and `TrustedToolRuntimeContext` from
`app/policy/__init__.py`.

- [ ] **Step 4: Invoke the required `unit-test-writer` skill for policy tests.**

Give the test-writing agent this exact scope: it may edit only
`tests/test_policy.py`; it must not edit product files.

```text
Write offline pytest tests for app.policy.PolicyEngine. Build TenantConfig from
a dict with one intake type 'request', required field 'summary', configured
actions create_case, find_customer, and approval_action. Build valid
AgentProposal values without a real LLM.

Assert exact precedence: trusted risk beats unknown type/action; model injection
true requests review; proposal injection false cannot clear trusted risk;
unknown type beats missing/action; a missing summary blocks create_case but not
find_customer when requires_complete_fields is false; unregistered action
send_reply is unknown even if YAML contains it; YAML allowed=false is denied;
requires_approval=true is pending approval; an allowed complete create_case is
ready/allow. Assert high confidence never changes a deny/review result and
proposal.missing_required_fields never changes recomputed missing fields.
```

- [ ] **Step 5: Run policy tests.**

Run:

```powershell
pytest tests/test_policy.py -q
```

Expected: all precedence outcomes have deterministic status, decision, reason,
and sorted missing fields.

## Task 4: Implement typed tools, tenant-scoped ports, and policy gate

**Files:**
- Create: `app/tools/__init__.py`
- Create: `app/tools/models.py`
- Create: `app/tools/ports.py`
- Create: `app/tools/in_memory.py`
- Create: `app/tools/executor.py`
- Test: `tests/test_tools.py`

- [ ] **Step 1: Define closed tool inputs/results and metadata.**

In `app/tools/models.py`, create a strict Pydantic base
(`extra="forbid", frozen=True, strict=True`) and these models:

```python
class FindCustomerArgs(StrictToolModel):
    email: str | None = None
    external_id: str | None = None

    @model_validator(mode="after")
    def require_lookup_key(self) -> Self:
        if not self.email and not self.external_id:
            raise ValueError("email or external_id is required")
        return self

class CreateCaseArgs(StrictToolModel):
    customer_id: UUID | None = None

class UpdateCaseFieldsArgs(StrictToolModel):
    case_id: UUID

class CreateReplyDraftArgs(StrictToolModel):
    case_id: UUID | None = None

class FlagForReviewArgs(StrictToolModel):
    note: str | None = Field(default=None, max_length=500)
```

Define closed result models for each table in the approved specification and
`ToolDefinition(name, requires_complete_fields, arguments_model, handler)`.
Handlers receive parsed arguments, proposal, and trusted runtime context.

- [ ] **Step 2: Define the tenant-scoped port.**

In `app/tools/ports.py`, use a synchronous protocol only for this phase:

```python
class TenantToolPort(Protocol):
    def find_customer(
        self, tenant_id: UUID, *, email: str | None, external_id: str | None
    ) -> CustomerSummary | None: ...
    def create_case(
        self, tenant_id: UUID, source: TrustedSource, *,
        customer_id: UUID | None, fields: Mapping[str, ToolData],
    ) -> CreatedCase: ...
    def update_case_fields(
        self, tenant_id: UUID, case_id: UUID, *,
        fields: Mapping[str, ToolData],
    ) -> UpdatedCase | None: ...
```

The port has no async bridge and no SQLAlchemy import.

- [ ] **Step 3: Implement deterministic in-memory tenant isolation.**

Create `InMemoryTenantToolPort` with dictionaries keyed by UUID records that
each carry `tenant_id`. `find_customer` filters before matching.
`update_case_fields` returns `None` unless both case ID and tenant ID match,
then merges only supplied fields and returns sorted changed names.
`create_case` creates a generated UUID, fixed `received` status, and copies
the trusted source only. Never accept source fields from tool arguments.

- [ ] **Step 4: Implement registry and policy-gated executor.**

`PolicyGatedToolExecutor.execute` must:

```python
definition = self._definitions.get(tool_call.name)
policy = self._policy.evaluate(
    PolicyInput(
        proposal=proposal,
        requested_action=tool_call.name,
        requires_complete_fields=(
            definition.requires_complete_fields if definition else False
        ),
        registered_actions=frozenset(self._definitions),
        runtime=self._runtime,
    )
)
if policy.decision is not RoutingDecision.ALLOW:
    return ToolExecutionResult(
        data=policy.as_tool_data(),
        continue_run=False,
        final_response=policy.reason,
    )
if definition is None:
    raise AssertionError("allowed action must have a registered tool")
try:
    arguments = definition.arguments_model.model_validate(
        dict(tool_call.arguments)
    )
except ValidationError:
    return ToolExecutionResult(
        data={"outcome": "tool_arguments_invalid"},
        continue_run=False,
        final_response="tool_arguments_invalid",
    )
return ToolExecutionResult(data=definition.handler(arguments, proposal, self._runtime))
```

Register only the five approved names. Implement draft/review handlers as
transient JSON-compatible results. Do not register `send_reply` or
`close_case`.

- [ ] **Step 5: Invoke `unit-test-writer` for tool coverage.**

Give it this exact scope: it may edit only `tests/test_tools.py`.

```text
Write offline pytest tests for PolicyGatedToolExecutor, InMemoryTenantToolPort,
and the five typed tools. Use two tenant UUIDs, a validated profile with all
five tool actions, a trusted source, and valid AgentProposal values.

Cover: find_customer finds only its tenant and returns not found for another;
cross-tenant update_case_fields returns not found and does not mutate; create
case copies trusted channel/subject/body rather than malicious tool arguments;
only declared proposal fields are written; invalid closed arguments stop without
mutation; create_reply_draft and flag_for_review return persisted=false and
create no records; a denied/review policy outcome never calls the handler and
stops the generic loop result. Include a real executed handler returning None
to prove ToolExecutionResult preserves transcript None.
```

- [ ] **Step 6: Run focused tool tests.**

Run:

```powershell
pytest tests/test_tools.py -q
```

Expected: no database URL, provider credential, or network service is needed.

## Task 5: Add skills, eval data, and tenant action catalog entries

**Files:**
- Create: `app/skills/__init__.py`
- Create: `app/skills/models.py`
- Create: `app/skills/definitions.py`
- Create: `evals/skills/extract_load_request.v1.yaml`
- Create: `evals/skills/classify_intake.v1.yaml`
- Create: `evals/skills/draft_ops_reply.v1.yaml`
- Edit: `examples/freight-broker.yaml`
- Edit: `examples/repair-service.yaml`
- Edit: `examples/language-school.yaml`
- Test: `tests/test_skills.py`
- Test: `tests/test_tenant_config.py`

- [ ] **Step 1: Define a strict declarative skill contract.**

Create frozen `SkillDefinition` with `name`, `input_model`,
`output_model`, `system_prompt`, and `eval_fixture: Path`. Validate the
name against `^[a-z][a-z0-9_]*\\.v[1-9][0-9]*$` and require a non-empty
prompt. Add `build_system_message()` returning:

```python
AgentMessage(
    role=MessageRole.SYSTEM,
    content=self.system_prompt,
)
```

Create strict input/output Pydantic models for the three approved skill names;
their output is candidate data, never a `ToolCall` or policy decision.

- [ ] **Step 2: Create concrete skill definitions.**

Export `EXTRACT_LOAD_REQUEST_V1`, `CLASSIFY_INTAKE_V1`, and
`DRAFT_OPS_REPLY_V1`. Use exact names
`extract_load_request.v1`, `classify_intake.v1`, and
`draft_ops_reply.v1`; give each a concise system prompt that says it proposes
structured candidates only and must not authorize actions.

- [ ] **Step 3: Add fixed eval fixtures.**

Each YAML fixture contains two cases: one normal and one adversarial prompt
injection case. Every case includes `input` and `expected`; the adversarial
expected value records a non-authorizing candidate and no tool permission.
Use synthetic contact addresses and no secrets.

- [ ] **Step 4: Add action policy entries to all examples.**

Under each `action_policy`, add exactly:

```yaml
find_customer: {allowed: true, requires_approval: false}
update_case_fields: {allowed: true, requires_approval: false}
create_reply_draft: {allowed: true, requires_approval: false}
```

Keep existing `create_case`, `flag_for_review`, `send_reply`,
`close_case`, routing statuses, and `always_approval_actions` unchanged.

- [ ] **Step 5: Invoke `unit-test-writer` for skills/config tests.**

Give it this exact scope: it may edit only `tests/test_skills.py` and
`tests/test_tenant_config.py`.

```text
Write offline pytest tests for the three skill definitions and fixtures.
Assert exact versioned names, non-empty prompts, strict input/output model
rejection of unexpected keys, build_system_message uses MessageRole.SYSTEM,
each checked-in YAML has a normal and adversarial case with input/expected, and
no output model exposes a ToolCall/policy allow field. Extend profile tests to
assert all three examples load and declare the five runnable Phase-6 actions
while preserving send_reply in always_approval_actions.
```

- [ ] **Step 6: Run skill/config tests.**

Run:

```powershell
pytest tests/test_skills.py tests/test_tenant_config.py -q
```

Expected: all eval fixtures parse, profile strictness remains intact, and no
provider is called.

## Task 6: Integrate test-agent output and protect Phase-4/5 regressions

**Files:**
- Review: test files changed by the required test-writing agents

- [ ] **Step 1: Inspect the test-agent diffs against their assigned scopes.**

Run:

```powershell
git diff -- tests/test_agent_loop.py tests/test_openai_compatible_provider.py tests/test_tenant_config.py tests/test_policy.py tests/test_tools.py tests/test_skills.py
```

Expected: only the behaviors specified in Tasks 1–5 are asserted; no test adds
a real LLM, provider network call, async DB bridge, or external integration.
If a test needs a different product contract, stop and discuss that expansion
with the user instead of editing tests manually.

- [ ] **Step 2: Run all focused tests.**

Run:

```powershell
pytest tests/test_policy.py tests/test_tools.py tests/test_skills.py tests/test_agent_loop.py tests/test_openai_compatible_provider.py tests/test_tenant_config.py -q
```

Expected: all tests pass offline. If a test fails, fix only the production/test
contract implicated by its asserted behavior; do not add an async DB bridge or
a real LLM call.

## Task 7: Required quality, review, and evidence gates

**Files:**
- Review: all changes from Tasks 1–6

- [ ] **Step 1: Run the complete suite and whitespace check.**

Run:

```powershell
pytest -q
git diff --check
```

Expected: all unit tests pass; existing PostgreSQL tests may skip only under
their established `DATABASE_URL` condition; `git diff --check` prints no
diagnostics.

- [ ] **Step 2: Call the `code-review` skill on the final diff.**

Provide the approved design, this plan, and the final diff. Require review of:
provider/loop separation; no sync/async bridge; exact seven-step precedence;
tenant context ownership and cross-tenant tool behavior; non-authorizing
confidence/injection handling; no phase-7/8 scope leakage; system transcript
serialization; and all preserved loop controls. Do not make corrections based
on findings before discussing them with the user.

- [ ] **Step 3: Call `verification-before-completion` with evidence.**

Provide focused-test output, full `pytest -q`, `git diff --check`, the final
diff, and the code-review result. Do not claim Phase 6 complete until the skill
confirms each accepted requirement.

## Plan self-review

| Design requirement | Task(s) |
| --- | --- |
| Provider-independent generic loop; system message | 1, 6 |
| Existing YAML/router precedence retained | 2, 3 |
| Trusted tenant/source/risk input | 3, 4 |
| Five typed policy-gated tools | 4 |
| No persistence bridge, queue, send, approval persistence | 4, 7 |
| Versioned skills with schemas/prompt/evals | 5 |
| Profile actions and future-action deny | 2, 5 |
| Offline tests and required test writer | 3–6 |
| Code review and verification gates | 7 |

The plan contains no unassigned design requirement, and all names match the
approved specification.
