# Implementation plan: universal intake observability

> **For agents:** Use `executing-plans` task by task. Preserve all existing
> uncommitted Phase 12 changes; never reset unrelated work.

**Purpose:** Replace freight assumptions in profile, document, runtime,
action, observation, and eval seams with versioned server-owned capabilities.

**Architecture:** Compile strict tenant profiles against immutable action and
document registries. Persist generic snapshots, stop unreadable inputs through
deterministic preflight, and propagate trusted scenario identity through the
existing trace. Reuse one scripted eval core without weakening the 50-case
freight suite.

**Stack:** Python 3.12, Pydantic v2, FastAPI, async SQLAlchemy/PostgreSQL,
structlog, pytest, YAML tenant profiles.

**Specification:**
`docs/specs/2026-09-29-phase-12-universal-intake-observability-design.md`

---

## File map

Create:

- `app/tenants/identifiers.py` — bounded technical identifiers.
- `app/tenants/compiled.py` — immutable compiled profile contracts.
- `app/tools/registry.py` — executable action metadata.
- `app/documents/registry.py` — versioned normalizer metadata.
- `app/documents/legacy.py` — legacy rate-confirmation snapshot decoder.
- `app/runtime/legacy_profiles.py` — hash-preserving profile-v1 upgrader.
- `app/runtime/preflight.py` — pure continue/terminal decision.
- `evals/core/models.py`, `evals/core/runner.py` — reusable scripted seams.
- `evals/repair/cases.py`, `evals/education/cases.py` — compact contracts.
- `tests/test_scenario_profiles.py`, `tests/test_scenario_contracts.py`.

Edit:

- `app/tenants/config.py`, `loader.py`, `examples/*.yaml` — profile v2.
- `app/runtime/profiles.py`, `factory.py`, `app/policy/*` — compiled runtime.
- `app/documents/*`, `app/domain/intake.py`, `app/inbound/service.py` — generic
  document intake.
- `app/tools/models.py`, `executor.py`, `postgres.py` — registry execution.
- `app/observability/events.py`, `context.py` and emitters — safe dimensions.
- `app/workers/agent_worker.py`, approval services/repositories — terminal
  preflight and original profile identity.
- `evals/freight/runner.py` — delegate common runner only.
- `README.md`, `AGENTS.md` — verified product/contributor behavior.

---

### Task 0: Establish the dirty-worktree baseline

**Files:** Verify only.

- [ ] Run `git status --short`, `git diff --stat`, and `git diff --check`.

Expected: existing Phase 12 changes remain visible; whitespace check exits 0.

- [ ] Run:

```powershell
pytest tests/test_tenant_config.py tests/test_documents.py tests/test_runtime.py tests/test_tools.py tests/test_observability.py -q
```

Expected: all collected baseline tests pass. Diagnose any failure before edits.

---

### Task 1: Introduce the version-2 profile contract

**Files:** Create `app/tenants/identifiers.py`; edit
`app/tenants/config.py`, `app/tenants/__init__.py`, `app/tenants/loader.py`,
`examples/*.yaml`; test `tests/test_tenant_config.py`.

- [ ] Add failing tests asserting every checked-in profile has version 2 and
  scenario keys `freight_broker`, `repair_service`, `language_school`; reject
  `Customer Name`, `bad/type`, duplicate defaults, missing action execution
  mode, and document bindings to unknown intake types.

```python
def test_profiles_have_explicit_scenario_identity() -> None:
    expected = {
        "freight-broker.yaml": "freight_broker",
        "repair-service.yaml": "repair_service",
        "language-school.yaml": "language_school",
    }
    for filename, key in expected.items():
        value = load_tenant_config(EXAMPLES / filename)
        assert (value.profile_version, value.scenario_key) == (2, key)
```

- [ ] Run `pytest tests/test_tenant_config.py -q`.

Expected: FAIL because the v2 fields do not exist.

- [ ] Add this identifier contract:

```python
SAFE_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
SafeIdentifier = Annotated[
    str, StringConstraints(pattern=SAFE_IDENTIFIER_PATTERN)
]
```

- [ ] Add `ActionExecutionMode(EXECUTABLE, POLICY_ONLY)`, required
  `ActionRule.execution`, `DocumentBindingConfig(intake_type, normalizer,
  normalizer_version>=1, default_for_inbound=False)`, and these `TenantConfig`
  fields:

```python
profile_version: Literal[2]
scenario_key: SafeIdentifier
documents: dict[SafeIdentifier, DocumentBindingConfig] = Field(default_factory=dict)
```

Use `SafeIdentifier` for intake, field, action, and document keys/references.
Validate document intake references and at most one inbound default.

- [ ] Update all example actions: five current tools are `executable`;
  `send_reply` and `close_case` are `policy_only`. Add only this freight
  binding; use `documents: {}` for the other profiles:

```yaml
documents:
  rate_confirmation:
    intake_type: rate_confirmation
    normalizer: bounded_text_pdf
    normalizer_version: 1
    default_for_inbound: true
```

- [ ] Update existing fixtures and run `pytest tests/test_tenant_config.py -q`.

Expected: PASS without routing/field-policy changes.

---

### Task 2: Add immutable action and document registries

**Files:** Create `app/tools/registry.py`, `app/documents/registry.py`; edit
`app/tools/models.py`, package exports; test `tests/test_tools.py`,
`tests/test_documents.py`.

- [ ] Add failing tests for duplicate/unsafe keys, exact normalizer versions,
  and the built-in catalogs:

```python
assert BUILTIN_ACTION_REGISTRY.keys == frozenset({
    "find_customer", "create_case", "update_case_fields",
    "create_reply_draft", "flag_for_review",
})
assert BUILTIN_DOCUMENT_REGISTRY.require("bounded_text_pdf", 1).version == 1
with pytest.raises(DocumentCapabilityUnavailable):
    BUILTIN_DOCUMENT_REGISTRY.require("bounded_text_pdf", 2)
```

- [ ] Run `pytest tests/test_tools.py tests/test_documents.py -q`.

Expected: FAIL on missing registry imports.

- [ ] Implement immutable mappings with these descriptors:

```python
@dataclass(frozen=True, slots=True)
class ActionCapability:
    key: str
    arguments_model: type[BaseModel]
    requires_complete_fields: bool
    commits_side_effect: bool
    pending_action_version: int = 1

@dataclass(frozen=True, slots=True)
class DocumentNormalizerCapability:
    key: str
    version: int
    supported_media_types: frozenset[DocumentMediaType]
```

Registry constructors validate safe unique keys and positive versions;
`require` raises a fixed-message typed error. Register the five current action
argument models and `bounded_text_pdf` v1. No handlers or dynamic imports live
in registries.

- [ ] Change `PendingAction.name` to `SafeIdentifier` and its version to a
  positive strict integer; execution authorization will be registry lookup.

- [ ] Run `pytest tests/test_tools.py tests/test_documents.py tests/test_approvals.py -q`.

Expected: PASS; execution behavior remains unchanged.

---

### Task 3: Replace freight document models with generic snapshots

**Files:** Edit `app/documents/models.py`, `normalizer.py`, `__init__.py`;
create `app/documents/legacy.py`; test `tests/test_documents.py`.

- [ ] Add failing tests for `DocumentInput`, `NormalizedDocument`, trusted
  binding fields, exact text/error exclusivity, and legacy decoding.

```python
result = DocumentNormalizer().normalize(
    DocumentInput(channel="email", subject="doc", body="",
                  media_type=DocumentMediaType.TEXT, text="hello"),
    document_kind="rate_confirmation",
    target_intake_type="rate_confirmation",
    normalizer_key="bounded_text_pdf",
    normalizer_version=1,
)
assert result.snapshot_version == 2
assert result.target_intake_type == "rate_confirmation"
```

- [ ] Run `pytest tests/test_documents.py -q`.

Expected: FAIL on missing generic models/signature.

- [ ] Implement `DocumentInput` with the current bounded input fields and:

```python
class NormalizedDocument(_StrictDocumentModel):
    snapshot_version: Literal[2] = 2
    document_kind: SafeIdentifier
    target_intake_type: SafeIdentifier
    media_type: DocumentMediaType
    sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    normalizer_key: SafeIdentifier
    normalizer_version: Annotated[int, Field(ge=1)]
    text: StrictStr | None = None
    extraction_error: DocumentExtractionError | None = None
```

- [ ] Make all normalizer success/error constructors require the four trusted
  binding arguments. Preserve byte/page/text limits and PDF behavior exactly.

- [ ] Isolate old literals (`rate_confirmation`,
  `rate_confirmation_document.v1`) in `app/documents/legacy.py`; decode them to
  v2 without rewriting stored JSON. Common models/runtime must not import a
  freight-specific type.

