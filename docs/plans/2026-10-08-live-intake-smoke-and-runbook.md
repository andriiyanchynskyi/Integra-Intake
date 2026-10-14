# Implementation plan: Live intake smoke and runbook

> **For agents:** Use the executing-plans skill to implement this plan task by task. Steps use checkbox syntax (`- [ ]`) for tracking.

**Purpose:** Demonstrate body intake and signed webhook intake through the running API, worker, provider, policy, and tool boundaries with a complete source-free console report.

**Architecture:** Carry typed execution evidence from `PolicyGatedToolExecutor` through `AgentLoop` into the worker's bounded JSON result summary, then expose it through the tenant-scoped job projection. Extend the existing smoke script with real webhook submission, named cases, strict custom JSON input, expectations, and human output while retaining `--json` for automation. Keep the 50-case freight eval as the exhaustive provider-free behavior gate.

**Key files/stack:** Python 3.12, FastAPI, Pydantic v2, httpx, PostgreSQL JSON job results, pytest, PowerShell runbook commands.

## File map

- `app/agent/models.py`: define immutable tool and agent execution-evidence types; attach them to tool and run results.
- `app/agent/loop.py`: add the current step to tool evidence and return the bounded execution path.
- `app/agent/__init__.py`: export the new evidence types through the lazy public interface.
- `app/tools/executor.py`: create evidence from the same policy and tool outcomes used for observation events.
- `app/workers/agent_worker.py`: persist agent steps, stop reason, routing, and execution path in the safe job summary.
- `app/domain/jobs.py`: validate and project the new safe fields from durable JSON.
- `scripts/live_provider_smoke.py`: submit body or signed-webhook requests, run named/custom cases, validate results, and render human or JSON output.
- `tests/test_agent_loop.py`: verify evidence accumulation and step numbering.
- `tests/test_tools.py`: verify evidence for allow, deny, approval, invalid arguments, backend failure, and unknown actions.
- `tests/test_agent_worker.py`: verify source-free evidence persistence and deterministic preflight summaries.
- `tests/test_jobs_api.py`: verify closed public projection, historical compatibility, and unknown-action redaction.
- `tests/test_live_provider_smoke.py`: verify transports, fixtures, idempotency cases, custom input, expectations, rendering, and exit codes without network access.
- `docs/runbooks/live-provider-demo.md`: add the ten requested live checks and explain hybrid evidence.
- `README.md`: document default human output and `--json`.
- `tests/test_observability.py`: extend the documentation contract for the new runbook sections.
- `docs/specs/2026-10-08-live-intake-smoke-and-runbook-design.md`: approved design; no implementation edits expected.

No database migration is required. `agent_jobs.result` already stores JSON, and historical result objects remain valid when the new keys are absent.

---

### Task 1: Add typed execution evidence to the agent loop

**Files:**
- Edit: `app/agent/models.py`
- Edit: `app/agent/loop.py`
- Edit: `app/agent/__init__.py`
- Test: `tests/test_agent_loop.py`

- [ ] **Step 1: Add a failing loop test for bounded step evidence**

Add an executor that returns one typed evidence value and a test that checks the loop-owned step number:

```python
class _EvidenceTool:
    definitions = {"create_case": object()}

    def execute(self, proposal, tool_call):
        return ToolExecutionResult(
            data={"outcome": "executed"},
            continue_run=False,
            final_response="done",
            evidence=ToolExecutionEvidence(
                action_key="create_case",
                action_known=True,
                policy_decision="allow",
                routing_status="ready",
                policy_reason="action_allowed",
                missing_required_fields=(),
                tool_outcome="executed",
                side_effect_committed=True,
            ),
        )


def test_agent_loop_attaches_step_to_typed_tool_evidence() -> None:
    result = AgentLoop(
        _SequenceLLM([proposal("create_case")]),
        _EvidenceTool(),
    ).run(initial_messages)

    assert result.execution_path == (
        ExecutionPathEntry(
            step=1,
            action_key="create_case",
            action_known=True,
            policy_decision="allow",
            routing_status="ready",
            policy_reason="action_allowed",
            missing_required_fields=(),
            tool_outcome="executed",
            side_effect_committed=True,
        ),
    )
```

Import `ExecutionPathEntry` and `ToolExecutionEvidence` from `app.agent`. Add a second assertion to an existing fake-executor test proving that a `ToolExecutionResult` without evidence produces `execution_path == ()`.

