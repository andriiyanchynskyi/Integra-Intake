# Implementation plan: Phase 9 freight documents

> **For agents:** Use the `executing-plans` skill to implement this plan task by
> task. Steps use checkbox syntax (`- [ ]`) for tracking.

**Purpose:** Add a bounded, tenant-safe internal rate-confirmation document
path for plaintext and `pypdf` PDF-to-text extraction, with a versioned skill,
evidence-verified candidates, and deterministic `awaiting_input` failures.

**Architecture:** A pure synchronous normalizer turns one internal plaintext or
PDF input into a typed document snapshot before the existing idempotent job
enqueue. The persisted tenant-owned snapshot contains only normalized text or
a stable error code, never PDF bytes. At runtime, a document-only skill
instructs the existing structured-output provider; an evidence filter makes
only candidates excerpted from trusted text eligible for policy completeness.
Unreadable documents use a server-owned static proposal so the existing
bounded loop and deterministic router produce `awaiting_input` without an LLM
request.

**Key files/stack:** Python 3.12, Pydantic v2, `pypdf`, FastAPI unchanged,
async SQLAlchemy jobs, synchronous `AgentLoop`, `pytest`, and synthetic
fixtures only. No migration, external network, document endpoint, OCR,
storage, email, TMS/Odoo, MCP, evaluation CI, or observability work.

## File map

| Path | Responsibility |
| --- | --- |
| `pyproject.toml` | Add the single runtime PDF text dependency, `pypdf`. |
| `app/documents/models.py` | Strict internal input, normalized snapshot, stable parser error, and document limits. |
| `app/documents/normalizer.py` | Pure synchronous plaintext/PDF-to-text normalizer; no DB/network/logging. |
| `app/documents/__init__.py` | Narrow public exports for document domain code. |
| `app/policy/models.py` | Extend trusted source context with an optional typed document snapshot, one shared snapshot decoder, and verified-present-field policy input. |
| `app/domain/intake.py` | Internal document enqueue command and document-aware canonical idempotency hash; keep public request untouched. |
| `app/domain/job_repository.py` | Persist the closed optional document snapshot and return it on claim. |
| `app/runtime/factory.py` | Validate legacy/document snapshots, build source-data messages, select the document skill/static unreadable proposal. |
| `app/agent/models.py` | Add optional bounded source excerpts to proposal values without changing existing payload validity. |
| `app/skills/definitions.py`, `app/skills/__init__.py` | Define/export `extract_rate_confirmation.v1`. |
| `app/policy/engine.py`, `app/tools/executor.py` | Enforce unreadable-document routing and evidence-filtered candidate/policy fields. |
| `app/tools/postgres.py` | Reconstruct a document-aware trusted source for approved local actions, while never copying document text to a case. |
| `app/workers/agent_worker.py` | Persist only a safe final routing summary from the final tool result. |
| `examples/freight-broker.yaml` | Make rate-confirmation requirements explicitly equal to the specified document field set. |
| `evals/fixtures/docs/` | Eight synthetic plaintext/PDF fixture inputs plus closed JSON/YAML manifest; no real data. |
| `tests/test_documents.py` | Unit coverage for models, normalizer, and fixture corpus. |
| `tests/test_intake.py`, `tests/test_runtime.py`, `tests/test_skills.py` | Contract tests for enqueue/snapshot, runtime message construction, and skill metadata. |
| `tests/test_policy.py`, `tests/test_tools.py`, `tests/test_agent_worker.py` | Deterministic evidence, routing, side-effect, and persisted-outcome checks. |
| `tests/test_tenant_config.py` | Freight YAML requirement and unrelated-profile compatibility. |

## Invariants to preserve throughout

- `/v1/intake`, `/v1/cases`, and `/v1/approvals/{id}/decide` have no new
  request fields or response fields.
- Raw PDF bytes, base64, password, filename/path, parser exception text,
  transcript, rationale, confidence, or document excerpts never enter a case,
  approval, audit event, or persisted job result.
