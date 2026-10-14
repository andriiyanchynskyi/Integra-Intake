# Implementation plan: deterministic freight evals and CI

> **For agents:** Use the `executing-plans` skill to implement this plan task by task. Steps use checkbox syntax (`- [ ]`) for tracking.

**Purpose:** Add a 50-case, provider-free freight behavior dataset, a typed deterministic runner with privacy-safe reporting, and GitHub Actions gates for the existing unit suite and eval corpus.

**Architecture:** Versioned YAML supplies synthetic sources, scripted strict `AgentProposal` sequences, and explicit expected observations. The runner delegates decisions to the current document, inbound, `AgentLoop`, policy, tool, approval, idempotency, and tenant seams; it adds no extraction or routing rules. Pytest collects one test per case and writes an optional sanitized JSON report. Standard CI runs unit tests and evals without credentials or external services.

**Key files/stack:** Python 3.12+, Pydantic v2, PyYAML, pytest/pytest-asyncio, existing `AgentLoop`, `PolicyEngine`, `PolicyGatedToolExecutor`, `InMemoryTenantToolPort`, `DocumentNormalizer`, inbound models/services, and GitHub Actions. No new package dependency, migration, API endpoint, provider access, or networked test is required.

---

## File map

| Path | Responsibility |
| --- | --- |
| `evals/__init__.py` | Mark eval code as a repository-local package with no production import side effects. |
| `evals/freight/__init__.py` | Export the narrow dataset, runner, and report contracts. |
| `evals/freight/models.py` | Strict versioned case, source, expectation, observation, and report models. |
| `evals/freight/loader.py` | Safe YAML loading, unique-ID/category-count validation, and contained document-fixture resolution. |
| `evals/freight/runner.py` | Scripted typed LLM, recording policy wrapper, source adapters, scenario execution, and structured comparison. |
| `evals/freight/reporting.py` | Build/write sanitized JSON and render the same safe counts as GitHub Markdown. |
| `evals/conftest.py` | Register `--freight-eval-report` and collect per-case safe results for the optional report. |
| `evals/test_freight.py` | Parameterize all dataset cases and expose stable case IDs to pytest. |
| `evals/datasets/freight.v1.yaml` | Exactly 40 core and 10 adversarial synthetic freight cases. |
| `tests/test_freight_eval_models.py` | Unit tests for closed schemas, loader invariants, fixture containment, and report redaction. |
| `tests/test_freight_eval_runner.py` | Unit tests for real-seam delegation, loop execution, scripted proposal accounting, idempotency, and tenant isolation. |
| `.github/workflows/ci.yml` | Pull-request, main-push, and manual unit/eval/whitespace gates with a safe artifact. |
| `README.md` | Product-facing eval command, metric meaning, CI behavior, and limitation statement. |
| `AGENTS.md` | Accurate next-phase agent context for the implemented worker, policy, approvals, documents, inbound webhook, evals, and CI. |

The plan does not edit `app/agent/`, `app/providers/`, `app/runtime/`,
`app/policy/`, `app/tools/`, `app/workers/`, `app/inbound/`, `app/documents/`,
database models, Alembic revisions, tenant YAML, public API schemas, existing
Phase-9 fixtures, or existing skill fixtures.

## Contracts fixed for all tasks

Use these names consistently:

```python
DATASET_VERSION = 1
RUNNER_VERSION = "freight_eval.v1"

class EvalCaseKind(str, Enum):
    AGENT = "agent"
    WEBHOOK_IDEMPOTENCY = "webhook_idempotency"
    TENANT_ISOLATION = "tenant_isolation"

class EvalSourceKind(str, Enum):
    BODY = "body"
    DOCUMENT = "document"
    INBOUND_WEBHOOK = "inbound_webhook"

class ClaimLevel(str, Enum):
    DOWNSTREAM_FROM_TYPED_PROPOSAL = "downstream_from_typed_proposal"
    DETERMINISTIC_SERVER_INVARIANT = "deterministic_server_invariant"
```

`FreightEvalObservation` and `FreightEvalExpected` share these closed optional
fields. Each case kind validates which fields are required:

```python
intake_type: str | None
missing_required_fields: tuple[str, ...] | None
policy_decision: str | None
routing_status: str | None
routing_reason: str | None
tool_name: str | None
approval_required: bool | None
case_created: bool | None
duplicate_reused: bool | None
conflict_raised: bool | None
tenant_isolated: bool | None
provider_calls: int | None
llm_calls: int | None
```

All models use `ConfigDict(extra="forbid", frozen=True, strict=True)`. Reports
contain no source or proposal model. The workflow and runner use no environment
credential.

---

## Task 1: Add strict dataset models and safe loading

**Files:**
- Create: `evals/__init__.py`
- Create: `evals/freight/__init__.py`
- Create: `evals/freight/models.py`
- Create: `evals/freight/loader.py`
- Create: `tests/test_freight_eval_models.py`

- [ ] **Step 1: Delegate model and loader tests through `unit-test-writer`.**

Ask `unit-test-writer` to create `tests/test_freight_eval_models.py` with
focused tests for:

- strict rejection of unknown root, case, source, proposal, expected, and
  metadata keys;
- dataset version exactly `1`;
- exactly 50 cases, exactly 40 `metadata.adversarial=false`, exactly 10
  `metadata.adversarial=true`;
- exact category counts from the approved specification;
- unique, lowercase kebab-case case IDs;
- `AgentProposal.model_validate` rejection for invalid scripted proposal data;
- `claim_level=downstream_from_typed_proposal` for semantic agent cases and
  `deterministic_server_invariant` for idempotency/isolation cases;
