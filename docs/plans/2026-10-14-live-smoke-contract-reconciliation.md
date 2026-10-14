# Implementation plan: Live smoke contract reconciliation

> **For agents:** Use the executing-plans skill to implement this plan task by task. Steps use checkbox syntax (`- [ ]`) for tracking.

**Purpose:** Make the live intake smoke accept the current safe `JobRead` response, preserve validated acceptance evidence on later failures, and correct the migration instructions in the operator runbook.

**Architecture:** Keep the smoke as a fail-closed operational client. Validate the decoded job response through the public `JobRead` model plus explicit smoke-only invariants, carry one source-free progress object through acceptance and polling, and make body and webhook transports share the same polling path. Add a cross-boundary test based on `JobRead.model_dump(mode="json")` so future server/client drift fails in CI.

**Key files/stack:** Python 3.12+, Pydantic v2, httpx mock transports, pytest, FastAPI response models, Markdown runbook contract tests.

**Approved design:** `docs/specs/2026-10-14-live-smoke-contract-reconciliation-design.md`

---

## File map

- `scripts/live_provider_smoke.py` — validate the complete public job response, preserve safe progress on failure, share polling between transports, and render result references.
- `tests/test_live_provider_smoke.py` — reproduce API/smoke schema drift, malformed boundary responses, progress preservation, and safe rendering.
- `tests/test_observability.py` — lock the live-provider runbook to the current one-revision migration baseline and troubleshooting contract.
- `docs/runbooks/live-provider-demo.md` — replace obsolete `002_job_artifacts` instructions and document safe result-reference/failure evidence.

No database, API, worker, provider, policy, approval, artifact, or migration implementation changes belong in this plan.

---

### Task 1: Reconcile the smoke parser with `JobRead`

**Files:**
- Edit: `scripts/live_provider_smoke.py`
- Test: `tests/test_live_provider_smoke.py`

- [ ] **Step 1: Call the `unit-test-writer` skill for the response-contract tests**

Give it the approved design, the current `JobRead` model in `app/domain/jobs.py`, `ResultReference` in `app/agent/models.py`, and the current `_safe_job_response`/`_safe_execution_path` functions. Require tests for the full serialized server model and every newly supported closed value. Do not ask it to change production code.

- [ ] **Step 2: Replace the hand-written minimal job fixture with a complete public projection helper**

Add imports and a helper to `tests/test_live_provider_smoke.py`:

```python
from datetime import datetime, timezone
from uuid import UUID

from app.agent import ResultReference
from app.domain.jobs import JobRead


NOW = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
CASE_ID = UUID("5f83b8cb-8b9c-4abf-999c-91b91aaf95d2")


def _job_payload(
    status: str,
    *,
    scenario_key: str | None = "freight_broker",
    result_references: tuple[ResultReference, ...] = (),
) -> dict[str, object]:
    return JobRead(
        job_id=UUID(JOB_ID),
        trace_id=UUID(TRACE_ID),
        scenario_key=scenario_key,
        status=status,
        attempt_count=1,
        agent_steps=1 if status in {"succeeded", "awaiting_approval"} else None,
        stop_reason=(
            "executor_stopped"
            if status in {"succeeded", "awaiting_approval"}
            else None
        ),
        execution_path=(),
        routing_status="ready" if status == "succeeded" else None,
        routing_reason="no_action_requested" if status == "succeeded" else None,
        missing_required_fields=(),
        approval=None,
        result_references=result_references,
        error_code=None,
        created_at=NOW,
        started_at=NOW if status != "queued" else None,
        finished_at=NOW if status == "succeeded" else None,
    ).model_dump(mode="json")


def _job(
    status: str,
    *,
    scenario_key: str | None = "freight_broker",
    result_references: tuple[ResultReference, ...] = (),
) -> httpx.Response:
    return httpx.Response(
        200,
        json=_job_payload(
            status,
            scenario_key=scenario_key,
            result_references=result_references,
        ),
    )
```