- [ ] Run `pytest tests/test_documents.py tests/test_inbound_webhook.py -q`.

Expected: PASS including all malformed/encrypted/size/redaction tests.

---

### Task 4: Compile profiles and upgrade legacy snapshots hash-first

**Files:** Create `app/tenants/compiled.py`, `app/runtime/legacy_profiles.py`,
`tests/test_scenario_profiles.py`; edit `app/runtime/profiles.py`, exports,
`tests/test_runtime.py`.

- [ ] Add failing tests for action-set separation, document binding resolution,
  missing executable adapters, missing normalizer versions, v1 upgrade, and
  hash verification before upgrade.

```python
assert "send_reply" in compiled.declared_actions
assert "send_reply" not in compiled.available_actions
assert "create_case" in compiled.available_actions
assert compiled.default_inbound_document.document_kind == "rate_confirmation"
```

- [ ] Run `pytest tests/test_scenario_profiles.py tests/test_runtime.py -q`.

Expected: FAIL on missing compiler contracts.

- [ ] Implement frozen `CompiledDocumentBinding` and
  `CompiledTenantProfile(config, scenario_key, profile_fingerprint,
  declared_actions, registered_actions, available_actions, documents,
  default_inbound_document)`.

- [ ] Implement `compile_tenant_profile`: executable actions must be
  registered; policy-only actions remain declared/unavailable; every document
  binding must resolve its exact normalizer version.

- [ ] Implement the v1 upgrader only in `legacy_profiles.py`: derive scenario
  key from validated slug, infer executable mode from the built-in action
  registry, mark other actions policy-only, and add the legacy freight binding
  only when `rate_confirmation` exists.

- [ ] In persisted-profile resolution, SHA-256 the untouched mapping and
  compare first; only then upgrade and validate. Extend `ResolvedTenantProfile`
  with `compiled` and inject registries with built-in defaults.

- [ ] Run:

```powershell
pytest tests/test_scenario_profiles.py tests/test_tenant_config.py tests/test_runtime.py -q
```

Expected: PASS with original canonical hashes for new profiles and readable v1
snapshots.

---

### Task 5: Add universal observation dimensions

**Files:** Edit `app/observability/events.py`, `context.py`, exports; test
`tests/test_observability.py`, `tests/test_observability_api.py`.

- [ ] Add failing tests for safe scenario binding, 64-hex profile fingerprints,
  preflight/capability enums, known flags, and rejection of raw unknown names.

- [ ] Run `pytest tests/test_observability.py tests/test_observability_api.py -q`.

Expected: FAIL on missing fields/enums.

- [ ] Add events `profile.resolution.completed` and
  `runtime.preflight.completed`; components `profile`, `runtime`; enums:

```python
class PreflightOutcome(str, Enum):
    CONTINUE = "continue"
    TERMINAL = "terminal"
class CapabilityErrorCode(str, Enum):
    UNAVAILABLE = "capability_unavailable"
    SNAPSHOT_INCOMPATIBLE = "snapshot_incompatible"
```

Add optional bounded fields: `scenario_key`, `profile_fingerprint`,
`document_kind`, `normalizer_key/version`, validated `intake_type`,
`intake_type_known`, `action_key`, `action_known`, preflight outcome, capability
error. Add `CAPABILITY_UNAVAILABLE` worker error. No arbitrary mapping.

- [ ] Extend `ObservationContext` with explicit validated scenario/fingerprint
  bind fields. Keep `tool_name` temporarily until Task 8 migrates all emitters.

- [ ] Run `pytest tests/test_observability.py tests/test_observability_api.py -q`.

Expected: PASS and JSON contains no source/proposal/exception values.

---

### Task 6: Make enqueue and duplicate identity profile-driven

**Files:** Edit `app/domain/intake.py`, `job_repository.py`, inbound/API adapters;
test `tests/test_intake.py`, `test_job_repository.py`,
`test_inbound_webhook.py`.

- [ ] Add failing tests: freight document uses the compiled default; repair
  document rejects before normalizer call; generic body marker is `Document
  received.`; duplicate events use the existing job's trace, scenario, and
  fingerprint after the file profile changes.

- [ ] Run the three focused test files. Expected: FAIL because normalization
  currently precedes profile resolution and duplicate lookup returns only trace.

- [ ] Change document commands to `DocumentInput`. Resolve/compile once before
  normalization, require `default_inbound_document`, resolve its exact registry
  capability, and pass trusted binding fields. Refactor `_enqueue` to receive
  the resolved profile.