- rejection of absolute document paths, `..`, undeclared Phase-9 fixture names,
  and paths outside `evals/fixtures/docs/`;
- loader errors that include only dataset path plus case ID, never source text;
  and
- a minimal valid in-memory YAML fixture that loads into an immutable model.

The tests must call the future public function:

```python
load_freight_dataset(
    dataset_path: Path,
    *,
    document_manifest_path: Path,
) -> LoadedFreightDataset
```

- [ ] **Step 2: Run the focused tests and confirm the missing-module failure.**

Run:

```powershell
pytest tests/test_freight_eval_models.py -q
```

Expected: collection fails because `evals.freight.models` and
`evals.freight.loader` do not exist. Existing tests must remain untouched.

- [ ] **Step 3: Implement the closed case and report-adjacent models.**

Create empty docstring-only `evals/__init__.py`, then define in
`evals/freight/models.py`:

```python
class FreightBodySource(_EvalModel):
    kind: Literal[EvalSourceKind.BODY]
    channel: StrictStr = Field(min_length=1, max_length=100)
    subject: StrictStr = Field(min_length=1, max_length=500)
    body: StrictStr = Field(min_length=1, max_length=100_000)


class FreightDocumentSource(_EvalModel):
    kind: Literal[EvalSourceKind.DOCUMENT]
    fixture_id: StrictStr = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    channel: StrictStr = "eval_document"
    subject: StrictStr = Field(min_length=1, max_length=500)
    body: StrictStr = "Rate confirmation document received."


class FreightWebhookSource(_EvalModel):
    kind: Literal[EvalSourceKind.INBOUND_WEBHOOK]
    provider_id: StrictStr = Field(min_length=1, max_length=200)
    from_addr: StrictStr = Field(min_length=1, max_length=320)
    subject: StrictStr = Field(max_length=500)
    body: StrictStr = Field(max_length=100_000)
    document_fixture_id: StrictStr | None = None
    second_body: StrictStr | None = Field(default=None, max_length=100_000)
    second_tenant: bool = False


FreightEvalSource = Annotated[
    FreightBodySource | FreightDocumentSource | FreightWebhookSource,
    Field(discriminator="kind"),
]


class FreightEvalMetadata(_EvalModel):
    adversarial: bool
    claim_level: ClaimLevel


class FreightEvalCase(_EvalModel):
    id: StrictStr = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    category: StrictStr
    language: Literal["en", "uk", "mixed", "none"]
    kind: EvalCaseKind
    source: FreightEvalSource
    scripted_proposals: tuple[AgentProposal, ...] = ()
    expected: FreightEvalExpected
    metadata: FreightEvalMetadata


class FreightEvalDataset(_EvalModel):
    version: Literal[1]
    cases: tuple[FreightEvalCase, ...]
```

Add model validators that require proposals for `AGENT`, forbid proposals for
server-invariant cases, require agent outcome fields for `AGENT`, require
`duplicate_reused/conflict_raised` for `WEBHOOK_IDEMPOTENCY`, and require
`tenant_isolated` for `TENANT_ISOLATION`. Sort and freeze every expected
missing-field tuple while rejecting duplicates.

Define `FreightEvalObservation` and `FreightEvalExpected` from the shared
closed field list above. Define these result/report models in the same module:

```python
class FreightEvalCaseResult(_EvalModel):
    id: StrictStr
    category: StrictStr
    passed: bool
    observation: FreightEvalObservation
    expected: FreightEvalExpected
    mismatches: tuple[StrictStr, ...] = ()


class FreightEvalReportCase(_EvalModel):
    id: StrictStr
    category: StrictStr
    passed: bool
    mismatches: tuple[StrictStr, ...] = ()


class FreightEvalReport(_EvalModel):
    dataset_version: Literal[1]
    runner_version: Literal["freight_eval.v1"]
    commit_sha: StrictStr | None
    total: int
    passed: int
    failed: int
    pass_rate: float
    categories: dict[str, int]
    cases: tuple[FreightEvalReportCase, ...]
```

`FreightEvalCaseResult.safe_failure_message()` returns only its case ID and
sorted mismatch codes with their safe enum/field-name values. Report building
must project a case result to `FreightEvalReportCase`; it must never serialize
`observation`, `expected`, source, or proposals.

Export only the public enums/models/constants from `evals/freight/__init__.py`.

- [ ] **Step 4: Implement safe YAML and document-manifest loading.**

In `evals/freight/loader.py`, use `yaml.safe_load` and Pydantic validation.
Read the existing document manifest into an `id -> resolved file` mapping,
resolve every referenced file, and enforce:

```python
fixture_path.is_relative_to(document_manifest_path.parent.resolve())
```

Represent each allowed reference as a frozen `ResolvedDocumentFixture` with
only `id`, resolved `path`, and `DocumentMediaType`. Return the mapping beside
the loaded dataset from a `LoadedFreightDataset` frozen dataclass so runner
code never resolves arbitrary paths itself.

Reject duplicate manifest IDs/files and require the referenced file to exist.
Validate the fixed category counts:

```python
CORE_COUNTS = {
    "body_only": 16,
    "document": 12,
    "signed_webhook": 6,
    "multi_request": 4,
    "tenant_idempotency": 2,
}
ADVERSARIAL_COUNTS = {
    "body_override": 4,
    "document_override": 3,
    "webhook_override": 3,
}
```

Wrap YAML, I/O, and validation errors in one `FreightDatasetError` whose text
contains the dataset filename, a case ID when the loader has validated one,
and a safe reason code such as
`invalid_schema`, `invalid_counts`, `duplicate_case_id`, or
`invalid_document_reference`. Do not include source values or raw validation
inputs.