Adapt existing tests that mutate `_job(...).json()`; do not keep a second partial job schema.

- [ ] **Step 3: Add failing cross-boundary and result-reference tests**

Add these tests with the exact current public shape:

```python
def test_safe_job_response_accepts_complete_serialized_job_read() -> None:
    payload = _job_payload(
        "succeeded",
        result_references=(ResultReference(kind="case", id=CASE_ID),),
    )

    safe = smoke._safe_job_response(payload)

    assert set(safe) == set(JobRead.model_fields)
    assert safe["result_references"] == [
        {"kind": "case", "id": str(CASE_ID)}
    ]


@pytest.mark.parametrize(
    "reason",
    ["no_action_requested", "invalid_field_values", "action_not_executable"],
)
def test_safe_job_response_accepts_current_policy_reasons(reason: str) -> None:
    payload = _job_payload("succeeded")
    payload["execution_path"] = [
        {
            "step": 1,
            "action_key": None,
            "action_known": False,
            "policy_decision": "allow" if reason == "no_action_requested" else "deny",
            "routing_status": "ready" if reason == "no_action_requested" else "awaiting_input",
            "policy_reason": reason,
            "missing_required_fields": [],
            "tool_outcome": None,
            "side_effect_committed": False,
        }
    ]

    assert smoke._safe_job_response(payload)["execution_path"][0][
        "policy_reason"
    ] == reason


@pytest.mark.parametrize(
    "references",
    [
        [{"kind": "unknown", "id": str(CASE_ID)}],
        [{"kind": "case", "id": "not-a-uuid"}],
        [
            {"kind": "case", "id": str(CASE_ID)},
            {"kind": "case", "id": str(UUID(int=2))},
        ],
        [
            {"kind": "case", "id": str(UUID(int=1))},
            {"kind": "reply_draft", "id": str(UUID(int=2))},
            {"kind": "review_flag", "id": str(UUID(int=3))},
            {"kind": "case", "id": str(UUID(int=4))},
        ],
    ],
)
def test_safe_job_response_rejects_invalid_result_references(
    references: list[dict[str, str]],
) -> None:
    payload = _job_payload("succeeded")
    payload["result_references"] = references

    with pytest.raises(SmokeFailure, match="malformed_job_response"):
        smoke._safe_job_response(payload)
```

- [ ] **Step 4: Add failing tests for nullable scenarios and remaining public types**

Add parameterized mutations for invalid job/trace UUIDs, timestamps, approval UUID/status, routing status/reason, error code, unsafe missing-field names, and unknown execution-path policy reasons. Add these state-specific assertions:

```python
def test_failed_job_parser_accepts_null_scenario() -> None:
    safe = smoke._safe_job_response(
        _job_payload("failed", scenario_key=None)
    )

    assert safe["scenario_key"] is None


@pytest.mark.parametrize("status", ["queued", "running", "succeeded", "awaiting_approval"])
def test_polling_rejects_null_scenario_for_non_failed_job(status: str) -> None:
    transport = SequenceTransport([_job(status, scenario_key=None)])
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(SmokeFailure, match="scenario_mismatch"):
            smoke._poll_job(
                http_client=client,
                base_url="http://testserver",
                api_key=RAW_TENANT_KEY,
                progress=smoke.SmokeProgress.accepted(JOB_ID, TRACE_ID),
                expected_scenario="freight_broker",
                timeout_seconds=5,
                poll_interval_seconds=0,
                clock=lambda: 1.0,
                sleep=lambda _: None,
            )
    finally:
        client.close()
```

Keep the second test failing until `SmokeProgress` is introduced in Task 2. Do
not weaken it to call the old separate-ID polling signature.

- [ ] **Step 5: Run the new tests and confirm the current implementation fails**

Run:

```powershell
pytest tests/test_live_provider_smoke.py -q
```

Expected: failures include `unsafe_job_response` for `result_references`, `malformed_job_response` for nullable `scenario_key`, and rejection of the newly valid policy reasons.

- [ ] **Step 6: Implement the complete fail-closed parser**