- Only a tenant-owned `AgentJob.source_snapshot` holds bounded normalized
  document text. Existing three-key snapshots remain valid.
- `AgentLoop` stays synchronous with `MAX_STEPS=8`, third-identical-tool stop,
  frozen transcript data, one tool call, and preserved `None` tool results.
- Policy, rather than the document or the LLM, decides allow/review/deny;
  existing risk, action, YAML, and Phase-8 approval precedence remains intact.
- Async sessions stay in the worker/database layer; no code added by this plan
  calls `asyncio.run()`.

---

## Task 1: Add the pure document contract and parser boundary

**Files:**
- Edit: `pyproject.toml`
- Create: `app/documents/__init__.py`
- Create: `app/documents/models.py`
- Create: `app/documents/normalizer.py`
- Test: `tests/test_documents.py`

- [x] **Step 1: Delegate document-boundary tests through `unit-test-writer`.**

  Ask the skill to add tests for: strict unknown-key rejection; one and only
  one of plaintext/PDF bytes; `text/plain` and `application/pdf` only;
  5 MiB input and 100,000-character normalized-text limits; LF normalization;
  SHA-256 of original bytes; malformed PDF; encrypted PDF; whitespace/textless
  PDF; and more than 20 pages. Include a `pypdf.PdfWriter` generated in-memory
  PDF and never a real file or network call.

- [x] **Step 2: Run the focused test before implementation.**

  Run: `pytest tests/test_documents.py -q`

  Expected: collection/import fails because `app.documents` and `pypdf` do not
  yet exist.

- [x] **Step 3: Add only the required dependency.**

  Add this project dependency exactly once:

  ```toml
  "pypdf>=5.0",
  ```

  Do not add OCR, a web framework extension, cloud SDK, task queue, or PDF
  service dependency.

- [x] **Step 4: Define strict document types and stable errors.**

  In `app/documents/models.py`, create frozen `extra="forbid"` Pydantic
  models/enums with these constants and wire values:

  ```python
  MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
  MAX_DOCUMENT_PAGES = 20
  MAX_DOCUMENT_TEXT_CHARS = 100_000

  class DocumentMediaType(str, Enum):
      TEXT = "text/plain"
      PDF = "application/pdf"

  class DocumentExtractionError(str, Enum):
      UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
      DOCUMENT_TOO_LARGE = "document_too_large"
      DOCUMENT_TEXT_TOO_LARGE = "document_text_too_large"
      PDF_MALFORMED = "pdf_malformed"
      PDF_ENCRYPTED = "pdf_encrypted"
      PDF_EMPTY = "pdf_empty"
      DOCUMENT_TEXT_EMPTY = "document_text_empty"
  ```

  `RateConfirmationDocumentInput` has nonblank bounded `channel` and
  `subject`, `body: str = ""`, `media_type`, and exactly one private payload
  field (`text` for `text/plain`, `content: bytes` for `application/pdf`). A
  model validator rejects mismatched media/payload combinations, blank text,
  and both/neither payloads. `NormalizedRateConfirmationDocument` has exactly
  `kind: Literal["rate_confirmation"]`, media type, SHA-256, parser marker,
  optional normalized text, and optional stable extraction error; its validator
  requires exactly one of text/error. Keep original bytes out of every result.

- [x] **Step 5: Implement the pure normalizer.**

  `DocumentNormalizer.normalize()` must have the following narrow shape:

  ```python
  class DocumentNormalizer:
      def normalize(
          self, value: RateConfirmationDocumentInput
      ) -> NormalizedRateConfirmationDocument: ...
  ```

  For plaintext, UTF-8 encode the supplied text only to calculate the digest,
  normalize `\r\n`/`\r` to `\n`, enforce the character limit, and return either
  text or `DOCUMENT_TEXT_EMPTY`. For a PDF, reject an oversize byte stream
  before `PdfReader`; construct `PdfReader(BytesIO(content), strict=True)`;
  return `PDF_ENCRYPTED` if `reader.is_encrypted`; reject a page count above
  the limit; join `page.extract_text() or ""`; normalize the result; map
  `PdfReadError`, `ValueError`, and `OSError` to `PDF_MALFORMED`; and map an
  empty result to `PDF_EMPTY`. Do not retain an exception object or message.

  `app/documents/__init__.py` exports only the input, result, error/media
  enums, limits, and normalizer.