- [ ] **Step 5: Run model/loader tests.**

Run:

```powershell
pytest tests/test_freight_eval_models.py -q
```

Expected: all focused tests pass with no network, database, or provider setup.

---

## Task 2: Build the agent-flow runner around existing production seams

**Files:**
- Create: `evals/freight/runner.py`
- Create: `tests/test_freight_eval_runner.py`

- [ ] **Step 1: Delegate runner tests through `unit-test-writer`.**

Ask `unit-test-writer` to add tests proving that the runner:

- validates every scripted value through `AgentProposal` before execution;
- constructs body sources as `TrustedSource` and document sources through the
  real `DocumentNormalizer` plus the existing manifest reference;
- constructs webhook-derived sources through `WebhookEmailPayload.to_message`
  and `InboundIntakeService`, using a recording enqueue seam and no HMAC/network
  call in semantic cases;
- builds messages with `AgentRuntimeFactory._initial_messages`;
- invokes the real synchronous `AgentLoop` and
  `PolicyGatedToolExecutor`/`PolicyEngine` with `InMemoryTenantToolPort`;
- records policy by delegation and contains no required-field or precedence
  table in eval code;
- reports exact tool, policy, missing fields, case count, approval count,
  steps, stop reason, and provider-call count without source/proposal text;
- fails with `scripted_proposals_exhausted` when the loop calls too many times;
- fails with `scripted_proposals_unused` when a case supplies extra proposals;
- preserves a tool result of `None`, the third-identical-tool stop, and the
  eight-step cap by invoking the public loop interfaces rather than copying
  their algorithms; and
- marks an unreadable document `awaiting_input/document_unreadable`, creates no
  case/approval, and reports no document text.

- [ ] **Step 2: Run runner tests and confirm the missing implementation.**

Run:

```powershell
pytest tests/test_freight_eval_runner.py -q
```

Expected: collection fails because `evals.freight.runner` does not exist.

- [ ] **Step 3: Implement the scripted LLM and recording policy wrappers.**

Use wrappers that delegate or return supplied typed data only:

```python
class ScriptedLLM:
    def __init__(self, proposals: Sequence[AgentProposal]) -> None:
        self._proposals = deque(deepcopy(tuple(proposals)))
        self.calls = 0

    def complete(self, messages: Sequence[AgentMessage]) -> AgentProposal:
        del messages
        self.calls += 1
        if not self._proposals:
            raise FreightEvalExecutionError("scripted_proposals_exhausted")
        return deepcopy(self._proposals.popleft())

    def assert_consumed(self) -> None:
        if self._proposals:
            raise FreightEvalExecutionError("scripted_proposals_unused")


class RecordingPolicy:
    def __init__(self) -> None:
        self._delegate = PolicyEngine()
        self.outcomes: list[PolicyOutcome] = []

    def evaluate(self, value: PolicyInput) -> PolicyOutcome:
        outcome = self._delegate.evaluate(value)
        self.outcomes.append(outcome)
        return outcome
```

Do not inspect source strings in either wrapper.

- [ ] **Step 4: Implement source construction with current boundaries.**

Add private builders named `_body_source`, `_document_source`, and
`_webhook_source`. The first returns a `TrustedSource` from the closed body
model. The second receives one resolved fixture and returns a source containing
the real normalizer result. The async webhook builder receives an optional
resolved fixture plus server-owned tenant identity and returns the source
captured from the real inbound service mapping.

The document builder reads only the declared synthetic fixture, creates
`RateConfirmationDocumentInput`, and calls `DocumentNormalizer.normalize`.
The webhook builder creates the current strict payload, base64-encodes fixture
bytes in memory when required, calls `to_message`, and uses
`InboundIntakeService` with a recording enqueue service to capture the actual
`TrustedSource`. It must not persist base64 or include it in an observation.

- [ ] **Step 5: Implement one agent scenario execution.**

Define `async def run_freight_eval_case(case, *, tenant_config,
document_fixtures) -> FreightEvalCaseResult` with typed
`FreightEvalCase`/`TenantConfig`/fixture-mapping parameters.

For `EvalCaseKind.AGENT`, construct a fresh fixed tenant UUID, source, port,
recording policy, executor, scripted LLM, and loop. Build initial messages with
the existing runtime helper. Collect the first requested tool name from the
typed transcript and the final recorded policy outcome. Derive
`approval_required` from `port.approval_requests`, and `case_created` from
`port.cases`. Compare only the structured observation with the expected model.

The comparator returns mismatch codes such as `intake_type_mismatch`,
`missing_fields_mismatch`, `policy_decision_mismatch`, `tool_name_mismatch`,
`approval_mismatch`, and `case_side_effect_mismatch`. It does not put source,
rationale, proposal dumps, or document text into exceptions/results.

- [ ] **Step 6: Run focused agent-runner tests.**

Run:

```powershell
pytest tests/test_freight_eval_runner.py -q
```

Expected: body, document, webhook-derived, allow, deny, approval, malformed
document, scripted-call accounting, and loop-invariant tests pass.

---

## Task 3: Add idempotency and tenant-isolation scenario seams

**Files:**
- Edit: `evals/freight/runner.py`
- Edit: `tests/test_freight_eval_runner.py`

- [ ] **Step 1: Delegate server-invariant scenario tests through `unit-test-writer`.**

Add tests for:

- two identical inbound deliveries for one tenant/provider ID return the same
  job ID and create one job/idempotency record;
