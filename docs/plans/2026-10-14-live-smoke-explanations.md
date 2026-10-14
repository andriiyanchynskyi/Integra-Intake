# Implementation plan: Live smoke explanations

> **For agents:** Use the executing-plans skill to implement this plan task by task. Steps use checkbox syntax (`- [ ]`) for tracking.

**Purpose:** Add source-free purpose, expectation, and observed-outcome explanations to every live-smoke mode, enforce the missing-contact business contract for `webhook-body`, and link the demo runbook from the README introduction.

**Architecture:** Keep explanations inside the smoke report contract so human and JSON output use the same data. Select purpose and expectation from closed mode metadata, derive observations only from the validated safe job projection, and leave the renderer responsible only for layout. Preserve the default and custom modes as connectivity checks while making the named `webhook-body` case a strict semantic check.

**Key files/stack:** Python 3.12, argparse, httpx, Pydantic v2, pytest, `scripts/live_provider_smoke.py`, `tests/test_live_provider_smoke.py`, `tests/test_observability.py`, Markdown documentation.

---

## File map

- `scripts/live_provider_smoke.py` — own closed explanation metadata, strict named-case assertions, safe observed-outcome projection, report construction, and human rendering.
- `tests/test_live_provider_smoke.py` — prove explanation coverage, strict `webhook-body` behavior, output safety, and JSON/human consistency without network access.
- `tests/test_observability.py` — enforce the README/runbook documentation contract.
- `README.md` — expose the live-provider demo runbook near the repository introduction.
- `docs/runbooks/live-provider-demo.md` — document the new explanation fields and the strict `webhook-body` expectation.

## Task 1: Add closed explanation metadata to every smoke mode

**Files:**
- Edit: `scripts/live_provider_smoke.py`
- Test: `tests/test_live_provider_smoke.py`

- [ ] **Step 1: Write failing metadata and report tests**

Add imports only if the test module does not already access the symbols through `smoke`. Add these tests near the existing named-case report tests:

```python
def test_every_named_demo_case_has_bounded_explanation_metadata() -> None:
    assert set(smoke.DEMO_CASE_EXPLANATIONS) == set(smoke.DemoCase)
    for explanation in smoke.DEMO_CASE_EXPLANATIONS.values():
        assert explanation.purpose.strip()
        assert explanation.expected_outcome.strip()
        assert len(explanation.purpose) <= 300
        assert len(explanation.expected_outcome) <= 500


def test_named_demo_report_contains_source_free_explanations() -> None:
    transport = SequenceTransport([_accepted(), _job("succeeded")])
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        report = run_demo_case(
            case="body-complete",
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

    assert report["purpose"] == smoke.DEMO_CASE_EXPLANATIONS[
        smoke.DemoCase.BODY_COMPLETE
    ].purpose
    assert report["expected_outcome"] == smoke.DEMO_CASE_EXPLANATIONS[
        smoke.DemoCase.BODY_COMPLETE
    ].expected_outcome
    assert "job status=succeeded" in report["observed_outcome"]
    assert "SOURCE_SECRET" not in json.dumps(report)


def test_default_and_custom_modes_describe_connectivity_not_fixed_semantics(
    tmp_path,
    monkeypatch,
) -> None:
    job = smoke._safe_job_response(_job_payload("succeeded"))
    monkeypatch.setattr(smoke, "run_smoke", lambda **_: job)

    default_report = smoke.run_live_report(
        profile="freight-broker",
        base_url="http://testserver",
        api_key=RAW_TENANT_KEY,
    )

    input_file = tmp_path / "custom.json"
    input_file.write_text(
        json.dumps(
            {
                "transport": "intake",
                "channel": "email",
                "subject": "Synthetic custom request",
                "body": "Synthetic custom body",
            }
        ),
        encoding="utf-8",
    )
    custom_report = smoke.run_live_report(
        profile="freight-broker",
        base_url="http://testserver",
        api_key=RAW_TENANT_KEY,
        input_file=input_file,
    )

    assert "connectivity" in default_report["purpose"].lower()
    assert "safe terminal" in default_report["expected_outcome"].lower()
    assert "caller-supplied synthetic input" in custom_report["purpose"]
    assert "fixed business semantics" in custom_report["expected_outcome"]
```