- [x] **Step 6: Verify the parser contract.**

  Run: `pytest tests/test_documents.py -q`

  Expected: all focused document tests pass and none makes an HTTP request.

## Task 2: Persist the normalized document only in the tenant job snapshot

**Files:**
- Edit: `app/policy/models.py`
- Edit: `app/domain/intake.py`
- Edit: `app/domain/job_repository.py`
- Test: `tests/test_intake.py`
- Test: `tests/test_documents.py`

- [x] **Step 1: Delegate document enqueue/snapshot tests through `unit-test-writer`.**

  Require tests proving that a document command stores only the normalized
  model dump under `source_snapshot["document"]`; a body-only command keeps
  the exact legacy three keys; the canonical hash changes with document digest
  or text/error; the same tenant/key/document reuses one job; changed document
  with the same key raises `IdempotencyConflict`; and no snapshot/result
  representation contains the original bytes, filename, or parser exception.

- [x] **Step 2: Run the focused tests and confirm the missing behavior.**

  Run: `pytest tests/test_intake.py tests/test_documents.py -q`

  Expected: the new document-command tests fail because trusted source and job
  snapshots have no document member.

- [x] **Step 3: Extend only the trusted internal model.**

  Make `TrustedSource` frozen with this optional member:

  ```python
  document: NormalizedRateConfirmationDocument | None = None
  ```

  Keep `channel`, `subject`, and `body` unchanged. For a document-only
  command, create the body internally as the constant
  `"Rate confirmation document received."`; never substitute document text
  into `body`, because case creation persists the ordinary source body.

  Add an internal `EnqueueDocumentIntakeCommand` containing tenant ID/slug,
  idempotency key, `RateConfirmationDocumentInput`, and optional risk signals.
  It is not imported by `app/api/intake.py` and it does not change
  `CreateIntakeRequest`.

- [x] **Step 4: Normalize before entering the existing enqueue transaction.**

  Add `IntakeEnqueueService.enqueue_document(command)` that calls the injected
  or default `DocumentNormalizer` before `async with self.session.begin()`;
  converts it to the existing `EnqueueIntakeCommand` plus `TrustedSource`; and
  delegates to one shared enqueue method. Do not parse a PDF while a database
  transaction is open. Extend `canonical_intake_hash` with an optional
  canonical `document` dump only when one exists:

  ```python
  payload = {"body": source.body, "channel": source.channel, "subject": source.subject}
  if source.document is not None:
      payload["document"] = source.document.model_dump(mode="json")
  ```

  `JobRepository.create_job()` writes the same optional JSON-safe dump. It does
  not add a table/column/migration: `source_snapshot` is already JSONB.

- [x] **Step 5: Verify idempotency and compatibility.**

  Run: `pytest tests/test_intake.py tests/test_documents.py -q`

  Expected: new document tests and the original public intake tests pass with
  exactly the existing HTTP request/response shape.

## Task 3: Add the versioned document skill and structured evidence field

**Files:**
- Edit: `app/agent/models.py`
- Edit: `app/skills/definitions.py`
- Edit: `app/skills/__init__.py`
- Create: `evals/skills/extract_rate_confirmation.v1.yaml`
- Test: `tests/test_skills.py`
- Test: `tests/test_agent_loop.py`

- [x] **Step 1: Delegate skill/schema regression tests through `unit-test-writer`.**

  Add coverage for exact skill ordering/export, name
  `extract_rate_confirmation.v1`, fixture availability, forbidden unknown keys,
  no policy/tool/permission fields, and the fact that ordinary pre-Phase-9
  `ProposalValue` payloads remain valid without an excerpt. Add model tests
  rejecting a blank or >500-character `source_excerpt`.