In `scripts/live_provider_smoke.py`:

1. Add `result_references` to `SAFE_JOB_RESPONSE_KEYS`.
2. Parse a copied mapping; never mutate `value`.
3. Validate the shape through `JobRead.model_validate` and convert back with `model_dump(mode="json")`.
4. Before model validation, require job IDs, trace IDs, approval IDs, and result-reference IDs to arrive as strings so Python-side coercion cannot broaden the wire contract.
5. Apply smoke-only closed invariants after Pydantic validation.

Use these constants and structure:

```python
from app.domain.jobs import JobRead
from app.observability.events import PolicyReason, WorkerErrorCode
from app.tenants.config import RoutingStatus


RESULT_REFERENCE_KINDS = frozenset({"case", "reply_draft", "review_flag"})
APPROVAL_STATUSES = frozenset({"pending", "approved", "rejected", "expired"})
ROUTING_REASONS = frozenset({"complete", *(item.value for item in PolicyReason)})
ROUTING_STATUSES = frozenset(item.value for item in RoutingStatus)
WORKER_ERROR_CODES = frozenset(item.value for item in WorkerErrorCode)
MAX_RESULT_REFERENCES = 3
```

Use this result-reference validator:

```python
def _safe_result_references(value: object) -> list[dict[str, str]]:
    if not isinstance(value, list) or len(value) > MAX_RESULT_REFERENCES:
        raise SmokeFailure("malformed_job_response")
    safe: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"kind", "id"}:
            raise SmokeFailure("unsafe_job_response")
        kind = item.get("kind")
        identifier = item.get("id")
        if (
            not isinstance(kind, str)
            or kind not in RESULT_REFERENCE_KINDS
            or kind in seen
            or not isinstance(identifier, str)
        ):
            raise SmokeFailure("malformed_job_response")
        try:
            normalized_id = str(UUID(identifier))
        except ValueError as error:
            raise SmokeFailure("malformed_job_response") from error
        seen.add(kind)
        safe.append({"kind": kind, "id": normalized_id})
    return sorted(safe, key=lambda item: (item["kind"], item["id"]))
```

Catch `ValidationError`, `TypeError`, and `ValueError` from `JobRead`. For a
`ValidationError`, inspect `error.errors()`; if any item has
`type == "extra_forbidden"`, raise `unsafe_job_response`, otherwise raise
`malformed_job_response`. Unknown outer keys must be rejected before Pydantic
validation with `unsafe_job_response`. Validate timezone-aware timestamps and
safe missing-field identifiers after serialization. Accept `scenario_key=None`
in the parser; enforce state-dependent scenario rules in `_poll_job`.

- [ ] **Step 7: Run the focused parser tests**

Run:

```powershell
pytest tests/test_live_provider_smoke.py -q
```

Expected: parser, policy-reason, nullable-scenario, and existing smoke tests pass.

---

### Task 2: Preserve acceptance progress and unify polling

**Files:**
- Edit: `scripts/live_provider_smoke.py`
- Test: `tests/test_live_provider_smoke.py`

- [ ] **Step 1: Call the `unit-test-writer` skill for post-acceptance failure tests**

Require a matrix covering body and webhook acceptance followed by malformed JSON, unsafe fields, `401`, network failure, timeout, failed job with null scenario, trace mismatch, and scenario mismatch. Each assertion must verify that no response body or exception text reaches the report.

- [ ] **Step 2: Add failing progress-preservation tests**

Add this representative body test and parameterize equivalent webhook cases:

```python
def test_failure_after_acceptance_preserves_safe_identity() -> None:
    transport = SequenceTransport(
        [_accepted(), httpx.Response(200, content=b"not-json")]
    )
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(SmokeFailure) as failure:
            run_smoke(
                profile="freight-broker",
                base_url="http://testserver",
                api_key=RAW_TENANT_KEY,
                timeout_seconds=5,
                poll_interval_seconds=0,
                client=client,
                clock=lambda: 1.0,
                sleep=lambda _: None,
    finally:
        client.close()

    arguments = argparse.Namespace(
        transport=None,
        attachment=None,
        input_file=None,
        demo_case=None,
        profile="freight-broker",
    )
    report = smoke._failure_report(arguments, failure.value)
    output = render_human(report)
    assert "HTTP accepted:    202" in output
    assert f"Job ID:           {JOB_ID}" in output
    assert f"Trace ID:         {TRACE_ID}" in output
    assert "Smoke error:      malformed_job_response" in output
    assert "not-json" not in output
```

Import `argparse` in the test module for the failure-report arguments.

Add a terminal failure test:

```python
def test_failed_job_with_null_scenario_keeps_safe_failure_state() -> None:
    payload = _job_payload("failed", scenario_key=None)
    payload["error_code"] = "snapshot_incompatible"
    transport = SequenceTransport([_accepted(), httpx.Response(200, json=payload)])
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(SmokeFailure, match="job_failed") as failure:
            run_smoke(
                profile="freight-broker",
                base_url="http://testserver",
                api_key=RAW_TENANT_KEY,
                client=client,
                timeout_seconds=5,
                poll_interval_seconds=0,
                clock=lambda: 1.0,
                sleep=lambda _: None,
            )
    finally:
        client.close()

    assert failure.value.progress.acceptance_status == 202
    assert failure.value.progress.last_state == smoke._safe_job_response(payload)
```

- [ ] **Step 3: Run the progress tests and confirm they fail**

Run:

```powershell
pytest tests/test_live_provider_smoke.py -q
```

Expected: accepted job/trace values are currently lost, malformed JSON reports `malformed_provider_response`, and the body path still uses its duplicate polling loop.

- [ ] **Step 4: Introduce the immutable safe progress contract**

Add this dataclass near `SmokeFailure`:

```python
@dataclass(frozen=True, slots=True)
class SmokeProgress:
    acceptance_status: int | None = None
    job_id: str | None = None
    trace_id: str | None = None
    last_state: dict[str, object] | None = None
    duplicate_reused: bool | None = None
    conflict_rejected: bool | None = None

    @classmethod
    def accepted(cls, job_id: str, trace_id: str) -> "SmokeProgress":
        return cls(acceptance_status=202, job_id=job_id, trace_id=trace_id)

    def with_job(self, job: Mapping[str, object]) -> "SmokeProgress":
        return replace(self, last_state=deepcopy(dict(job)))
```

Import `replace` from `dataclasses`. Extend `SmokeFailure` to accept one
`progress` value. Keep a read-only `last_state` compatibility property so the
existing tests and callers do not need a flag-day rewrite:

```python
class SmokeFailure(RuntimeError):
    def __init__(
        self,
        code: str,
        *,
        progress: SmokeProgress | None = None,
        last_state: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(code)
        if progress is not None and last_state is not None:
            raise TypeError("progress and last_state are mutually exclusive")
        if last_state is not None:
            safe_job = deepcopy(dict(last_state))
            progress = SmokeProgress(
                acceptance_status=202,
                job_id=str(safe_job["job_id"]),
                trace_id=str(safe_job["trace_id"]),
                last_state=safe_job,
            )
        self.code = code
        self.progress = progress or SmokeProgress()

    @property
    def last_state(self) -> dict[str, object] | None:
        return self.progress.last_state
```

Import `deepcopy` from `copy` and `replace` from `dataclasses`.

- [ ] **Step 5: Make JSON errors endpoint-specific**

Replace `_json_response` with:

```python
def _json_response(response: httpx.Response, *, error_code: str) -> object:
    try:
        return response.json()
    except ValueError as error:
        raise SmokeFailure(error_code) from error
```

Call it with `malformed_intake_response`, `malformed_webhook_response`, or
`malformed_job_response` at the corresponding boundary. When the caller has an
accepted `SmokeProgress`, catch the decoding failure and re-raise the same code
with that progress; never attach the rejected response.

- [ ] **Step 6: Return validated progress from acceptance and pass it through polling**