- [ ] **Step 2: Run the new tests and confirm the missing contract**

Run:

```powershell
pytest tests/test_live_provider_smoke.py `
  -k "every_named_demo_case_has_bounded_explanation_metadata or named_demo_report_contains_source_free_explanations or default_and_custom_modes_describe_connectivity_not_fixed_semantics" -q
```

Expected: FAIL because `DEMO_CASE_EXPLANATIONS` and the three report fields do not exist.

- [ ] **Step 3: Define closed mode metadata**

Add this model after `DemoCase` and before `DemoCaseSpec`:

```python
@dataclass(frozen=True, slots=True)
class SmokeExplanation:
    purpose: str
    expected_outcome: str
```

Add these constants after `DemoCaseSpec`:

```python
DEFAULT_EXPLANATION = SmokeExplanation(
    purpose=(
        "Verify live intake connectivity through the API, job polling, worker, "
        "configured provider, and safe terminal handling."
    ),
    expected_outcome=(
        "The request is accepted and the job reaches a safe terminal status. "
        "No fixed business classification is required."
    ),
)
CUSTOM_EXPLANATION = SmokeExplanation(
    purpose=(
        "Verify live connectivity for caller-supplied synthetic input through "
        "the selected transport."
    ),
    expected_outcome=(
        "The job reaches a safe terminal status without asserting fixed business "
        "semantics."
    ),
)
DEMO_CASE_EXPLANATIONS: dict[DemoCase, SmokeExplanation] = {
    DemoCase.BODY_COMPLETE: SmokeExplanation(
        purpose="Verify complete body intake and expose its governed execution path.",
        expected_outcome=(
            "The job reaches a safe terminal status; a pending approval includes "
            "an approval ID and a needs_approval policy step."
        ),
    ),
    DemoCase.BODY_INCOMPLETE: SmokeExplanation(
        purpose="Verify policy handling for a load request without contact details.",
        expected_outcome=(
            "The job succeeds with awaiting_input, reports only contact as missing, "
            "and commits no side effect."
        ),
    ),
    DemoCase.WEBHOOK_BODY: SmokeExplanation(
        purpose=(
            "Verify signed webhook acceptance and policy handling for a load "
            "request without contact details."
        ),
        expected_outcome=(
            "The job succeeds with awaiting_input and missing_required_fields, "
            "reports only contact as missing, creates no approval, and commits no "
            "side effect."
        ),
    ),
    DemoCase.WEBHOOK_DOCUMENT_TXT: SmokeExplanation(
        purpose=(
            "Verify signed text attachment transport, document normalization, "
            "worker execution, and the configured provider boundary."
        ),
        expected_outcome=(
            "The attachment is accepted and the job reaches a safe terminal status; "
            "no fixed semantic classification is required."
        ),
    ),
    DemoCase.WEBHOOK_DOCUMENT_PDF: SmokeExplanation(
        purpose=(
            "Verify signed PDF attachment transport, document normalization, "
            "worker execution, and the configured provider boundary."
        ),
        expected_outcome=(
            "The attachment is accepted and the job reaches a safe terminal status; "
            "no fixed semantic classification is required."
        ),
    ),
    DemoCase.WEBHOOK_DOCUMENT_MALFORMED: SmokeExplanation(
        purpose="Verify deterministic preflight for an unreadable PDF attachment.",
        expected_outcome=(
            "The job succeeds with awaiting_input and document_unreadable before "
            "provider, tool, or approval execution."
        ),
    ),
    DemoCase.WEBHOOK_DUPLICATE: SmokeExplanation(
        purpose="Verify idempotent handling of a repeated signed webhook delivery.",
        expected_outcome=(
            "The second delivery reuses the original job ID and root trace ID."
        ),
    ),
    DemoCase.WEBHOOK_CONFLICT: SmokeExplanation(
        purpose=(
            "Verify conflict handling when one provider delivery ID is reused with "
            "a changed payload."
        ),
        expected_outcome=(
            "The changed delivery is rejected with conflict and the original job "
            "remains the authoritative result."
        ),
    ),
}
```

- [ ] **Step 4: Add one safe observed-outcome projector**

Add this function immediately before `_report`:

```python
def _observed_outcome(
    *,
    acceptance_status: object,
    job: Mapping[str, object],
    duplicate_reused: bool | None,
    conflict_rejected: bool | None,
    error_code: str | None,
) -> str:
    observations: list[str] = []
    if acceptance_status is not None:
        observations.append(f"HTTP acceptance={acceptance_status}")
    if job.get("status") is not None:
        observations.append(f"job status={job['status']}")
    if job.get("routing_status") is not None:
        observations.append(f"routing status={job['routing_status']}")
    if job.get("routing_reason") is not None:
        observations.append(f"routing reason={job['routing_reason']}")
    missing = job.get("missing_required_fields")
    if isinstance(missing, list):
        observations.append(
            "missing fields=" + (",".join(str(item) for item in missing) or "none")
        )
    if "approval" in job:
        approval = job.get("approval")
        observations.append(
            "approval="
            + (
                str(approval.get("status"))
                if isinstance(approval, Mapping)
                else "none"
            )
        )
    path = job.get("execution_path")
    if isinstance(path, list):
        committed = any(
            item.get("side_effect_committed") is True
            for item in path
            if isinstance(item, Mapping)
        )
        observations.append(f"committed side effect={'yes' if committed else 'no'}")
    if duplicate_reused is not None:
        observations.append(f"duplicate reused={'yes' if duplicate_reused else 'no'}")
    if conflict_rejected is not None:
        observations.append(f"conflict rejected={'yes' if conflict_rejected else 'no'}")
    if error_code is not None:
        observations.append(f"smoke error={error_code}")
    if not observations:
        return "No validated outcome was observed."
    return "; ".join(observations) + "."