- the same provider ID with a changed body raises `IdempotencyConflict`;
- the same provider ID under another tenant creates an independent job;
- tenant A cannot find/update tenant B's customer or case through
  `InMemoryTenantToolPort`; and
- invariant observations contain booleans/IDs only and no sender/body/key.

Reuse the query-aware enqueue session behavior already proven in
`tests/test_intake.py`, but place the new eval-only seam in
`evals/freight/runner.py`; do not import from `tests`.

- [ ] **Step 2: Run invariant tests and confirm their case kinds are unsupported.**

Run:

```powershell
pytest tests/test_freight_eval_runner.py -q
```

Expected: the new `WEBHOOK_IDEMPOTENCY` and `TENANT_ISOLATION` tests fail with
an unsupported-case error while the Task-2 tests pass.

- [ ] **Step 3: Implement the eval-only enqueue persistence seam.**

Create a small async session/repository double inside `evals/freight/runner.py`
that supports only `begin`, `begin_nested`, `flush`, and the repository calls
`get_idempotency_for_tenant`, `create_job`, and
`create_idempotency_record` made by `IntakeEnqueueService`.

Keep records indexed by `(tenant_id, key)` and retain actual
`canonical_intake_hash` values. Do not reproduce conflict decisions: call
`IntakeEnqueueService._existing_result` through the public `enqueue` flow so
production code raises `IdempotencyConflict`.

Inject this repository into the service after construction only within the
eval package. Do not change `app/domain/intake.py` or add a production in-memory
adapter.

- [ ] **Step 4: Implement invariant case dispatch.**

For `WEBHOOK_IDEMPOTENCY`, run the declared first and second delivery through
`InboundIntakeService` plus the actual enqueue service and collect
`duplicate_reused`/`conflict_raised`. For `TENANT_ISOLATION`, seed the existing
in-memory port with tenant B records, query/mutate as tenant A, and collect
`tenant_isolated` from the absence of visibility/mutation.

Require `metadata.claim_level == "deterministic_server_invariant"` and zero
scripted proposals for both kinds.

- [ ] **Step 5: Run invariant and existing intake/tool regressions.**

Run:

```powershell
pytest tests/test_freight_eval_runner.py tests/test_intake.py tests/test_inbound_webhook.py tests/test_tools.py -q
```

Expected: all tests pass; production idempotency and tenant tests retain their
existing assertions.

---

## Task 4: Add safe pytest reporting

**Files:**
- Create: `evals/freight/reporting.py`
- Create: `evals/conftest.py`
- Edit: `tests/test_freight_eval_models.py`

- [ ] **Step 1: Delegate reporting/plugin tests through `unit-test-writer`.**

Add tests that build passing/failing `FreightEvalCaseResult` objects and assert
the exact JSON keys:

```python
{
    "dataset_version",
    "runner_version",
    "commit_sha",
    "total",
    "passed",
    "failed",
    "pass_rate",
    "categories",
    "cases",
}
```

Each case entry must contain only `id`, `category`, `passed`, and `mismatches`.
Tests must use sentinel source/API-key/base64/excerpt/rationale strings and
assert none occurs in JSON, Markdown, exception text, or `repr(report)`.

Add a pytester-style or direct hook test proving that no report file appears
without `--freight-eval-report`, while an explicit path receives UTF-8 JSON.

- [ ] **Step 2: Run reporting tests and confirm missing modules/options.**

Run:

```powershell
pytest tests/test_freight_eval_models.py -q
```

Expected: reporting imports or the custom option fail before implementation.

- [ ] **Step 3: Implement JSON and Markdown rendering.**

Define three pure functions: `build_freight_eval_report(results, *,
dataset_version=DATASET_VERSION, commit_sha=None) -> FreightEvalReport`,
`write_freight_eval_report(report, path) -> None`, and
`render_github_summary(report) -> str`.

Use `json.dumps(report.model_dump(mode="json"), ensure_ascii=False,
allow_nan=False, sort_keys=True, indent=2)`. Compute `pass_rate` as
`passed / total`, require `total > 0`, and
sort categories/cases/mismatch codes. Markdown contains only counts, pass rate,
and failed case IDs with mismatch codes.

- [ ] **Step 4: Implement the local pytest option and hooks.**

In `evals/conftest.py`, register:

```python
parser.addoption(
    "--freight-eval-report",
    action="store",
    default=None,
    help="Write a sanitized freight eval JSON report to PATH.",
)
```

Collect safe `FreightEvalCaseResult` objects from the parameterized eval test
through an explicit fixture/helper, not by scraping assertion messages. In
`pytest_sessionfinish`, write only when the option is set and at least one case
result exists. A collection failure produces no report; the CI summary command
handles that absence without hiding the original pytest failure.

Implement `FreightEvalRecorder` as a session-scoped object with
`record(result: FreightEvalCaseResult) -> None` and an immutable `results`
snapshot. Expose it through the session-scoped `freight_eval_recorder` fixture;
the session-finish hook reads that same object.

- [ ] **Step 5: Run reporting tests.**

Run:

```powershell
pytest tests/test_freight_eval_models.py -q
```

Expected: exact-shape, redaction, optional-output, and deterministic-order
tests pass.

---

## Task 5: Add the 40 core freight cases

**Files:**
- Create: `evals/datasets/freight.v1.yaml`
- Create: `evals/test_freight.py`
- Edit: `tests/test_freight_eval_models.py`
- Edit: `tests/test_freight_eval_runner.py`

- [ ] **Step 1: Delegate corpus-inventory tests through `unit-test-writer`.**