- [ ] Add tenant-scoped repository projection:

```python
@dataclass(frozen=True, slots=True)
class JobObservationIdentity:
    trace_id: UUID
    tenant_config_snapshot: dict[str, object]
    tenant_config_sha256: str
```

Compile this stored identity for duplicate observations; never expose it in API
bodies.

- [ ] Bind scenario/fingerprint to enqueue and document events. Log no profile
  body, document hash/text, payload, or exception.

- [ ] Run:

```powershell
pytest tests/test_intake.py tests/test_job_repository.py tests/test_inbound_webhook.py tests/test_observability_api.py -q
```

Expected: PASS with unchanged public status/body contracts.

---

### Task 7: Replace the fake document LLM with deterministic preflight

**Files:** Create `app/runtime/preflight.py`; edit `app/policy/models.py`,
`engine.py`, `app/runtime/factory.py`, `app/workers/agent_worker.py`; test
`tests/test_runtime.py`, `test_agent_worker.py`, `test_policy.py`.

- [ ] Add failing tests proving unreadable documents return a terminal result,
  with zero LLM factory, thread, tool, and approval calls; assert no
  `"rate_confirmation"` literal in common factory/policy files.

- [ ] Run the three test files. Expected: FAIL because
  `_UnreadableDocumentLLM` still exists.

- [ ] Decode `TrustedSource.document` as v2 or through the isolated legacy
  decoder. Add pure results:

```python
@dataclass(frozen=True, slots=True)
class ContinuePreflight:
    outcome: Literal["continue"] = "continue"
@dataclass(frozen=True, slots=True)
class TerminalPreflightResult:
    routing_status: RoutingStatus
    reason: Literal["document_unreadable", "capability_unavailable"]
    target_intake_type: str | None
    missing_required_fields: tuple[str, ...]
    document_kind: str | None
    extraction_error: DocumentExtractionError | None
```

- [ ] Make factory return `ReadyAgentRuntime | TerminalPreflightResult`. If
  hash-first persisted-profile compilation raises the typed unavailable-
  capability error, translate it to terminal `capability_unavailable` before
  constructing trusted tools. Otherwise run document preflight before creating
  HTTP client/provider/executor. Remove the fake LLM and emit one closed
  preflight event for either terminal path.

- [ ] Teach worker to persist unreadable as the existing awaiting-input safe
  result and capability-unavailable as non-retryable failure without consuming
  retry attempts. Keep policy's defensive document denial, deriving the trusted
  target intake type.

- [ ] Run:

```powershell
pytest tests/test_runtime.py tests/test_agent_worker.py tests/test_policy.py tests/test_documents.py -q
```

Expected: PASS; terminal branches make no provider/tool/approval call.

---

### Task 8: Drive prompts, tools, policy, approvals, and events from registry

**Files:** Edit `app/tools/executor.py`, `models.py`, `postgres.py`,
`app/runtime/factory.py`, `app/policy/*`, `app/observability/events.py`; test
tool/policy/runtime/approval suites.

- [ ] Add failing tests: prompts list only five available actions; `send_reply`
  is absent; unknown model action emits `action_known=False` without its raw
  name; descriptor supplies completeness/side-effect/pending version; unknown
  or mismatched approved action fails without side effect.

- [ ] Run:

```powershell
pytest tests/test_tools.py tests/test_policy.py tests/test_runtime.py tests/test_approvals.py -q
```

Expected: FAIL on hard-coded prompt/emitter/executor behavior.

- [ ] Build runtime definitions from registry descriptors plus the exact bound
  handlers for the five built-ins. Assert handler keys equal registry keys.
  Pass `CompiledTenantProfile` through trusted runtime; use its available set
  for prompts and registered set for policy.

- [ ] In approval execution, registry-resolve name, require exact pending
  version, validate descriptor arguments, then dispatch through the fixed
  internal map. Preserve tenant predicates and execute-once behavior.

- [ ] Replace every `ToolName/tool_name/_safe_tool_name` use with known
  `action_key/action_known`. Use descriptor side-effect metadata and remove
  hard-coded action-name side-effect sets.

- [ ] Run:

```powershell
pytest tests/test_tools.py tests/test_policy.py tests/test_runtime.py tests/test_approvals.py tests/test_approvals_api.py tests/test_postgres_approvals.py tests/test_postgres_tool_port.py -q
```