- [ ] **Step 2: Run the focused tests and confirm the missing contracts**

Run:

```powershell
pytest tests/test_agent_loop.py -q
```

Expected: collection or assertion failure because the evidence types and result fields do not exist.

- [ ] **Step 3: Define immutable evidence contracts**

Add these dataclasses before `ToolExecutionResult` in `app/agent/models.py`:

```python
@dataclass(frozen=True, slots=True)
class ToolExecutionEvidence:
    action_key: str | None
    action_known: bool
    policy_decision: str
    routing_status: str
    policy_reason: str
    missing_required_fields: tuple[str, ...] = ()
    tool_outcome: str | None = None
    side_effect_committed: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "missing_required_fields",
            tuple(sorted(set(self.missing_required_fields))),
        )
        if self.action_known != (self.action_key is not None):
            raise ValueError("known action evidence requires an action key")


@dataclass(frozen=True, slots=True)
class ExecutionPathEntry:
    step: int
    action_key: str | None
    action_known: bool
    policy_decision: str
    routing_status: str
    policy_reason: str
    missing_required_fields: tuple[str, ...] = ()
    tool_outcome: str | None = None
    side_effect_committed: bool = False

    @classmethod
    def from_tool(
        cls,
        step: int,
        evidence: ToolExecutionEvidence,
    ) -> "ExecutionPathEntry":
        if step < 1:
            raise ValueError("execution path step must be positive")
        return cls(step=step, **asdict(evidence))
```

Import `asdict` from `dataclasses`. Add `evidence: ToolExecutionEvidence | None = None` to `ToolExecutionResult` and `execution_path: tuple[ExecutionPathEntry, ...] = ()` to `AgentRunResult`. Preserve defaults so existing fake executors and direct result construction remain compatible.

- [ ] **Step 4: Accumulate evidence inside `AgentLoop.run`**

Initialize `execution_path: list[ExecutionPathEntry] = []` beside `steps`. Immediately after `self.tools.execute(...)`, append typed evidence when present:

```python
if execution.evidence is not None:
    execution_path.append(
        ExecutionPathEntry.from_tool(steps, execution.evidence)
    )
```

Pass `execution_path=tuple(execution_path)` to every `AgentRunResult` constructor in `run`, including final, invalid, repeated-tool, failed-tool, executor-stopped, and max-step exits. Provider exceptions still propagate and do not create a result.

- [ ] **Step 5: Export the contracts**

Add `ExecutionPathEntry` and `ToolExecutionEvidence` to the `TYPE_CHECKING` imports, `_MODEL_EXPORTS`, and `__all__` in `app/agent/__init__.py`.

- [ ] **Step 6: Run the agent-loop suite**

Run:

```powershell
pytest tests/test_agent_loop.py -q
```

Expected: all tests pass; existing generic executors return an empty path.

### Task 2: Produce policy and tool evidence at the execution boundary

**Files:**
- Edit: `app/tools/executor.py`
- Test: `tests/test_tools.py`

- [ ] **Step 1: Add failing evidence tests for each terminal branch**

Extend existing allow, deny, approval, invalid-argument, backend-failure, and unknown-action tests with assertions in this form:

```python
result = executor.execute(proposal, ToolCall(name="create_case"))

assert result.evidence == ToolExecutionEvidence(
    action_key="create_case",
    action_known=True,
    policy_decision="allow",
    routing_status="ready",
    policy_reason="action_allowed",
    missing_required_fields=(),
    tool_outcome="executed",
    side_effect_committed=True,
)
```

Use these branch-specific expectations:

| Branch | `policy_decision` | `tool_outcome` | `side_effect_committed` |
| --- | --- | --- | --- |
| policy deny | `deny` | `None` | `False` |
| approval request | `needs_approval` | `None` | `False` |
| invalid arguments | `allow` | `invalid_arguments` | `False` |
| backend exception | `allow` | `backend_failure` | `False` |
| successful `find_customer` | `allow` | `executed` or `no_result` | `False` |
| successful `create_case` | `allow` | `executed` | `True` |
| unknown action | `deny` | `None` | `False` |

For an unknown action, assert `action_key is None` and `action_known is False`; never assert or serialize the model-provided unknown name.

- [ ] **Step 2: Run the tool tests and confirm evidence is absent**

Run:

```powershell
pytest tests/test_tools.py -q
```