```

Only pass this function data that came from `_safe_job_response`, `SmokeProgress`, enum choices, or the smoke's own closed booleans. Do not add source, provider response, exception, or arbitrary request values.

- [ ] **Step 5: Extend report construction with the three explanation fields**

Change `_report` to accept `explanation: SmokeExplanation` and calculate the observation from its existing safe arguments:

```python
def _report(
    *,
    demo_case: str | None,
    profile: str,
    transport: SmokeTransport,
    endpoint: str,
    input_kind: str,
    job: Mapping[str, object],
    checks: list[dict[str, object]],
    explanation: SmokeExplanation,
    duplicate_reused: bool | None = None,
    conflict_rejected: bool | None = None,
) -> dict[str, object]:
    return {
        "result": "PASS" if all(check["passed"] for check in checks) else "FAIL",
        "demo_case": demo_case,
        "profile": profile,
        "transport": transport.value,
        "endpoint": endpoint,
        "input_kind": input_kind,
        "purpose": explanation.purpose,
        "expected_outcome": explanation.expected_outcome,
        "observed_outcome": _observed_outcome(
            acceptance_status=202,
            job=job,
            duplicate_reused=duplicate_reused,
            conflict_rejected=conflict_rejected,
            error_code=None,
        ),
        "acceptance_status": 202,
        "duplicate_reused": duplicate_reused,
        "conflict_rejected": conflict_rejected,
        "checks": checks,
        "job": dict(job),
        "error_code": None,
    }
```

At the `_report` call in `run_demo_case`, pass:

```python
explanation=DEMO_CASE_EXPLANATIONS[selected],
```

At the `_report` call in `run_custom_input`, pass:

```python
explanation=_with_expected_path(CUSTOM_EXPLANATION, expect_path),
```

Add this helper near the constants:

```python
def _with_expected_path(
    explanation: SmokeExplanation,
    expect_path: str | None,
) -> SmokeExplanation:
    if expect_path is None:
        return explanation
    return replace(
        explanation,
        expected_outcome=(
            f"{explanation.expected_outcome} The execution path must include "
            f"{expect_path}."
        ),
    )
```

Change `_report_for_single_job` to pass the default explanation:

```python
return _report(
    demo_case=None,
    profile=profile,
    transport=transport,
    endpoint=endpoint,
    input_kind=input_kind,
    job=job,
    checks=checks,
    explanation=_with_expected_path(DEFAULT_EXPLANATION, expect_path),
)
```

- [ ] **Step 6: Run the metadata and report tests**

Run:

```powershell
pytest tests/test_live_provider_smoke.py `
  -k "every_named_demo_case_has_bounded_explanation_metadata or named_demo_report_contains_source_free_explanations or default_and_custom_modes_describe_connectivity_not_fixed_semantics" -q
```