Change `_accepted_identity` to return `SmokeProgress`. Validate and normalize
both UUIDs before constructing it:

```python
def _accepted_identity(
    response: httpx.Response,
    *,
    error_prefix: str,
) -> SmokeProgress:
    if response.status_code != 202:
        if response.status_code == 401:
            raise SmokeFailure(f"{error_prefix}_unauthorized")
        raise SmokeFailure(f"{error_prefix}_http_{response.status_code}")
    payload = _json_response(
        response,
        error_code=f"malformed_{error_prefix}_response",
    )
    if not isinstance(payload, dict) or set(payload) != {"job_id", "status"}:
        raise SmokeFailure(f"malformed_{error_prefix}_response")
    if payload.get("status") != "queued" or not isinstance(payload.get("job_id"), str):
        raise SmokeFailure(f"malformed_{error_prefix}_response")
    trace_id = response.headers.get("X-Trace-ID")
    if trace_id is None:
        raise SmokeFailure("trace_id_missing")
    try:
        job_id = str(UUID(payload["job_id"]))
        normalized_trace = str(UUID(trace_id))
    except ValueError as error:
        raise SmokeFailure(f"malformed_{error_prefix}_response") from error
    return SmokeProgress.accepted(job_id, normalized_trace)
```

Change `_poll_job` to accept `progress` instead of separate IDs. Every error
after acceptance must raise `SmokeFailure(code, progress=progress)`. After a
job response passes `_safe_job_response`, update `progress = progress.with_job(job)`
before identity, scenario, status, or timeout checks.

Apply scenario checks in this order:

```python
status = job["status"]
scenario_key = job["scenario_key"]
if scenario_key is not None and scenario_key != expected_scenario:
    raise SmokeFailure("scenario_mismatch", progress=progress)
if status not in {"failed", "failed_uncertain"} and scenario_key is None:
    raise SmokeFailure("scenario_mismatch", progress=progress)
if status in {"failed", "failed_uncertain"}:
    raise SmokeFailure(f"job_{status}", progress=progress)
if status in {"succeeded", "awaiting_approval"}:
    return job
```

- [ ] **Step 7: Remove the duplicate body polling loop**

In `run_smoke`, keep request construction and POST error mapping, then use the
same helpers as webhook:

```python
accepted_progress = _accepted_identity(accepted, error_prefix="intake")
return _poll_job(
    http_client=http_client,
    base_url=base_url,
    api_key=api_key,
    progress=accepted_progress,
    expected_scenario=expected_scenario,
    timeout_seconds=timeout_seconds,
    poll_interval_seconds=poll_interval_seconds,
    clock=clock,
    sleep=sleep,
)
```

Update `run_webhook_smoke` and `run_webhook_idempotency` to use
`progress.job_id` and `progress.trace_id`. When duplicate/conflict observations
are established, use
`replace(progress, duplicate_reused=..., conflict_rejected=...)` before
polling or raising.

- [ ] **Step 8: Build failure reports from progress**

Update `_failure_report` so accepted identity remains visible even without a
validated poll response:

```python
progress = error.progress
job = dict(progress.last_state or {})
if progress.job_id is not None:
    job.setdefault("job_id", progress.job_id)
if progress.trace_id is not None:
    job.setdefault("trace_id", progress.trace_id)
return {
    "result": "FAIL",
    "demo_case": arguments.demo_case,
    "profile": arguments.profile,
    "transport": selected_transport,
    "endpoint": endpoint,
    "input_kind": input_kind,
    "acceptance_status": progress.acceptance_status,
    "duplicate_reused": progress.duplicate_reused,
    "conflict_rejected": progress.conflict_rejected,
    "checks": [],
    "job": job,
    "error_code": error.code,
}
```

Expectation failures raised after a validated terminal job must construct
progress from that safe job and include established duplicate/conflict flags.

- [ ] **Step 9: Run the smoke test suite**

Run:

```powershell
pytest tests/test_live_provider_smoke.py -q
```