Expected: unit/API pass; PostgreSQL tests pass or explicitly skip when no DB.

---

### Task 9: Propagate original scenario identity through worker and approvals

**Files:** Edit factory, worker, approval repository/service/API; test worker
and approval suites.

- [ ] Add failing fixed-trace assertions that claim, preflight/agent, policy,
  tool, approval, and finish events all carry `repair_service` and the original
  profile SHA. Change the profile file before approval decision and assert the
  stored job identity still wins.

- [ ] Run worker/approval tests. Expected: FAIL where context currently carries
  only trace/tenant/job IDs.

- [ ] Return stored profile snapshot/hash in safe lease/approval projections;
  compile with tenant predicates and hash-first validation. Build one scenario
  context per job and pass it explicitly through all emitters. Add no DB column.

- [ ] Run:

```powershell
pytest tests/test_agent_worker.py tests/test_approvals.py tests/test_approvals_api.py tests/test_observability.py -q
```

Expected: PASS; events contain no profile body, source, or operator reason.

---

### Task 10: Extract the reusable eval core and scenario contracts

**Files:** Create `evals/core/*`, `evals/repair/*`, `evals/education/*`,
`tests/test_scenario_contracts.py`; edit freight runner/tests.

- [ ] Add failing tests for strict generic cases, real-loop scripted execution,
  fixed trace/scenario observation, and safe result keys only:

```python
SAFE_RESULT_KEYS = {
    "case_id", "scenario_key", "passed", "routing_status",
    "policy_reason", "action_key", "mismatch_codes",
}
```

Reject source/body/fields/proposal/rationale/events/logs/trace/secrets in JSON.

- [ ] Run scenario and freight model/runner tests. Expected: FAIL on missing
  `evals.core`.

- [ ] Move `ScriptedLLM`, recording policy, and generic real
  AgentLoop/policy/executor orchestration into core. Keep freight fixture,
  webhook, idempotency, category, comparison, and report logic freight-owned.

- [ ] Add six immutable cases per repair and education: success, missing
  fields, unknown intake, approval, tool failure, tenant isolation. Use typed
  proposals and synthetic content only.

- [ ] Add an in-test `future_scenario` with fake registered action and document
  normalizer. Prove compilation, v2 snapshot, preflight continue, action event,
  and safe projection without production registration.

- [ ] Run:

```powershell
pytest tests/test_scenario_contracts.py tests/test_freight_eval_runner.py tests/test_freight_eval_models.py -q
pytest evals/test_freight.py -q
```

Expected: all pass; exactly 50 freight cases and existing report schema/counts.

---

### Task 11: Update verified documentation

**Files:** Edit `README.md`, `AGENTS.md`; test observability/scenario contracts.

- [ ] Add failing assertions requiring profile-owned scenario identity,
  registered capabilities, provider-free unreadable preflight, local source-free
  events, and explicit absence of repair/education parsers and outbound delivery.

- [ ] Run documentation tests. Expected: only new assertions fail.

- [ ] Update README with verified product behavior and limits. Use `update-md`
  during execution to synchronize AGENTS.md with final code. Include extension
  procedures for profiles/actions/documents; make no generic OCR, SaaS,
  transcript, or dynamic-plugin claim.

- [ ] Run:

```powershell
pytest tests/test_observability.py tests/test_scenario_contracts.py -q
rg -n -i "phase 12|roadmap phase|interview|vacancy|tutorial|training context|learning project" README.md AGENTS.md
```

Expected: tests pass and `rg` has no matches.

---

### Task 12: Run final quality, compatibility, and privacy gates

**Files:** Verify every file changed by Tasks 1-11.

No formatter, linter, or static type checker is configured in `pyproject.toml`;
do not invent one for this plan.

- [ ] Run the complete configured suite with a fresh workspace-local temp path:

```powershell
$phase12Temp = '.pytest-tmp-phase12-universal-final'
if (Test-Path -LiteralPath $phase12Temp) {
    throw "Temporary test path already exists: $phase12Temp"
}
pytest -q --basetemp $phase12Temp
```

Expected: all collected tests pass. Report PostgreSQL skips separately; they
are not integration evidence.

- [ ] Run the freight gate and safe report renderer:

```powershell
$freightReport = Join-Path ([System.IO.Path]::GetTempPath()) 'integra-freight-universal-final.json'
pytest evals/test_freight.py -q --freight-eval-report $freightReport
python -m evals.freight.reporting --report $freightReport
```