Expected: 3 passed.

## Task 2: Make `webhook-body` assert missing-contact freight behavior

**Files:**
- Edit: `scripts/live_provider_smoke.py`
- Test: `tests/test_live_provider_smoke.py`

- [ ] **Step 1: Add a reusable strict job fixture and failing behavior tests**

Add this helper after `_job_payload`:

```python
def _missing_contact_job_payload() -> dict[str, object]:
    payload = _job_payload("succeeded")
    payload.update(
        routing_status="awaiting_input",
        routing_reason="missing_required_fields",
        missing_required_fields=["contact"],
        approval=None,
        execution_path=[
            {
                "step": 1,
                "action_key": "create_case",
                "action_known": True,
                "policy_decision": "deny",
                "routing_status": "awaiting_input",
                "policy_reason": "missing_required_fields",
                "missing_required_fields": ["contact"],
                "tool_outcome": None,
                "side_effect_committed": False,
            }
        ],
    )
    return payload
```

Add these tests near the other named-case tests:

```python
def test_webhook_body_requires_exact_missing_contact_business_outcome() -> None:
    transport = SequenceTransport(
        [_accepted(), httpx.Response(200, json=_missing_contact_job_payload())]
    )
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        report = run_demo_case(
            case="webhook-body",
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

    assert report["result"] == "PASS"
    assert {check["name"] for check in report["checks"]} >= {
        "status",
        "routing_status",
        "routing_reason",
        "missing_required_fields",
        "approval",
        "no_side_effect",
    }
    sent = json.loads(transport.requests[0].content)
    assert "contact:" not in sent["body"].lower()
    assert "origin:" in sent["body"].lower()
    assert "destination:" in sent["body"].lower()


def test_webhook_body_rejects_unrelated_safe_terminal_outcome() -> None:
    transport = SequenceTransport([_accepted(), _job("succeeded")])
    client = httpx.Client(transport=httpx.MockTransport(transport))
    try:
        with pytest.raises(SmokeFailure, match="expectation_failed"):
            run_demo_case(
                case="webhook-body",
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


@pytest.mark.parametrize(
    ("field", "value", "failed_check"),
    [
        (
            "approval",
            {"approval_id": str(UUID(int=10)), "status": "pending"},
            "approval",
        ),
        (
            "execution_path",
            [
                {
                    "step": 1,
                    "action_key": "create_case",
                    "action_known": True,
                    "policy_decision": "deny",
                    "routing_status": "awaiting_input",
                    "policy_reason": "missing_required_fields",
                    "missing_required_fields": ["contact"],
                    "tool_outcome": "executed",
                    "side_effect_committed": True,
                }
            ],
            "no_side_effect",
        ),
    ],
)
def test_webhook_body_requires_no_approval_or_committed_side_effect(
    field: str,
    value: object,
    failed_check: str,
) -> None:
    job = _missing_contact_job_payload()
    job[field] = value

    checks = smoke._case_checks(smoke.DemoCase.WEBHOOK_BODY, job)

    assert next(check for check in checks if check["name"] == failed_check)[
        "passed"
    ] is False
```

- [ ] **Step 2: Run the strict-case tests and confirm the permissive behavior**

Run:

```powershell
pytest tests/test_live_provider_smoke.py `
  -k "webhook_body_requires_exact or webhook_body_rejects_unrelated or webhook_body_requires_no_approval" -q
```

Expected: FAIL because the case still sends a vague body and only checks terminal status.

- [ ] **Step 3: Replace the vague webhook fixture with a clear incomplete load**

Change the `DemoCase.WEBHOOK_BODY` entry in `DEMO_CASE_SPECS` to:

```python
DemoCase.WEBHOOK_BODY: DemoCaseSpec(
    transport=SmokeTransport.WEBHOOK,
    payload={
        "subject": "Synthetic incomplete freight webhook",
        "body": (
            "Origin: Chicago; destination: Detroit; equipment: dry_van; "
            "pickup_window: 2026-10-01T09:00:00Z; commodity: appliances"
        ),
    },
),
```

- [ ] **Step 4: Add exact policy and safety assertions**

Add this branch to `_case_checks` immediately after `BODY_INCOMPLETE`:

```python
elif case is DemoCase.WEBHOOK_BODY:
    checks.extend(
        [
            _check("status", "succeeded", job.get("status")),
            _check("routing_status", "awaiting_input", job.get("routing_status")),
            _check(
                "routing_reason",
                "missing_required_fields",
                job.get("routing_reason"),
            ),
            _check(
                "missing_required_fields",
                ["contact"],
                job.get("missing_required_fields"),
            ),
            _check("approval", None, job.get("approval")),
            _check(
                "no_side_effect",
                True,
                not any(
                    item.get("side_effect_committed")
                    for item in job.get("execution_path", [])
                    if isinstance(item, dict)
                ),
            ),
        ]
    )