Expected: all smoke tests pass; body and webhook post-acceptance failures retain
safe identity and emit endpoint-specific codes.

---

### Task 3: Render typed result references safely

**Files:**
- Edit: `scripts/live_provider_smoke.py`
- Test: `tests/test_live_provider_smoke.py`

- [ ] **Step 1: Call the `unit-test-writer` skill for human and JSON output tests**

Require empty, populated, and unobserved reference states. Include sentinel
artifact payload text and assert it never appears in either renderer.

- [ ] **Step 2: Add failing renderer assertions**

Extend `test_human_renderer_shows_safe_lifecycle_and_path_without_source`:

```python
report["job"]["result_references"] = [
    {"kind": "case", "id": str(CASE_ID)},
    {"kind": "reply_draft", "id": str(UUID(int=5))},
]

rendered = render_human(report)

assert "Result references:" in rendered
assert f"kind=case id={CASE_ID}" in rendered
assert f"kind=reply_draft id={UUID(int=5)}" in rendered
assert "SOURCE_SECRET" not in rendered
```

Add separate assertions that an empty list renders `NONE`, a missing field
renders `NOT OBSERVED`, and `--json` contains only `kind`/`id` reference data.

- [ ] **Step 3: Run the renderer tests and confirm they fail**

Run:

```powershell
pytest tests/test_live_provider_smoke.py -q
```

Expected: failure because `render_human` has no result-reference section.

- [ ] **Step 4: Add the result-reference renderer**

Add:

```python
def _result_references_text(value: object) -> list[str]:
    if value is None:
        return ["  NOT OBSERVED"]
    if not isinstance(value, list) or not value:
        return ["  NONE"]
    return [
        f"  kind={item['kind']} id={item['id']}"
        for item in value
        if isinstance(item, Mapping)
    ]
```

In `render_human`, append `Result references:` after approval/error lifecycle
fields and extend with `_result_references_text(safe_job.get("result_references"))`.
Do not retrieve artifacts or render any other keys.

- [ ] **Step 5: Run all smoke tests**

Run:

```powershell
pytest tests/test_live_provider_smoke.py -q
```

Expected: all tests pass and no credential/source sentinel appears in output.

---

### Task 4: Reconcile the live-provider runbook

**Files:**
- Edit: `docs/runbooks/live-provider-demo.md`
- Test: `tests/test_observability.py`

- [ ] **Step 1: Call the `unit-test-writer` skill for the documentation contract**

Require an assertion for `001_initial_schema (head)`, rejection of operational
references to `002_job_artifacts`, the result-reference output label, and the
three new troubleshooting concepts from the approved design.

- [ ] **Step 2: Add failing documentation assertions**

Extend `test_product_docs_describe_safe_live_demo_entrypoints`:

```python
assert "001_initial_schema (head)" in text
assert "002_job_artifacts" not in text
assert "Result references:" in text
for phrase in (
    "unsafe_job_response",
    "schema drift",
    "scenario_key",
    "acceptance evidence",
    "validated poll",
):
    assert phrase.lower() in text.lower(), phrase
```

- [ ] **Step 3: Run the documentation test and confirm it fails**

Run:

```powershell
pytest tests/test_observability.py::test_product_docs_describe_safe_live_demo_entrypoints -q
```

Expected: failure because the runbook still names `002_job_artifacts` and does
not document the reconciled output/troubleshooting contract.

- [ ] **Step 4: Update the migration and smoke guidance**

In `docs/runbooks/live-provider-demo.md`:

1. Replace the migration-head paragraph with:

```markdown
The current and head revisions must report `001_initial_schema`. The complete
release schema, including `job_artifacts`, is created by this single baseline
revision. API and worker startup do not apply migrations.
```

2. Replace the clean-release evidence row so it expects
   `001_initial_schema (head)`.
3. Add the exact `Result references:` output label to the evidence section and
   explain that it contains only typed kind/UUID pairs.