- [x] **Step 2: Run the new focused tests first.**

  Run: `pytest tests/test_skills.py tests/test_agent_loop.py -q`

  Expected: failure because the fourth skill, its fixture, and the optional
  structured evidence member do not exist.

- [x] **Step 3: Extend `ProposalValue` compatibly.**

  Add this optional field without altering the `name`/`value` contract:

  ```python
  source_excerpt: str | None = Field(default=None, min_length=1, max_length=500)
  ```

  Existing generic skills and provider fixtures omit it. Do not add arbitrary
  mapping values, Markdown parsing, or a second provider schema.

- [x] **Step 4: Define the rate-confirmation skill.**

  Add strict input/output models:

  ```python
  class ExtractRateConfirmationInput(_SkillModel):
      document_text: StrictStr
      known_fields: list[StrictStr]

  class ExtractRateConfirmationOutput(_SkillModel):
      fields: list[ProposalValue]
      missing_required_fields: list[StrictStr]
  ```

  Define `EXTRACT_RATE_CONFIRMATION_V1` with an instruction that treats source
  as data, yields only tenant-catalog candidates, requires `source_excerpt`
  for every candidate, never fills absent values, and never authorizes tools,
  persistence, approval, or sending. Set its fixture to the new `evals/skills`
  file and append it to `ALL_SKILLS`. Export it from `app/skills/__init__.py`.
  The YAML fixture has one normal and one injection-in-source case; both carry
  `metadata.may_authorize: false`.

- [x] **Step 5: Verify skills and loop invariants.**

  Run: `pytest tests/test_skills.py tests/test_agent_loop.py -q`

  Expected: all skill tests pass and the loop still enforces its existing
  structured, frozen, bounded behavior.

## Task 4: Make policy completeness depend on document-verified candidates

**Files:**
- Edit: `app/policy/models.py`
- Edit: `app/policy/engine.py`
- Edit: `app/tools/executor.py`
- Test: `tests/test_policy.py`
- Test: `tests/test_tools.py`

- [x] **Step 1: Delegate evidence/routing tests through `unit-test-writer`.**

  Require a readable document proposal with complete valid excerpts to allow
  the registered `create_case`; an incomplete document whose model claims all
  fields but has absent/mismatched excerpts to return
  `awaiting_input/missing_required_fields`; unknown field names and blank
  values to be ignored; an injection signal to retain existing safety/review
  precedence; and an unreadable document to return
  `awaiting_input/document_unreadable` with zero handler, case, or approval
  calls.

- [x] **Step 2: Run policy/executor tests before the change.**

  Run: `pytest tests/test_policy.py tests/test_tools.py -q`

  Expected: document-mode candidate/evidence and unreadable-document tests
  fail because policy trusts all known proposal values equally.

- [x] **Step 3: Add an explicit policy input, not a policy exception.**

  Add optional server-computed `verified_present_fields: frozenset[str] | None`
  to `PolicyInput`. In `PolicyEngine.evaluate`, retain the current computation
  when it is `None`; otherwise use the supplied set after intersecting it with
  the tenant catalog. Before unknown intake/action handling but after the
  existing trusted/model risk condition, return:

  ```python
  PolicyOutcome(
      decision=RoutingDecision.DENY,
      status=RoutingStatus.AWAITING_INPUT,
      reason="document_unreadable",
      missing_required_fields=tuple(sorted(required_document_fields)),
  )
  ```

  only when `runtime.source.document.extraction_error` is present and its kind
  is `rate_confirmation`. Determine `required_document_fields` from the
  pinned tenant `rate_confirmation` configuration, not a hard-coded global
  list. Body-only routes do not enter this branch.