```

Do not add an `invalid_fields` check. The public job projection exposes invalid required names through `missing_required_fields` with `routing_reason=invalid_field_values`; this fixture tests an absent `contact` field.

- [ ] **Step 5: Run the strict-case tests**

Run:

```powershell
pytest tests/test_live_provider_smoke.py `
  -k "webhook_body_requires_exact or webhook_body_rejects_unrelated or webhook_body_requires_no_approval" -q
```

Expected: 4 passed, including both parameterized safety cases.

## Task 3: Render explanations consistently for success and failure

**Files:**
- Edit: `scripts/live_provider_smoke.py`
- Test: `tests/test_live_provider_smoke.py`

- [ ] **Step 1: Add failing human, JSON, and failure-report tests**

Extend `test_human_renderer_shows_safe_lifecycle_and_path_without_source` by adding the three report fields:

```python
"purpose": "Verify signed text document intake.",
"expected_outcome": "The job reaches a safe terminal status.",
"observed_outcome": (
    "HTTP acceptance=202; job status=succeeded; routing status=ready; "
    "routing reason=action_allowed; missing fields=none; approval=none; "
    "committed side effect=yes."
),
```

Add these assertions after rendering:

```python
assert rendered.index("Purpose:") < rendered.index("Job ID:")
assert "Purpose:          Verify signed text document intake." in rendered
assert "Expected:         The job reaches a safe terminal status." in rendered
assert "Observed:         HTTP acceptance=202; job status=succeeded" in rendered
```

Add these standalone tests:

```python
def test_failure_report_keeps_mode_explanation_and_safe_progress() -> None:
    arguments = argparse.Namespace(
        transport=None,
        attachment=None,
        input_file=None,
        demo_case="webhook-body",
        profile="freight-broker",
        expect_path=None,
    )
    failure = SmokeFailure(
        "expectation_failed",
        progress=smoke.SmokeProgress.from_job(
            smoke._safe_job_response(_job_payload("succeeded"))
        ),
    )

    report = smoke._failure_report(arguments, failure)

    assert report["purpose"] == smoke.DEMO_CASE_EXPLANATIONS[
        smoke.DemoCase.WEBHOOK_BODY
    ].purpose
    assert "missing contact" in report["expected_outcome"].lower()
    assert "smoke error=expectation_failed" in report["observed_outcome"]
    assert "job status=succeeded" in report["observed_outcome"]


def test_json_cli_preserves_explanation_fields(monkeypatch, capsys) -> None:
    report = {
        "result": "PASS",
        "purpose": "Verify synthetic connectivity.",
        "expected_outcome": "The job reaches a safe terminal status.",
        "observed_outcome": "HTTP acceptance=202; job status=succeeded.",
        "job": _job_payload("succeeded"),
    }
    monkeypatch.setenv("INTEGRA_DEMO_API_KEY", RAW_TENANT_KEY)
    monkeypatch.setattr(smoke, "run_live_report", lambda **_: report)

    assert smoke.main(["--json"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["purpose"] == report["purpose"]
    assert output["expected_outcome"] == report["expected_outcome"]
    assert output["observed_outcome"] == report["observed_outcome"]
```

- [ ] **Step 2: Run the output tests and confirm explanations are absent**

Run:

```powershell
pytest tests/test_live_provider_smoke.py `
  -k "human_renderer_shows_safe_lifecycle or failure_report_keeps_mode_explanation or json_cli_preserves_explanation" -q