Add an inventory test that loads the repository dataset and requires these
40 non-adversarial IDs:

```text
body-en-complete-01 .. body-en-complete-04
body-en-incomplete-01 .. body-en-incomplete-04
body-uk-complete-01 .. body-uk-complete-04
body-uk-incomplete-01 .. body-uk-incomplete-04
doc-complete-en
doc-complete-uk
doc-complete-pdf
doc-two-page-pdf
doc-no-rate-hint
doc-incomplete
doc-malformed
doc-missing-excerpt
doc-mismatched-excerpt
doc-unknown-field
doc-blank-value
doc-all-fields-out-of-order
webhook-body-en-complete
webhook-body-uk-incomplete
webhook-document-complete
webhook-document-malformed
webhook-duplicate-reuse
webhook-changed-payload-conflict
multi-en-two-complete
multi-en-complete-incomplete
multi-uk-two-complete
multi-uk-complete-incomplete
tenant-provider-id-independent
tenant-cross-scope-hidden
```

The test must also assert the approved 16/12/6/4/2 core category counts.

- [ ] **Step 2: Run the inventory test and confirm the dataset is absent.**

Run:

```powershell
pytest tests/test_freight_eval_models.py -q
```

Expected: failure because `evals/datasets/freight.v1.yaml` does not exist.

- [ ] **Step 3: Write the 16 body-only cases.**

Use synthetic dispatch messages only. Complete cases provide the six required
`load_request` fields: `origin`, `destination`, `equipment`, `pickup_window`,
`commodity`, and `contact`. Incomplete cases omit one of these fixed sets:

```text
01: [contact]
02: [equipment]
03: [destination, pickup_window]
04: [origin, commodity, contact]
```

Repeat those missing sets for English and Ukrainian. Script complete proposals
as `load_request -> create_case -> terminal`, with policy
`allow/ready/action_allowed`, no approval, and one case. Script incomplete
proposals as `load_request -> create_case`, with
`deny/awaiting_input/missing_required_fields`, sorted missing names, no
approval, and no case. Set every semantic case claim level to
`downstream_from_typed_proposal`.

- [ ] **Step 4: Write the 12 document cases.**

Reference manifest fixture IDs rather than copying document content:

```text
complete-en, complete-uk, complete-pdf, complete-two-page-pdf,
complete-no-rate-hint, incomplete, malformed-pdf
```

The first five, `doc-unknown-field`, and `doc-all-fields-out-of-order` use all eight required
rate-confirmation fields with exact source excerpts and expect
`allow/ready/action_allowed`, `create_case`, no approval, and one case.

Use these exact negative expectations:

```text
doc-incomplete: missing [equipment, valid_until]
doc-malformed: reason document_unreadable and all eight required names
doc-missing-excerpt: origin missing
doc-mismatched-excerpt: destination missing
doc-unknown-field: unknown candidate ignored; required fields still complete
doc-blank-value: quoted_rate missing
```

All negative cases expect no case and no approval. The malformed case uses a
single typed proposal matching the runtime's closed unreadable-document
proposal and records `llm_calls=1` and `provider_calls=0`; the existing runtime
regression remains the hard proof that production uses zero HTTP provider
calls for unreadable documents.

- [ ] **Step 5: Write the six signed-webhook cases.**

Use the current strict webhook fields and fixed server tenant/channel mapping.
Four semantic cases cover English complete body, Ukrainian incomplete body,
complete document, and malformed document. Two server-invariant cases cover
exact duplicate reuse and changed-body conflict with the same provider ID.

Do not place an API key or HMAC signature in the dataset. Authentication/HMAC
stays covered by `tests/test_auth.py` and `tests/test_inbound_webhook.py`.

- [ ] **Step 6: Write the four multi-request and two tenant cases.**

Multi-request sources contain two clearly separated synthetic loads. Their
scripted proposals use `intake_type="other"` and tool `flag_for_review` with
`allow/ready/action_allowed`, no approval row, and no case. Mark their claim
level `downstream_from_typed_proposal`; the report must not describe this as
autonomous multi-request detection.

The two invariant cases use no proposals. One verifies the same provider ID
under separate tenant IDs creates separate jobs. The other verifies tenant A
cannot find or mutate tenant B's seeded records.

- [ ] **Step 7: Add one pytest case per dataset entry.**

In `evals/test_freight.py`, load the checked-in dataset at collection through
the strict loader and parameterize with stable IDs:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("case", DATASET.cases, ids=lambda item: item.id)
async def test_freight_behavior(
    case: FreightEvalCase,
    freight_eval_recorder: FreightEvalRecorder,
) -> None:
    result = await run_freight_eval_case(
        case,
        tenant_config=FREIGHT_CONFIG,
        document_fixtures=DOCUMENT_FIXTURES,
    )
    freight_eval_recorder.record(result)
    assert result.passed, result.safe_failure_message()