- [x] **Step 4: Filter accepted values and present names in one place.**

  In `app/tools/executor.py`, add a pure helper that, for document sources,
  accepts a proposal field only when all of these are true:

  ```python
  item.name in runtime.tenant_config.fields
  and proposal_value_is_present(item.value)
  and item.source_excerpt is not None
  and item.source_excerpt in runtime.source.document.text
  ```

  The helper returns both `dict[str, ToolData]` and `frozenset[str]`; use the
  dictionary for `_known_proposal_fields`/pending action/case tools and the set
  for `PolicyInput.verified_present_fields`. Keep the current generic behavior
  unchanged when no document exists. Do not use regex extraction, trust the
  model's missing list, or accept evidence outside normalized source text.

- [x] **Step 5: Verify deterministic policy and side-effect guards.**

  Run: `pytest tests/test_policy.py tests/test_tools.py -q`

  Expected: all old precedence tests pass; missing/malformed document paths
  are `awaiting_input`; no unverified candidate reaches a case or approval.

## Task 5: Rebuild document snapshots safely at runtime and preserve approval behavior

**Files:**
- Edit: `app/runtime/factory.py`
- Edit: `app/tools/postgres.py`
- Test: `tests/test_runtime.py`
- Test: `tests/test_postgres_approvals.py`

- [x] **Step 1: Delegate runtime/approval compatibility tests through `unit-test-writer`.**

  Add tests that: legacy snapshots still build; a document snapshot with an
  unknown key/wrong type fails before a provider client is constructed; a valid
  document source produces the initial messages in this order—base trust
  warning, existing generic skills, document skill, catalog, user envelope;
  the envelope contains normalized document text but no bytes/digest/tenant
  ID/risk signal; unreadable document construction uses a static local LLM;
  and the Phase-8 approved `create_case` reconstruction accepts the optional
  document snapshot but persists only ordinary source body and accepted fields.

- [x] **Step 2: Run focused tests before implementation.**

  Run: `pytest tests/test_runtime.py tests/test_postgres_approvals.py -q`

  Expected: document snapshot and document-skill message tests fail because
  factory/approval reconstruction still require exactly three snapshot keys.

- [x] **Step 3: Centralize snapshot decoding.**

  In `app/policy/models.py`, add a pure shared
  `trusted_source_from_snapshot(value)` function that accepts either exactly
  `{channel, subject, body}` or exactly `{channel, subject, body, document}`.
  Validate all primitive types and use
  `NormalizedRateConfirmationDocument.model_validate()` for `document`.
  Export/use it from `AgentRuntimeFactory` and
  `PostgresApprovalActionExecutor` rather than creating two parsers or an
  import cycle. Reject before `httpx.Client()` creation.

- [x] **Step 4: Add a static unreadable-document proposal without bypassing the loop.**

  In the factory, when the trusted document has `extraction_error`, supply an
  `LLMClient` implementation local to the factory that returns one closed
  `AgentProposal`: `intake_type="rate_confirmation"`, no fields,
  `priority="normal"`, no injection, short rationale `"document_unreadable"`,
  and a valid empty-argument `create_case` tool call. This client performs no
  HTTP request. The existing loop invokes the existing executor, whose
  server-owned policy gate returns the deterministic terminal result.

  For readable documents, retain the existing configured LLM client. Build the
  generic prompt list by filtering the document skill out of `ALL_SKILLS`, then
  append it only for a readable document source. Put user content in a fixed
  JSON envelope with `message` and `document_text` properties. Do not include
  hash, parser error, raw bytes, tenant ID, profile hash, or risk signals.

- [x] **Step 5: Verify factory and approval transaction behavior.**

  Run: `pytest tests/test_runtime.py tests/test_postgres_approvals.py -q`

  Expected: snapshot validation occurs before provider use; normal documents
  reach the provider through the established route; unreadable ones do not;
  tenant-scoped approval execution remains atomic and compatible.

## Task 6: Persist a safe routing summary and configure freight requirements