```

Expected: FAIL because the renderer and failure report do not expose explanation fields.

- [ ] **Step 3: Print the three explanations before lifecycle details**

Insert these lines in `render_human` after `Input` and before `HTTP accepted`:

```python
f"Purpose:          {_display(report.get('purpose'))}",
f"Expected:         {_display(report.get('expected_outcome'))}",
f"Observed:         {_display(report.get('observed_outcome'))}",
```

Do not render unknown report keys. Keep source input and provider payloads outside the report.

- [ ] **Step 4: Make failure reports use the same closed metadata and observation formatter**

Add this selector before `_failure_report`:

```python
def _failure_explanation(arguments: argparse.Namespace) -> SmokeExplanation:
    expect_path = getattr(arguments, "expect_path", None)
    if arguments.demo_case is not None:
        try:
            selected = DemoCase(arguments.demo_case)
        except ValueError:
            return DEFAULT_EXPLANATION
        return DEMO_CASE_EXPLANATIONS[selected]
    if arguments.input_file is not None:
        return _with_expected_path(CUSTOM_EXPLANATION, expect_path)
    return _with_expected_path(DEFAULT_EXPLANATION, expect_path)
```

Resolve the selected transport at the beginning of `_failure_report` so named
webhook failures do not fall back to `intake`:

```python
if arguments.demo_case is not None:
    try:
        selected_case = DemoCase(arguments.demo_case)
    except ValueError:
        selected_case = None
    case_transport = (
        DEMO_CASE_SPECS[selected_case].transport.value
        if selected_case is not None
        else None
    )
else:
    selected_case = None
    case_transport = None
selected_transport = arguments.transport or case_transport or (
    SmokeTransport.WEBHOOK.value
    if arguments.attachment
    else SmokeTransport.INTAKE.value
)
```

When `selected_case` is present, derive `input_kind` with the same case groups as
`run_demo_case`; otherwise keep the current custom/attachment/transport logic:

```python
if selected_case is DemoCase.WEBHOOK_DOCUMENT_TXT:
    input_kind = "text attachment"
elif selected_case in {
    DemoCase.WEBHOOK_DOCUMENT_PDF,
    DemoCase.WEBHOOK_DOCUMENT_MALFORMED,
}:
    input_kind = "PDF attachment"
elif selected_case in {
    DemoCase.WEBHOOK_BODY,
    DemoCase.WEBHOOK_DUPLICATE,
}:
    input_kind = "webhook body"
elif selected_case is DemoCase.WEBHOOK_CONFLICT:
    input_kind = "webhook conflict"
elif selected_case in {DemoCase.BODY_COMPLETE, DemoCase.BODY_INCOMPLETE}:
    input_kind = "body"
else:
    input_kind = (
        "custom input"
        if arguments.input_file is not None
        else "attachment"
        if arguments.attachment is not None
        else "webhook body"
        if selected_transport == SmokeTransport.WEBHOOK.value
        else "body"
    )
```

Construct the failure report in a local variable and add the explanation fields before returning it:

```python
explanation = _failure_explanation(arguments)
report = {
    "result": "FAIL",
    "demo_case": arguments.demo_case,
    "profile": arguments.profile,
    "transport": selected_transport,
    "endpoint": endpoint,
    "input_kind": input_kind,
    "purpose": explanation.purpose,
    "expected_outcome": explanation.expected_outcome,
    "acceptance_status": progress.acceptance_status,
    "duplicate_reused": progress.duplicate_reused,
    "conflict_rejected": progress.conflict_rejected,
    "checks": [],
    "job": job,
    "error_code": error.code,
}
report["observed_outcome"] = _observed_outcome(
    acceptance_status=progress.acceptance_status,
    job=job,
    duplicate_reused=progress.duplicate_reused,
    conflict_rejected=progress.conflict_rejected,
    error_code=error.code,
)
return report
```

- [ ] **Step 5: Run all live-smoke tests**

Run:

```powershell
pytest tests/test_live_provider_smoke.py -q
```

Expected: all tests pass with no network access.

## Task 4: Link the runbook and document the stricter evidence contract

**Files:**
- Edit: `README.md`
- Edit: `docs/runbooks/live-provider-demo.md`
- Test: `tests/test_observability.py`

- [ ] **Step 1: Strengthen the documentation contract test**

In `test_product_docs_describe_safe_live_demo_entrypoints`, add these assertions after loading `readme` and `text`:

```python
runbook_link = "[Live provider demo runbook](docs/runbooks/live-provider-demo.md)"
assert runbook_link in readme
assert readme.index(runbook_link) < readme.index("## Capabilities")
for explanation_label in ("Purpose:", "Expected:", "Observed:"):
    assert explanation_label in text