Expected: the new assertions fail because `PolicyGatedToolExecutor` does not populate evidence.

- [ ] **Step 3: Add one evidence builder**

Import `replace`, `ToolExecutionEvidence`, and `PolicyOutcome`. Add this helper to `PolicyGatedToolExecutor`:

```python
@classmethod
def _with_evidence(
    cls,
    result: ToolExecutionResult,
    *,
    action_key: str,
    action_known: bool,
    policy: PolicyOutcome,
    tool_executed: bool,
    side_effect_committed: bool = False,
) -> ToolExecutionResult:
    return replace(
        result,
        evidence=ToolExecutionEvidence(
            action_key=action_key if action_known else None,
            action_known=action_known,
            policy_decision=policy.decision.value,
            routing_status=policy.status.value,
            policy_reason=policy.reason,
            missing_required_fields=policy.missing_required_fields,
            tool_outcome=(
                cls._tool_outcome(result.data).value
                if tool_executed
                else None
            ),
            side_effect_committed=side_effect_committed,
        ),
    )
```

`replace` comes from `dataclasses`. Keep `_tool_outcome` as the single mapping from tool data to the closed `OutcomeCode` values used by logs and durable evidence.

- [ ] **Step 4: Attach evidence in every return branch**

Wrap the result returned by each branch with `_with_evidence`:

```python
return self._with_evidence(
    result,
    action_key=tool_call.name,
    action_known=definition is not None,
    policy=policy,
    tool_executed=False,
)
```

Use `tool_executed=False` for policy denial and approval requests. Use `tool_executed=True` for invalid arguments and every handler result or handler failure. Pass `side_effect_committed=definition.commits_side_effect` only after a handler returns successfully; all stopped or failed branches pass `False`.

Do not derive evidence from observation events or tool payloads after execution. The policy outcome and `_tool_outcome` mapping remain the authoritative closed values.

- [ ] **Step 5: Verify tools and agent integration**

Run:

```powershell
pytest tests/test_tools.py tests/test_agent_loop.py -q
```

Expected: all tests pass. Existing event assertions remain unchanged, proving logs and durable evidence use the same decision source.

### Task 3: Persist and expose the safe execution path

**Files:**
- Edit: `app/workers/agent_worker.py`
- Edit: `app/domain/jobs.py`
- Test: `tests/test_agent_worker.py`
- Test: `tests/test_jobs_api.py`

- [ ] **Step 1: Add failing worker-summary tests**

Construct an `AgentRunResult` with one `ExecutionPathEntry` and assert the exact primitive summary:

```python
assert AgentWorker._result_summary(result) == {
    "status": "completed",
    "reason": "executor_stopped",
    "steps": 1,
    "routing_status": "ready",
    "routing_reason": "action_allowed",
    "missing_required_fields": [],
    "execution_path": [
        {
            "step": 1,
            "action_key": "create_case",
            "action_known": True,
            "policy_decision": "allow",
            "routing_status": "ready",
            "policy_reason": "action_allowed",
            "missing_required_fields": [],
            "tool_outcome": "executed",
            "side_effect_committed": True,
        }
    ],
}
```

Update the malformed-document preflight assertion to require
`"execution_path": []` beside `"steps": 0`. Retain assertions that source,
proposal, rationale, document content, tool arguments, and tool results do not
appear in `repr(summary)`.

- [ ] **Step 2: Add failing job-projection tests**

Extend `_row` in `tests/test_jobs_api.py` with the safe execution path above.
Assert these additions to `safe.model_dump(mode="json")`:

```python
"agent_steps": 1,
"stop_reason": "executor_stopped",
"execution_path": [
    {
        "step": 1,
        "action_key": "create_case",
        "action_known": True,
        "policy_decision": "allow",
        "routing_status": "ready",
        "policy_reason": "action_allowed",
        "missing_required_fields": [],
        "tool_outcome": "executed",
        "side_effect_committed": True,
    }
],
```

Add separate tests for:

```python
assert project_job_read(historical_row).agent_steps is None
assert project_job_read(historical_row).stop_reason is None
assert project_job_read(historical_row).execution_path == ()
```

and for an item with `action_known=False` plus a non-null action key. The unsafe
path must project as an empty tuple and its action string must not appear in
the rendered JSON.

- [ ] **Step 3: Run the focused worker and API tests**

Run:

```powershell
pytest tests/test_agent_worker.py tests/test_jobs_api.py -q
```