**Files:**
- Edit: `app/workers/agent_worker.py`
- Edit: `examples/freight-broker.yaml`
- Test: `tests/test_agent_worker.py`
- Test: `tests/test_tenant_config.py`

- [x] **Step 1: Delegate worker/profile tests through `unit-test-writer`.**

  Add one worker-result test for a terminal tool payload
  `{"decision": "deny", "status": "awaiting_input", "reason":
  "document_unreadable", "missing_required_fields": [...]}`. Assert the job
  result retains the existing `status`, `reason`, `steps`, and
  `final_response` fields and adds only safe `routing_status`,
  `routing_reason`, and a sorted list of missing field **names**. Assert no
  source/document/proposal/excerpt text is stored. Add a profile test that
  `rate_confirmation.required_fields` equals the required document set and
  repair/school profiles load unchanged.

- [x] **Step 2: Run the tests before implementation.**

  Run: `pytest tests/test_agent_worker.py tests/test_tenant_config.py -q`

  Expected: the routing-summary and full freight document-required-set tests
  fail against the current worker/profile.

- [x] **Step 3: Whitelist the final policy result in worker persistence.**

  In `_result_summary`, inspect only the last `MessageRole.TOOL` result. If it
  is a mapping with all of these JSON-safe keys/types—string `status`, string
  `reason`, and list of string `missing_required_fields`—append:

  ```python
  summary["routing_status"] = tool_result["status"]
  summary["routing_reason"] = tool_result["reason"]
  summary["missing_required_fields"] = sorted(tool_result["missing_required_fields"])
  ```

  Never add arbitrary tool-result values. The job lifecycle remains
  `succeeded` after a completed loop; `routing_status="awaiting_input"` is the
  durable domain outcome and no case/action is created.

- [x] **Step 4: Update only the freight profile.**

  Set the existing `rate_confirmation.required_fields` to:

  ```yaml
  [origin, destination, equipment, pickup_window, commodity, contact, quoted_rate, valid_until]
  ```

  Do not add fields to repair/language profiles, modify `FieldType`, or alter
  generic tenant validation.

- [x] **Step 5: Verify worker and profile contracts.**

  Run: `pytest tests/test_agent_worker.py tests/test_tenant_config.py -q`

  Expected: the job result is privacy-safe, `awaiting_input` is observable,
  and existing tenant profiles still validate.

## Task 7: Add the bounded synthetic fixture corpus and end-to-end deterministic tests

**Files:**
- Create: `evals/fixtures/docs/manifest.yaml`
- Create: `evals/fixtures/docs/01-complete-en.txt`
- Create: `evals/fixtures/docs/02-complete-uk.txt`
- Create: `evals/fixtures/docs/03-complete.pdf`
- Create: `evals/fixtures/docs/04-complete-two-page.pdf`
- Create: `evals/fixtures/docs/05-complete-no-rate-hint.txt`
- Create: `evals/fixtures/docs/06-complete-injection.pdf`
- Create: `evals/fixtures/docs/07-incomplete.txt`
- Create: `evals/fixtures/docs/08-malformed.pdf`
- Test: `tests/test_documents.py`
- Test: `tests/test_policy.py`
- Test: `tests/test_tools.py`

- [x] **Step 1: Delegate fixture-driven tests through `unit-test-writer`.**

  Require a manifest test for exactly eight files, synthetic-only fixture text,
  media type, expected parser outcome, intake type, verified field names, and
  expected missing names. Use fake LLMs producing predeclared candidates and
  excerpts. Verify five ordinary complete fixtures can reach `create_case`;
  the injection fixture retains review precedence; the incomplete and malformed
  fixtures produce `routing_status="awaiting_input"`, zero accepted invented
  field values, zero case calls, and zero approval calls.

- [x] **Step 2: Run fixture tests before adding fixtures.**

  Run: `pytest tests/test_documents.py tests/test_policy.py tests/test_tools.py -q`

  Expected: the corpus/manifest-specific tests fail because no
  `evals/fixtures/docs/` content exists.