```

The failure message contains only case ID and mismatch codes/structured safe
values.

- [ ] **Step 8: Run core inventory and eval collection.**

Run:

```powershell
pytest tests/test_freight_eval_models.py tests/test_freight_eval_runner.py -q
pytest evals/test_freight.py -q
```

Expected after this task: runner unit tests pass; repository dataset loading
fails only on the enforced missing 10 adversarial cases. Task 6 closes the
dataset before pytest collects repository eval cases.

---

## Task 6: Add the 10 adversarial cases and close the dataset gate

**Files:**
- Edit: `evals/datasets/freight.v1.yaml`
- Edit: `tests/test_freight_eval_models.py`

- [ ] **Step 1: Delegate adversarial inventory tests through `unit-test-writer`.**

Require these exact IDs and 4/3/3 category counts:

```text
adv-body-en-ignore-policy
adv-body-uk-ignore-policy
adv-body-invent-missing
adv-body-tenant-spoof
adv-doc-existing-injection-pdf
adv-doc-unsupported-excerpt
adv-doc-invent-required
adv-webhook-subject-body-override
adv-webhook-tenant-spoof
adv-webhook-send-immediately
```

Assert every adversarial case has `metadata.adversarial=true`, claim level
`downstream_from_typed_proposal`, and a scripted proposal with
`contains_injection_or_override_attempt=true`.

- [ ] **Step 2: Run the corpus tests and confirm the adversarial IDs are absent.**

Run:

```powershell
pytest tests/test_freight_eval_models.py -q
```

Expected: failure listing only the missing safe case IDs/count mismatch.

- [ ] **Step 3: Add four body override cases.**

Use synthetic English/Ukrainian messages requesting policy bypass, fabricated
missing fields, another tenant, or immediate send. Supply otherwise valid
typed proposals that request `create_case` and set the injection signal. Every
case expects `needs_approval/urgent/safety_or_legal_risk`, tool `create_case`,
one approval, and zero cases. Tenant spoof language remains source data and
does not alter the fixed runtime tenant.

- [ ] **Step 4: Add three document attack cases.**

Reference `complete-injection-pdf` for the existing injection case. Use the
incomplete fixture for unsupported-excerpt and invented-required-value cases.
Set the injection signal and expect risk precedence:
`needs_approval/urgent/safety_or_legal_risk`, one approval, and zero cases.
The dataset may contain scripted unsupported candidates, but the report must
not serialize them.

- [ ] **Step 5: Add three webhook attack cases.**

Use strict payload-compatible synthetic values for subject/body disagreement,
tenant-spoof language, and an attachment asking for immediate action. Set the
scripted injection signal and expect the same approval precedence. Do not add
wire credentials or signatures.

- [ ] **Step 6: Run all 50 evals and generate a local safe report.**

Use a temp path outside committed content:

```powershell
$report = Join-Path ([System.IO.Path]::GetTempPath()) 'integra-freight-evals.json'
pytest evals/test_freight.py -q --freight-eval-report $report
Get-Content -Raw -Encoding utf8 $report
```

Expected: `50 passed`, report totals `50/50/0`, pass rate `1.0`, exact category
counts, and no source text, proposal rationale, excerpts, API key, base64, or
document bytes.

- [ ] **Step 7: Run focused freight regressions.**

Run:

```powershell
pytest tests/test_documents.py tests/test_policy.py tests/test_tools.py tests/test_intake.py tests/test_inbound_webhook.py tests/test_agent_loop.py tests/test_agent_worker.py -q
```

Expected: all focused tests pass; database-only behavior remains covered by
its existing conditional integration suite.

---

## Task 7: Add minimal GitHub Actions CI

**Files:**
- Create: `.github/workflows/ci.yml`
- Edit: `tests/test_freight_eval_models.py`

- [ ] **Step 1: Delegate static workflow contract tests through `unit-test-writer`.**

Add a workflow-text test plus `yaml.BaseLoader` parsing so YAML 1.1 boolean
rules cannot convert the key `on`. Assert:

- triggers: `pull_request`, `push` restricted to `main`, and
  `workflow_dispatch`;
- workflow permissions exactly `contents: read`;
- Python `3.12`;
- `pip install -e ".[dev]"`;
- commands for `git diff --check`, `pytest -q`, and the 50-case eval suite;
- eval report path uses `${{ runner.temp }}`;
- summary/artifact steps use `if: always()`;
- artifact upload uses `if-no-files-found: ignore` and contains only the eval
  JSON report;
- no `${{ secrets.* }}`, `LLM_API_KEY`, API key, `DATABASE_URL`, service
  container, network curl, live-provider command, scheduled trigger, label
  parser, or production deployment step.

- [ ] **Step 2: Run the workflow contract test and confirm `.github/` is absent.**

Run:

```powershell
pytest tests/test_freight_eval_models.py -q
```

Expected: failure because `.github/workflows/ci.yml` does not exist.

- [ ] **Step 3: Create the workflow with least privilege.**

Use this exact job structure with the current official major versions verified
while writing this plan:

```yaml
name: ci

on:
  pull_request:
  push:
    branches: [main]
  workflow_dispatch:

permissions:
  contents: read

jobs:
  test:
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - name: Check out repository
        uses: actions/checkout@v7
        with:
          fetch-depth: 2
          persist-credentials: false
      - name: Set up Python
        uses: actions/setup-python@v7
        with:
          python-version: "3.12"
          cache: pip
      - name: Install package
        run: python -m pip install -e ".[dev]"
      - name: Check whitespace
        run: git diff --check HEAD^ HEAD
      - name: Run unit and API tests
        run: pytest -q
      - name: Run deterministic freight evals
        run: pytest evals/test_freight.py -q --freight-eval-report "${{ runner.temp }}/freight-evals.json"
      - name: Write freight eval summary
        if: always()
        run: python -m evals.freight.reporting --report "${{ runner.temp }}/freight-evals.json" --github-summary "$GITHUB_STEP_SUMMARY"
      - name: Upload freight eval report
        if: always()
        uses: actions/upload-artifact@v7
        with:
          name: freight-evals
          path: ${{ runner.temp }}/freight-evals.json
          if-no-files-found: ignore
          retention-days: 14