Expected: failures show that summaries and `JobRead` lack the new fields.

- [ ] **Step 4: Serialize evidence in worker summaries**

Replace transcript inspection in `_result_summary` with the typed path. Build
only primitive dictionaries:

```python
path = [
    {
        "step": item.step,
        "action_key": item.action_key,
        "action_known": item.action_known,
        "policy_decision": item.policy_decision,
        "routing_status": item.routing_status,
        "policy_reason": item.policy_reason,
        "missing_required_fields": list(item.missing_required_fields),
        "tool_outcome": item.tool_outcome,
        "side_effect_committed": item.side_effect_committed,
    }
    for item in result.execution_path
]
summary: dict[str, object] = {
    "status": result.status.value,
    "reason": result.reason.value,
    "steps": result.steps,
    "execution_path": path,
}
if path:
    terminal = path[-1]
    summary["routing_status"] = terminal["routing_status"]
    summary["routing_reason"] = terminal["policy_reason"]
    summary["missing_required_fields"] = terminal["missing_required_fields"]
```

Add `"execution_path": []` to the successful unreadable-document preflight
summary. Do not persist `final_response`, messages, proposals, or raw tool data.

- [ ] **Step 5: Add closed read models and parsers**

In `app/domain/jobs.py`, add:

```python
class ExecutionPathRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    step: int
    action_key: str | None
    action_known: bool
    policy_decision: RoutingDecision
    routing_status: RoutingStatus
    policy_reason: PolicyReason
    missing_required_fields: tuple[str, ...] = ()
    tool_outcome: OutcomeCode | None = None
    side_effect_committed: bool
```

Add to `JobRead`:

```python
agent_steps: int | None = None
stop_reason: StopReason | None = None
execution_path: tuple[ExecutionPathRead, ...] = ()
```

Create `_safe_execution_values(result, registered_actions)` that:

1. accepts `steps` only as a non-boolean integer from 0 through `MAX_STEPS`;
2. converts `reason` through `StopReason`;
3. requires `execution_path` to be a list of at most `MAX_STEPS` mappings;
4. converts decision, status, reason, and outcome through their enums;
5. requires step numbers from 1 through `MAX_STEPS` in ascending order;
6. sorts and deduplicates safe missing-field identifiers;
7. requires a known action to belong to the resolved profile's registered actions;
8. requires an unknown action to have `action_key=None`;
9. returns an empty path when any item fails validation.

Pass the resolved profile's action keys into this helper from `project_job_read`
and populate the three new response fields. Keep existing top-level routing and
approval projection behavior compatible with historical rows.

- [ ] **Step 6: Run the focused persistence and API tests**

Run:

```powershell
pytest tests/test_agent_worker.py tests/test_jobs_api.py -q
```

Expected: all tests pass, including historical and source-redaction cases.

### Task 4: Add real webhook transport and named live cases

**Files:**
- Edit: `scripts/live_provider_smoke.py`
- Test: `tests/test_live_provider_smoke.py`

- [ ] **Step 1: Add failing webhook request tests**

Use `httpx.MockTransport` to capture requests. Add tests that assert:

```python
assert request.url.path == "/v1/inbound/email/webhook"
assert request.headers["X-API-Key"] == RAW_TENANT_KEY
assert request.headers["X-Inbound-Timestamp"] == "1800000000"
assert verify_inbound_webhook_signature(
    RAW_TENANT_KEY,
    "1800000000",
    request.headers["X-Inbound-Signature"],
    request.content,
    now=1_800_000_000,
)
```

Decode the captured JSON and assert that body-only requests contain no
attachment, text and PDF cases contain exactly one correctly typed base64
attachment, and no request contains `tenant_id` or a client-selected channel.

- [ ] **Step 2: Add failing duplicate and conflict tests**

For `webhook-duplicate`, return two `202` responses with the same job ID and
trace, followed by the terminal job response. Assert `duplicate_reused is
True`. For `webhook-conflict`, return `202`, then `409`, then the original job;
assert `conflict_rejected is True` and that the script never prints the `409`
body.

- [ ] **Step 3: Run focused transport tests**

Run:

```powershell
pytest tests/test_live_provider_smoke.py -q
```

Expected: new tests fail because the script only posts body intake.

- [ ] **Step 4: Define the CLI transport and case contracts**

Add string enums and immutable specs:

```python
class SmokeTransport(str, Enum):
    INTAKE = "intake"
    WEBHOOK = "webhook"


class DemoCase(str, Enum):
    BODY_COMPLETE = "body-complete"
    BODY_INCOMPLETE = "body-incomplete"
    WEBHOOK_BODY = "webhook-body"
    WEBHOOK_DOCUMENT_TXT = "webhook-document-txt"
    WEBHOOK_DOCUMENT_PDF = "webhook-document-pdf"
    WEBHOOK_DOCUMENT_MALFORMED = "webhook-document-malformed"
    WEBHOOK_DUPLICATE = "webhook-duplicate"
    WEBHOOK_CONFLICT = "webhook-conflict"
```

Create a `DemoCaseSpec` mapping with synthetic payloads, implied transport,
fixture path, and expectation name. Resolve fixture paths from
`Path(__file__).resolve().parents[1]`, not from the caller's current directory.
Use `01-complete-en.txt`, `03-complete.pdf`, and `08-malformed.pdf` for the
three document cases.

- [ ] **Step 5: Build exact signed webhook bytes**

Import `sign_inbound_webhook` and `MAX_DOCUMENT_BYTES`. Build the envelope with
the same compact UTF-8 JSON encoding as the current sender:

```python
raw_body = json.dumps(
    payload,
    ensure_ascii=False,
    separators=(",", ":"),
).encode("utf-8")
signed_at = int(wall_clock())
headers = {
    "Content-Type": "application/json",
    "X-API-Key": api_key,
    "X-Inbound-Timestamp": str(signed_at),
    "X-Inbound-Signature": sign_inbound_webhook(
        api_key,
        signed_at,
        raw_body,
    ),
}
```

Post `content=raw_body` through the injected `httpx.Client`. Check suffix and
size before reading an attachment. Keep body intake on `/v1/intake` with a new
UUID-backed `Idempotency-Key`.

- [ ] **Step 6: Implement duplicate and conflict orchestration**

Use one generated provider ID within a command. Duplicate sends identical
envelope content twice and requires both `202` responses to return the same job
ID and bound trace. Conflict changes only the synthetic body on the second
delivery and requires `409`; it retains the first job for polling. Map all
unexpected statuses to stable local error codes without copying response text.

- [ ] **Step 7: Verify all transport cases offline**

Run:

```powershell
pytest tests/test_live_provider_smoke.py -q
```

Expected: body, webhook, text, PDF, malformed, duplicate, and conflict request
tests pass without network access.

### Task 5: Add strict custom input, expectations, and complete output

**Files:**
- Edit: `scripts/live_provider_smoke.py`
- Test: `tests/test_live_provider_smoke.py`

- [ ] **Step 1: Add failing custom-input tests**

Use `tmp_path` to write one intake JSON file and one webhook JSON file with a
relative attachment. Assert strict rejection of unknown keys, missing required
keys, unsupported transports, absolute or escaping attachment paths,
unsupported suffixes, oversized files, malformed JSON, and simultaneous
`--demo-case` plus `--input-file`.

Use this accepted shape in the test:

```python
payload_path.write_text(
    json.dumps(
        {
            "transport": "webhook",
            "provider_id": "custom-message-1",
            "from_addr": "dispatcher@example.test",
            "subject": "Synthetic rate confirmation",
            "body": "Attached synthetic document",
            "attachment_path": "fixture.txt",
        }
    ),
    encoding="utf-8",
)
```

- [ ] **Step 2: Add failing expectation and renderer tests**

Build a terminal job containing timestamps, routing, approval, error, and two
execution-path items. Assert that human output contains labels for every safe
field and derived queue/run/total milliseconds. Assert `NOT OBSERVED` for null
values and `NONE` for an empty path.

For JSON output, assert the exact top-level allowlist:

```python
assert set(report) == {
    "result",
    "demo_case",
    "profile",
    "transport",
    "endpoint",
    "input_kind",
    "acceptance_status",
    "duplicate_reused",
    "conflict_rejected",
    "checks",
    "job",
    "error_code",
}
```

Assert that source body, subject, sender, attachment bytes, raw key, signature,
provider response, and exception text appear in neither rendering mode.

- [ ] **Step 3: Run the focused tests and confirm missing behavior**

Run:

```powershell
pytest tests/test_live_provider_smoke.py -q
```

Expected: failures identify missing input parsing, expectations, human output,
and exit-code separation.