Expected: exactly 50 cases pass with the existing 40-core/10-adversarial
counts, pass rate 1.0, and no source/event payload in output.

- [ ] Verify migration topology and configured migration tests:

```powershell
alembic heads
pytest tests/test_migrations.py -q
```

Expected: one head, `007_phase12_job_trace_id`; migration tests pass. If an
explicit reachable `DATABASE_URL` exists, also run:

```powershell
alembic upgrade head
pytest tests/test_postgres_integration.py tests/test_postgres_approvals.py tests/test_postgres_tool_port.py -q
```

Otherwise state that live PostgreSQL verification was not performed.

- [ ] Run architecture and privacy scans, inspecting every match:

```powershell
rg -n 'rate_confirmation|NormalizedRateConfirmationDocument|_UnreadableDocumentLLM' app/runtime app/policy app/workers app/observability
rg -n 'body|document_text|content_base64|source_excerpt|rationale_short|Authorization|X-API-Key|X-Inbound-Signature|tool_result|pending_action|decision_reason' app/observability
rg -n 'source|rationale|transcript|events|logs|trace_id|content_base64' evals/core evals/freight/reporting.py .github/workflows/ci.yml
```

Expected: the first command has no common-runtime freight matches; freight
literals exist only in the freight binding and legacy adapters. Other matches
must be schema prohibitions/tests, never serialized content fields or artifact
paths.

- [ ] Verify whitespace and final scope:

```powershell
git diff --check
git status --short
git diff --stat
git diff -- app alembic tests evals examples README.md .github pyproject.toml .gitattributes
```

Expected: whitespace check exits 0; no new dependency, external service,
trace-event table, outbound integration, production parser, or unrelated file.

---

### Task 13: Review the complete implementation before corrections

**Files:** Review the complete final diff plus the direct contents of ignored
`docs/` and `AGENTS.md` files.

No project-local code-review subagent exists. Use the available `code-review`
skill.

- [ ] Call the `code-review` skill on all changes made by this plan with this
  review context:

```text
Review universal intake observability for: profile-v2 safe scenario identity;
hash-first legacy profile/document upgrades; immutable server-owned action and
document registries; no executable YAML or dynamic imports; generic document
snapshots; deterministic unreadable/capability preflight before provider,
tools, or approvals; registered/allowed prompt actions only; registry-resolved
policy, tools, pending approvals, and side-effect metadata; original trace and
profile identity across duplicate intake, worker, and later approval work;
source-free typed events with no arbitrary metadata or raw unknown model names;
repair, education, freight, and synthetic future-capability deterministic
tests; unchanged 50-case freight corpus/report; no public API, tenant-isolation,
sync-loop/async-session, MAX_STEPS, repeated-tool, explicit-None, structured-
output, CI-secret, migration, SaaS, transcript, parser, or outbound regression.
```

Give the skill the complete diff and the approved spec/plan paths. Require
file/line evidence for every finding.

- [ ] Present all findings to the user before editing anything.

Expected: do not apply review corrections automatically. Discuss proposed
changes with the user first; if there are no findings, report that explicitly
with the verification evidence from Task 12.

---

## Self-review coverage

| Specification area | Tasks |
| --- | --- |
| Profile v2, safe keys, policy-only actions | 1 |
| Action/document registries | 2 |
| Generic/legacy document snapshots | 3 |
| Compiled profiles and hash-first upgrade | 4 |
| Universal typed events | 5, 6, 8, 9 |
| Trusted document selection and duplicate identity | 6 |
| Deterministic preflight, no fake proposal | 7 |
| Registry prompt/policy/tool/approval/side effects | 8 |
| Original job identity through later work | 9 |
| Reusable eval core, three scenarios, future fake | 10 |
| Unchanged freight corpus/report | 10 |
| Accurate product/contributor docs | 11 |
| Full tests, evals, migrations, privacy and scope gates | 12 |
| Evidence-based final code review before corrections | 13 |

No task adds a database migration beyond the existing trace migration, a new
production parser/action, executable YAML, SaaS/exporter, content transcript,
or live network/secret test. Stable names used throughout are
`SafeIdentifier`, `CompiledTenantProfile`, `ActionRegistry`,
`DocumentNormalizerRegistry`, `NormalizedDocument`,
`TerminalPreflightResult`, `scenario_key`, `profile_fingerprint`,
`document_kind`, and `action_key`.