for strict_webhook_phrase in (
    'routing_status=awaiting_input',
    'routing_reason=missing_required_fields',
    'missing_required_fields=["contact"]',
    "no approval",
    "no committed side effect",
):
    assert strict_webhook_phrase in text
```

- [ ] **Step 2: Run the documentation contract and confirm it fails**

Run:

```powershell
pytest tests/test_observability.py::test_product_docs_describe_safe_live_demo_entrypoints -q
```

Expected: FAIL because the README link is not near the introduction and the runbook does not describe the new output contract or strict webhook assertions.

- [ ] **Step 3: Add the README entry point**

Insert this paragraph after the second introductory paragraph and before `## Capabilities`:

```markdown
Follow the [Live provider demo runbook](docs/runbooks/live-provider-demo.md) to
start the local stack, run source-free live checks, and record the evidence.
```

- [ ] **Step 4: Document explanation fields in the live-check introduction**

Replace the paragraph after the first smoke command in Section 8 with:

```markdown
The command submits synthetic body intake through the real API, polls
`GET /v1/jobs/{job_id}`, and prints a human-readable source-free report. Use
`--json` for the same allowlisted report as one machine-readable object. Every
mode prints `Purpose:`, `Expected:`, and `Observed:` before lifecycle details.
These lines state what the mode checks, which assertions define PASS, and what
the validated job projection showed. `succeeded` and `awaiting_approval` are
valid for connectivity-only modes; named cases can impose stricter assertions.
Provider output can vary, so the report shows the path actually observed and
prints `NOT OBSERVED` when a nullable policy or tool field was not reached.
```

- [ ] **Step 5: Document the strict `webhook-body` behavior**

Replace the explanatory paragraph under the `webhook-body` command with:

```markdown
The command signs the exact JSON bytes and sends them to
`POST /v1/inbound/email/webhook`. Its synthetic load request omits `contact`.
PASS requires `status=succeeded`, `routing_status=awaiting_input`,
`routing_reason=missing_required_fields`,
`missing_required_fields=["contact"]`, no approval, and no committed side
effect. A different safe terminal outcome proves connectivity but fails this
named business scenario; use the default smoke when only connectivity matters.
```

- [ ] **Step 6: Run focused documentation and smoke tests**

Run:

```powershell
pytest tests/test_observability.py::test_product_docs_describe_safe_live_demo_entrypoints `
  tests/test_live_provider_smoke.py -q
```

Expected: all selected tests pass.

## Task 5: Run final quality checks

**Files:**
- Verify: `scripts/live_provider_smoke.py`
- Verify: `tests/test_live_provider_smoke.py`
- Verify: `tests/test_observability.py`
- Verify: `README.md`
- Verify: `docs/runbooks/live-provider-demo.md`

- [ ] **Step 1: Run the focused regression suite**

Run:

```powershell
pytest tests/test_live_provider_smoke.py tests/test_observability.py -q
```

Expected: all selected tests pass. Investigate any failure before continuing;
do not weaken the new semantic or output-safety assertions.

- [ ] **Step 2: Run the full repository test suite**

Run:

```powershell
pytest -q
```

Expected: all runnable tests pass. PostgreSQL integration tests may report the
repository's documented skips when `DATABASE_URL` is absent or unreachable;
do not count those skips as integration evidence.

- [ ] **Step 3: Check whitespace and patch formatting**

Run:

```powershell
git diff --check
```

Expected: exit code 0 with no output.

- [ ] **Step 4: Confirm the final change scope**

Run:

```powershell
git status --short
git diff --stat
```

Expected: only the smoke implementation, its tests, README, runbook, and
implementation plan appear. The approved design spec remains available at
`docs/specs/2026-10-14-live-smoke-explanations-design.md`, but the repository's
existing `docs/*` ignore rule keeps new spec files out of `git status`. Do not
force-add it and do not alter unrelated user changes.