- [ ] **Step 4: Parse custom files with closed schemas**

Add strict Pydantic models local to the script:

```python
class _SmokeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    transport: Literal["intake", "webhook"]
    channel: str | None = None
    provider_id: str | None = None
    from_addr: str | None = None
    subject: str
    body: str
    attachment_path: str | None = None
```

Use a model validator to require `channel` only for intake; require
`provider_id` and `from_addr` only for webhook; reject attachment paths for
intake. Bound identifiers and text with the existing request limits. Read at
most 1 MiB of JSON, resolve relative attachments inside the input file's
directory, and reject paths that escape that directory.

- [ ] **Step 5: Validate the extended job response**

Extend `SAFE_JOB_RESPONSE_KEYS` with `agent_steps`, `stop_reason`, and
`execution_path`. Validate each path item against an exact key set and closed
value sets for decisions, routing statuses, policy reasons, and tool outcomes.
Require ascending steps between 1 and 8, a null action for unknown actions, and
safe lowercase identifiers for known actions and missing fields.

Reject unknown server fields with `unsafe_job_response`; never echo the raw
response.

- [ ] **Step 6: Implement case expectations**

Return named safe checks such as:

```python
{"name": "routing_status", "expected": "awaiting_input", "passed": True}
```

Apply these assertions:

- `body-complete`: terminal `succeeded`, or `awaiting_approval` with pending
  approval evidence and a `needs_approval` path item;
- `body-incomplete`: `succeeded`, `awaiting_input`, missing `contact`, no
  committed side effect;
- standard webhook cases: `202`, matching trace, and an accepted terminal job;
- malformed document: `succeeded`, `awaiting_input`,
  `document_unreadable`, zero steps, empty path, no approval;
- duplicate: same job and trace;
- conflict: second delivery returns `409`.

Implement repeatable `--expect-path tool` and `--expect-path approval`. A
missing expected path raises `expected_path_not_observed` and exits 1.

- [ ] **Step 7: Render human output and preserve safe JSON**

Create `render_human(report) -> str`. Use stable plain-text sections and no
terminal color dependency:

```text
LIVE INTAKE SMOKE: PASS
Case:             webhook-document-txt
Profile:          freight-broker
Transport:        webhook
Endpoint:         /v1/inbound/email/webhook
Input:            text attachment
HTTP accepted:    202
Job ID:           ...
Trace ID:         ...
Status:           succeeded
Worker attempts:  1
Agent steps:      2
Stop reason:      final
Routing:          ready / action_allowed
Missing fields:   NONE
Execution path:
  1. create_case | policy=allow | tool=executed | side_effect=yes
Approval:         NOT OBSERVED
Error:            NOT OBSERVED
Checks:           PASS
```

Print every safe job field even when absent. Compute durations only from parsed
server timestamps. `--json` uses `json.dumps(..., ensure_ascii=False,
sort_keys=True)` on the same safe report.

- [ ] **Step 8: Separate operational and usage exit codes**

Return 0 for PASS, 1 for network/HTTP/job/expectation failures, and 2 for
argument, credential, or input-file errors. Keep error output source-free and
include the validated last job state when available.

- [ ] **Step 9: Run the complete smoke unit suite**

Run:

```powershell
pytest tests/test_live_provider_smoke.py -q
```

Expected: all smoke tests pass without a live API, database, provider, or
credential.

### Task 6: Document the hybrid live demonstration

**Files:**
- Edit: `docs/runbooks/live-provider-demo.md`
- Edit: `README.md`
- Edit: `tests/test_observability.py`

- [ ] **Step 1: Add a failing documentation contract**

Extend the existing runbook test with exact required markers:

```python
for scenario in (
    "body-complete",
    "body-incomplete",
    "webhook-body",
    "webhook-document-txt",
    "webhook-document-pdf",
    "webhook-document-malformed",
    "webhook-duplicate",
    "webhook-conflict",
    "direct-case",
    "approval-decision",
):
    assert scenario in text

assert "--transport webhook" in text
assert "--input-file" in text
assert "--json" in text
assert "pytest evals/test_freight.py -q" in text
assert "NOT OBSERVED" in text
```

- [ ] **Step 2: Run the documentation test and confirm missing sections**

Run:

```powershell
pytest tests/test_observability.py -q
```

Expected: failure because the runbook lacks the named scenarios and new CLI
contract.

- [ ] **Step 3: Update the runbook setup and evidence model**