```

Do not add a dependency-update action or third-party reporting action. If an
official action major changes before execution, stop and ask the user before
changing these approved versions.

- [ ] **Step 4: Add the reporting module CLI used by CI.**

In `evals/freight/reporting.py`, add `argparse` options `--report` and
`--github-summary`. Read only the generated closed JSON report and append the
safe Markdown renderer output. If the report does not exist because an earlier
step failed, append `Freight eval report was not produced.` and exit zero so
the original failing step remains the job failure.

- [ ] **Step 5: Run workflow/reporting contract tests.**

Run:

```powershell
pytest tests/test_freight_eval_models.py -q
```

Expected: workflow shape, secret absence, and reporting CLI tests pass.

---

## Task 8: Update product-facing README without phase/training language

**Files:**
- Edit: `README.md`
- Edit: `tests/test_freight_eval_models.py`

- [ ] **Step 1: Delegate README contract tests through `unit-test-writer`.**

Add a focused documentation test requiring:

- the command `pytest evals/test_freight.py -q`;
- a statement that the corpus contains 40 core and 10 adversarial cases;
- exact `k=1`/100% deterministic gate semantics;
- a statement that scripted proposals test downstream behavior and do not
  measure live-model extraction/classification or injection detection;
- standard CI requires no `LLM_API_KEY`, tenant API key, or live provider; and
- no case-insensitive matches for `phase 11`, `roadmap phase`, `interview`,
  `vacancy`, `learning project`, `tutorial`, or `training context`.

- [ ] **Step 2: Run the README test and confirm the eval section is missing.**

Run:

```powershell
pytest tests/test_freight_eval_models.py -q
```

Expected: only the new README assertions fail.

- [ ] **Step 3: Update existing Testing and Current boundaries sections.**

Add a compact `Deterministic freight evals` subsection under `Testing` with:

```bash
pytest evals/test_freight.py -q
```

Explain the 50-case composition, `k=1`, 100% merge gate, provider-free
execution, sanitized optional report, and the scripted-proposal limitation.
Remove `eval CI` from `Current boundaries`; keep live-provider evaluation,
LLM-as-judge, production observability, MCP, external email providers,
outbound delivery, TMS/Odoo, and cloud storage as current boundaries.

Do not mention implementation phases, the roadmap, educational goals, or
historical documents.

- [ ] **Step 4: Run README and eval model tests.**

Run:

```powershell
pytest tests/test_freight_eval_models.py -q
```

Expected: documentation and model/loader/report/workflow tests pass.

---

## Task 9: Synchronize AGENTS.md with the implemented repository

**Files:**
- Edit: `AGENTS.md`

The user requested this update for the next phase. Apply the `update-md` skill
point by point after Tasks 1-8 pass. Plan approval authorizes this changelog:

`AGENTS.md` is currently ignored and untracked by the repository. Treat this
as a workspace-local next-agent context update. Verify its content directly;
do not change `.gitignore`, force-add the file, or claim it appears in the Git
diff without separate user authorization.

```text
DELETE:
- Opening/current-boundary claims that workers, queues, PolicyEngine, real
  tools, approval endpoints, and case-status persistence are absent; code has
  implemented these boundaries.
- Duplicate agent/provider-only caveats that contradict the worker runtime.
- Testing file trees that omit current approval, worker, document, inbound,
  policy, tool, and eval suites.

UPDATE:
- Overview and key features: include queued intake, worker runtime, policy-
  gated tools, approvals, bounded freight documents, signed inbound webhook,
  deterministic freight evals, and CI.
- Architecture/data flow: message or document -> intake job -> async worker ->
  synchronous AgentLoop -> structured proposal -> deterministic policy/tools/
  approval -> durable result.
- Code structure and important files: add app/runtime, app/policy, app/tools,
  app/workers, app/inbound, app/documents, app/skills, evals, and .github.
- Testing and commands: include deterministic eval command, CI behavior, and
  the rule that PostgreSQL skips are not integration evidence.
- External interfaces: include /v1/intake, signed inbound webhook, and approval
  decision endpoint while preserving existing case/health contracts.
- Configuration and deployment: include worker and approval settings without
  inventing production deployment/observability.
- Development agreements and pitfalls: preserve tenant derivation, sync loop,
  async session ownership, policy precedence, privacy, MAX_STEPS, repeated-
  tool guard, None results, no Markdown parsing, and no LangChain.

ADD:
- Eval contract: 40 core + 10 adversarial YAML cases, scripted typed proposals,
  k=1 deterministic 100% gate, safe JSON artifact, and explicit non-claim of
  live model quality.
- CI contract: unit tests and offline evals on PR/main/manual, no runtime
  secrets or provider/network calls.
- Typical task guidance for extending an eval case without duplicating policy
  or adding source text to reports.