4. Add troubleshooting text with these semantics:
   - `unsafe_job_response` after `HTTP accepted: 202` means the API returned a
     field outside the smoke's closed safe schema;
   - a failed job may show `Scenario: NOT OBSERVED` when persisted profile
     enrichment is unavailable, while its stable job error remains valid;
   - acceptance evidence comes from the validated `202` response, whereas
     lifecycle fields come only from the latest fully validated poll.

Do not edit historical plans or earlier approved design documents.

- [ ] **Step 5: Run the documentation contract**

Run:

```powershell
pytest tests/test_observability.py::test_product_docs_describe_safe_live_demo_entrypoints -q
```

Expected: PASS.

---

### Task 5: Verify the reconciled boundary

**Files:**
- Verify: `scripts/live_provider_smoke.py`
- Verify: `tests/test_live_provider_smoke.py`
- Verify: `tests/test_jobs_api.py`
- Verify: `tests/test_observability.py`
- Verify: `docs/runbooks/live-provider-demo.md`

- [ ] **Step 1: Run the focused contract suite**

Run:

```powershell
pytest tests/test_live_provider_smoke.py tests/test_jobs_api.py `
  tests/test_observability.py -q
```

Expected: all focused tests pass without a live provider or network.

- [ ] **Step 2: Reproduce the original payload through the parser**

Run:

```powershell
@'
from datetime import datetime, timezone
from uuid import uuid4

from app.agent import ResultReference
from app.domain.jobs import JobRead
from scripts.live_provider_smoke import _safe_job_response

payload = JobRead(
    job_id=uuid4(),
    trace_id=uuid4(),
    scenario_key="freight_broker",
    status="succeeded",
    attempt_count=1,
    result_references=(ResultReference(kind="case", id=uuid4()),),
    created_at=datetime.now(timezone.utc),
).model_dump(mode="json")
safe = _safe_job_response(payload)
assert safe["result_references"][0]["kind"] == "case"
print("JOB_READ_SMOKE_CONTRACT: PASS")
'@ | python -
```

Expected: `JOB_READ_SMOKE_CONTRACT: PASS`.

- [ ] **Step 3: Run the opt-in live smoke when the local credential is available**

Check only whether the variable is present; never print it:

```powershell
if ([string]::IsNullOrWhiteSpace($env:INTEGRA_DEMO_API_KEY)) {
  Write-Output "LIVE_SMOKE: SKIPPED (INTEGRA_DEMO_API_KEY unavailable)"
} else {
  python scripts/live_provider_smoke.py `
    --profile freight-broker `
    --base-url http://localhost:8000
}
```

Expected with a running configured stack: `LIVE INTAKE SMOKE: PASS`,
`HTTP accepted: 202`, a job ID, a trace ID, and `Result references:` followed
by `NONE` or validated kind/UUID entries. A skip is not live integration
evidence and must be reported as such.

---

### Task 6: Run final code quality checks

**Files:**
- Verify: `scripts/live_provider_smoke.py`
- Verify: `tests/test_live_provider_smoke.py`
- Verify: `tests/test_jobs_api.py`
- Verify: `tests/test_observability.py`
- Verify: `docs/runbooks/live-provider-demo.md`

- [ ] **Step 1: Run the complete provider-free test suite**

Run:

```powershell
pytest -q
```

Expected: all collected unit, API, regression, and deterministic eval tests
pass. PostgreSQL integration skips are not database evidence and must be
reported with the final test counts.

- [ ] **Step 2: Compile all checked Python sources**

Run:

```powershell
python -m compileall -q app scripts evals tests
```

Expected: exit code `0` with no syntax errors.

- [ ] **Step 3: Check the final diff for whitespace errors**

Run:

```powershell
git diff --check
```

Expected: exit code `0` with no output.

- [ ] **Step 4: Record verification evidence without overstating skipped boundaries**

Report the focused-test and full-suite counts, `compileall` result,
`git diff --check` result, and the live-smoke outcome. If the live command was
skipped because the credential or stack was unavailable, state that explicitly
and do not claim live integration evidence.