Keep the current database, migration, API, worker, and key-provisioning steps.
Add a short evidence table that states:

- live smoke proves real HTTP, PostgreSQL, worker, configured provider, and the
  policy/tool path observed in that run;
- `pytest evals/test_freight.py -q` proves all 50 deterministic freight cases;
- live success alone does not claim every policy branch;
- `NOT OBSERVED` means the run did not reach that path.

- [ ] **Step 4: Add copyable commands for the eight queued-intake cases**

Document one PowerShell command per `--demo-case`, including `--json` variants
where captured machine output helps. Include the approved transport-only file
command:

```powershell
python scripts/live_provider_smoke.py `
  --profile freight-broker `
  --transport webhook `
  --attachment evals/fixtures/docs/01-complete-en.txt `
  --base-url http://localhost:8000
```

Add one custom body JSON file and one custom webhook JSON file example, then
run each with `--input-file`. Use synthetic content and do not print keys.

- [ ] **Step 5: Add the direct-case live check**

Document `POST /v1/cases`, capture the returned ID, and perform the tenant-
scoped GET:

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

State that this endpoint is synchronous and does not call the worker or
provider.

- [ ] **Step 6: Update the conditional approval check**

Retain separate operator provisioning. Require a real approval ID from an
`awaiting_approval` smoke result, display the closed decision response fields,
and show `decision="reject"` as a request-body variant. If no pending approval
exists, print `SKIPPED: no pending approval`; do not fabricate an ID.

- [ ] **Step 7: Update README and troubleshooting**

State that human output is the default and `--json` is the source-free machine
format. Keep detailed commands in the runbook. Add troubleshooting for failed
expectations, `NOT OBSERVED`, webhook signature rejection, duplicate mismatch,
expected conflict absence, poll timeout, and provider unavailability.

- [ ] **Step 8: Run documentation and smoke tests**

Run:

```powershell
pytest tests/test_observability.py tests/test_live_provider_smoke.py -q
```

Expected: all tests pass and documentation contains no real credentials or
customer data.

### Task 7: Run repository quality checks

**Files:**
- No source changes.

- [ ] **Step 1: Run the focused feature suite**

Run:

```powershell
pytest tests/test_agent_loop.py tests/test_tools.py `
  tests/test_agent_worker.py tests/test_jobs_api.py `
  tests/test_live_provider_smoke.py tests/test_observability.py -q
```

Expected: all focused agent, policy/tool, worker, API, smoke, and documentation
tests pass.

- [ ] **Step 2: Run the deterministic freight gate**

Run:

```powershell
pytest evals/test_freight.py -q
```

Expected: all 50 freight cases pass without a provider credential or network.

- [ ] **Step 3: Run the full configured test suite**

Run:

```powershell
pytest -q
```

Expected: all configured tests pass. PostgreSQL integration tests may skip only
when `DATABASE_URL` is absent or unreachable; report those skips separately
from passing integration evidence.

- [ ] **Step 4: Check whitespace and patch formatting**

Run:

```powershell
git diff --check
```

Expected: exit code 0 with no whitespace errors. Do not alter unrelated user
changes to satisfy this check; report a pre-existing warning with its path.

### Task 8: Review the completed change

**Files:**
- Review the final diff only.

- [ ] **Step 1: Call the `code-review` skill**

Call the `code-review` skill on the changes made by this plan. Provide the
final diff and the context from
`docs/specs/2026-10-08-live-intake-smoke-and-runbook-design.md`. Review typed
evidence ownership, source and secret redaction, tenant isolation, webhook
signing, file-path containment, idempotency semantics, response allowlists,
backward compatibility, and test coverage. Do not make corrections; discuss
findings with the user first.

## Spec coverage

- Tasks 1 and 2 implement typed policy/tool evidence without coupling durable
  state to the observability sink.
- Task 3 persists and exposes the bounded source-free job projection and keeps
  historical rows readable.
- Tasks 4 and 5 implement the real body/webhook transports, eight named queued
  cases, custom files, expectations, and human/JSON output.
- Task 6 documents the eight queued cases, `direct-case`,
  `approval-decision`, alternative profiles, and the deterministic freight
  gate.
- Task 7 verifies focused behavior, all 50 freight cases, the full suite, and
  patch formatting.
- Task 8 reviews the final diff against the approved privacy and security
  boundaries before any review-driven correction.