- [x] **Step 3: Create the exact synthetic corpus.**

  Generate PDFs in the repository using `pypdf.PdfWriter` during implementation
  from fixed synthetic strings; do not download, scan, OCR, or copy real
  documents. The manifest must declare cases 1–8 exactly as listed in the
  approved design. Case 7 omits `valid_until` and one load field. Case 8 is a
  small non-PDF byte fixture labeled `application/pdf`. Keep every file under
  the 5 MiB/20-page limits. Include an injection sentence only in case 6 and
  never make it a directive to the test harness.

- [x] **Step 4: Implement only deterministic fixture orchestration.**

  Fixture tests call `DocumentNormalizer`, internal enqueue/runtime helpers,
  fake `LLMClient`, and `InMemoryTenantToolPort`; they do not call an external
  provider, a FastAPI endpoint, or PostgreSQL. Read the manifest with
  `yaml.safe_load`, validate its closed test model, and assert expected values
  rather than using an LLM-as-judge or pass@k loop.

- [x] **Step 5: Verify the Phase-9 behavior.**

  Run: `pytest tests/test_documents.py tests/test_intake.py tests/test_runtime.py tests/test_skills.py tests/test_policy.py tests/test_tools.py tests/test_agent_worker.py tests/test_tenant_config.py -q`

  Expected: all focused suites pass; malformed/incomplete documents are
  reproducibly `awaiting_input`; no test needs a service, provider key, real
  PDF source, or external network.

## Task 8: Run regression, scope, and quality gates

**Files:**
- Verify: all files changed by Tasks 1–7

- [x] **Step 1: Run the full project suite from a fresh command.**

  Run: `pytest -q`

  Expected: all unit/API tests pass. PostgreSQL-only tests pass with a
  configured `DATABASE_URL`; otherwise their existing explicit skips are
  reported as skips, never as database evidence.

- [x] **Step 2: Check whitespace and approved scope.**

  Run: `git diff --check; git status --short; git diff --stat; git diff -- app pyproject.toml examples evals tests docs`

  Expected: no whitespace error. The implementation contains only `pypdf`,
  internal document parser/models, snapshot/runtime/policy/tool/worker changes,
  one freight YAML change, synthetic fixtures, tests, and docs. Stop for user
  direction if the diff includes a public upload/email endpoint, OCR,
  cloud/object storage, TMS/Odoo, outbound delivery, MCP, eval CI,
  observability, LangChain, an applied-migration edit, or `asyncio.run()`.

- [x] **Step 3: Request independent review through the mandated `code-review` skill.**

  Provide the final diff and these non-negotiable constraints: tenant predicates
  on every persisted path; no PDF bytes/exception text/transcript in records;
  public API compatibility; evidence-only document candidates; deterministic
  `awaiting_input`; Phase-8 transaction/approval safety; no hidden event loop;
  and no scope expansion. Do not make corrections based on review findings;
  summarize them and discuss them with the user first.

## Plan self-review

| Specification requirement | Plan tasks |
| --- | --- |
| Internal plaintext/PDF boundary and `pypdf` error/limit behavior | 1–2 |
| Existing intake/job/runtime placement and API compatibility | 2, 5–6 |
| Trusted text vs untrusted candidate vs server policy | 3–5 |
| No binary/secrets/exception text in transcript/approval/audit/result | 1–2, 5–6, 8 |
| Versioned skill and structured outputs | 3, 5 |
| Freight YAML without generic profile breakage | 6 |
| Tenant isolation and Phase 6–8 constraints | 2, 4–6, 8 |
| Eight synthetic fixtures and two `awaiting_input` failures | 7 |
| No external integrations or broad document platform | every task; enforced in 8 |

No plan step contains a placeholder, introduces a client-facing document API,
or requires real external data. Tasks 1–7 produce one coherent, independently
testable Phase-9 slice; Task 8 supplies the required regression, scope, and
review gates.