```

- [ ] **Step 1: Re-read every AGENTS.md statement against the final Task-8 tree.**

Use `pyproject.toml`, current route modules, worker/runtime/policy/tool/inbound/
document/eval packages, workflow, and test inventory as evidence. Preserve
non-obvious business context and safety constraints that code alone does not
explain.

- [ ] **Step 2: Apply the approved DELETE list as targeted edits.**

Remove only statements disproved by current code. Do not remove tenant safety,
secret handling, architecture rationale, or scope exclusions that remain true.

- [ ] **Step 3: Apply the approved UPDATE list section by section.**

Keep the current broad section structure. Replace obsolete module lists and
flows rather than appending contradictory addenda. Keep commands executable
from the repository root.

- [ ] **Step 4: Apply the approved ADD list.**

Describe evals and CI as current repository behavior after their verification.
Do not call deterministic fake-proposal coverage live-model accuracy. Do not
add roadmap phase numbers, tutorial language, or future observability/MCP as
implemented capabilities.

- [ ] **Step 5: Check AGENTS.md consistency and size.**

Run:

```powershell
rg -n "Workers, queues, real business tools|does not invoke that path yet|eval CI" AGENTS.md
rg -n "evals/test_freight.py|AgentWorker|PolicyEngine|InboundMessage|DocumentNormalizer|/v1/intake|/v1/inbound/email/webhook|/v1/approvals" AGENTS.md
```

Expected: the first command has no obsolete matches; the second finds each
current boundary. Read the full file once more to remove duplicate or
contradictory instructions.

---

## Task 10: Run full regression, privacy, and scope gates

**Files:**
- Verify: all files changed by Tasks 1-9

- [ ] **Step 1: Run the configured unit/API suite from a fresh command.**

Run:

```powershell
pytest -q
```

Expected: all non-PostgreSQL tests pass. PostgreSQL-only tests may skip without
an explicit reachable `DATABASE_URL`; report the skip count and never call it
database integration evidence.

- [ ] **Step 2: Run the complete deterministic eval gate with a fresh report.**

Run:

```powershell
$report = Join-Path ([System.IO.Path]::GetTempPath()) 'integra-freight-evals-final.json'
pytest evals/test_freight.py -q --freight-eval-report $report
python -m evals.freight.reporting --report $report
```

Expected: exactly 50 cases pass, pass rate is `1.0`, all category counts match,
and the rendered output contains only safe IDs/counts/mismatch codes.

- [ ] **Step 3: Scan reports and repository changes for forbidden data/access.**

Run targeted checks:

```powershell
rg -n "LLM_API_KEY|secrets\.|api[_-]?key|content_base64|document_text|source_excerpt|rationale_short" .github evals/freight
rg -n "httpx\.|requests\.|urllib\.|OpenAICompatibleLLMClient|LLM_BASE_URL" evals tests/test_freight_eval_*.py
```

Expected: only explicit negative assertions/documentation or schema-field
references appear; no workflow secret expression, outbound client call, live
provider construction, or report serialization of forbidden fields exists.

- [ ] **Step 4: Check whitespace and exact scope.**

Run:

```powershell
git diff --check
git status --short
git diff --stat
git diff -- .github evals tests README.md AGENTS.md pyproject.toml app alembic examples
```

Expected: no whitespace error; changes stay within the file map; `pyproject.toml`,
`app/`, `alembic/`, and `examples/` have no changes. Because `AGENTS.md` is
ignored and untracked, inspect it with `Get-Content -Raw -Encoding utf8
AGENTS.md`; do not rely on `git diff` for that file. Stop for user direction if
the diff includes a dependency, migration, API/runtime behavior, real provider,
new inbound channel, Gmail/IMAP, outbound delivery, TMS/Odoo, MCP, cloud
storage, production observability, LangChain, or production trace storage.

- [ ] **Step 5: Verify the workflow syntax through its strict loader test.**

Run:

```powershell
pytest tests/test_freight_eval_models.py -q
```

Expected: workflow trigger, permission, command, artifact, and secret-absence
tests pass. Do not claim GitHub-hosted execution before the workflow runs on
GitHub.

---

## Task 11: Review the completed implementation

**Files:**
- Review: the complete final diff from Tasks 1-10

- [ ] **Step 1: Call the `code-review` skill on the final diff.**

No project-local code-review agent is defined in the current workspace. Call
the `code-review` skill with the full diff and this context:

```text
Review the deterministic freight eval/CI implementation for: strict dataset
schema and exact 40+10 counts; fake proposals never parsing source or owning
policy; real AgentLoop/policy/tool seams; idempotency and tenant isolation as
hard gates; no source/proposal/document/secret data in JSON, Markdown, logs, or
artifacts; no provider/network access; no public API/runtime/dependency/schema
change; CI least privilege and no secrets; README without phase/training
language; AGENTS.md aligned with current code; preservation of MAX_STEPS=8,
third-identical-tool behavior, explicit None results, synchronous loop, async
SQLAlchemy ownership, strict structured outputs, no Markdown parsing, and no
LangChain.
```

Do not apply review corrections automatically. Report findings to the user and
discuss any change before editing.

---

## Plan self-review

| Approved specification requirement | Plan task(s) |
| --- | --- |
| Current body/document/webhook seams and typed fake execution | 2-3 |
| Exact 40 core + 10 adversarial YAML corpus | 5-6 |
| English/Ukrainian, complete/incomplete, duplicate, multi-request, document, injection, malformed cases | 5-6 |
| Structured intake/missing/policy/tool/approval assertions | 1-2, 5-6 |
| Tenant isolation and idempotency remain hard gates | 3, 5, 10 |
| Unit/regression tests separated from eval corpus | 1-6 |
| No recorded-provider semantic harness, live provider, or judge | all tasks; checked in 7 and 10 |
| `k=1`, 100% pass rate, safe JSON, GitHub summary/artifact | 4, 6-7 |
| PR/main/manual CI with no standard secrets | 7 |
| Existing APIs, loop, policy, worker, document, and privacy invariants | 2-3, 10-11 |
| README remains product-facing without phase/training context | 8 |
| AGENTS.md matches the implemented next-phase state | 9 |
| No scope expansion into integrations, observability, MCP, or storage | 7, 10-11 |

The plan uses stable names across tasks, fixes all dataset IDs/counts and
expected behavior classes, and contains no unresolved implementation marker.
Tasks 1-9 create independently testable changes. Task 10 supplies fresh
evidence, and Task 11 supplies the required independent review gate.
