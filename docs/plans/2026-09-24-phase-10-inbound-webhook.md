# Implementation plan: Phase 10 signed email inbound webhook

> **For agents:** Use the `executing-plans` skill to implement this plan task by task. Steps use checkbox syntax (`- [ ]`) for tracking.

**Purpose:** Add one locally usable, signed email-like webhook that derives a
tenant on the server, normalizes at most one supported attachment, and enqueues
the existing tenant-scoped intake job without running an agent in the request.

**Architecture:** A strict webhook wire model is translated to a frozen
transport-neutral `InboundMessage` only after active API-key/HMAC
authentication.  `InboundIntakeService` converts that message to the existing
`TrustedSource` and `IntakeEnqueueService`; a PDF/plaintext attachment is
normalized with `asyncio.to_thread` before the enqueue transaction.  The
existing PostgreSQL worker, synchronous `AgentLoop`, structured provider
boundary, policy executor, and idempotency table remain the downstream path.

**Key files/stack:** FastAPI request dependencies, Pydantic v2 closed models,
Python stdlib `hmac`/`base64`, SQLAlchemy async sessions, existing Phase-9
`DocumentNormalizer`, pytest/pytest-asyncio.  No dependency, migration, or
configuration-key change is required.

---

## File map

| File | Change and single responsibility |
| --- | --- |
| `app/policy/models.py` | Extend the trusted source snapshot compatibly with optional sender metadata. |
| `app/domain/intake.py` | Include an optional sender in the canonical hash only when present. |
| `app/domain/job_repository.py` | Serialize optional sender in an otherwise unchanged trusted source snapshot. |
| `app/runtime/factory.py` | Put optional sender into the model's fixed source envelope; never expose provider IDs or bytes. |
| `app/inbound/models.py` | Closed webhook payload, short-lived decoded attachment, internal inbound message, bounds, and sanitized validation error. |
| `app/inbound/service.py` | Map one internal message to the existing enqueue service and normalize an attachment outside the transaction. |
| `app/inbound/__init__.py` | Export only the inbound public internal contracts. |
| `app/auth/inbound_webhook.py` | HMAC/timestamp primitives and the FastAPI dependency that authenticates an active tenant API key. |
| `app/auth/__init__.py` | Export the inbound authentication dependency and signing helper. |
| `app/api/inbound.py` | Thin `POST /v1/inbound/email/webhook` adapter and HTTP error mapping. |
| `app/main.py` | Register the new router under `/v1`; leave all existing routes unchanged. |
| `scripts/send_inbound_webhook.py` | Optional local signed sender; reads its credential from the environment and writes none. |
| `tests/test_intake.py` | Prove legacy snapshots/hashes stay exact and inbound sender affects new idempotency content. |
| `tests/test_runtime.py` | Prove sender reaches only the fixed source envelope and provider IDs/digests remain absent. |
| `tests/test_inbound_webhook.py` | Offline contract, HMAC, service, duplicate, privacy, and API adapter tests. |

The plan deliberately does not change `app/documents/*`, database models,
Alembic revisions, `.env.example`, or the existing `/v1/intake` schema.

## Task 1: Preserve legacy intake while adding trusted sender metadata

**Files:**
- Edit: `app/policy/models.py`
- Edit: `app/domain/intake.py`
- Edit: `app/domain/job_repository.py`
- Edit: `app/runtime/factory.py`
- Test: `tests/test_intake.py`
- Test: `tests/test_runtime.py`

- [ ] **Step 1: Write failing compatibility tests.**

Add a `test_trusted_source_sender_is_optional_and_changes_only_new_hash` test
to `tests/test_intake.py`.  It must assert that a legacy source still hashes
to exactly the existing JSON object and that two otherwise equal sources with
different nonempty `sender` values hash differently.  Add an async enqueue
test whose expected snapshot is exactly:

```python
{
    "channel": "email_webhook",
    "sender": "dispatcher@example.test",
    "subject": "Load",
    "body": "Need a truck",
}
```

The test must also assert that the original body-only enqueue still contains
only `channel`, `subject`, and `body`.

Add a runtime test with a source snapshot including the sender.  Assert that
the final user message is the following fixed JSON value and contains no
tenant ID, document digest, risk signal, or provider ID:

```python
{
    "body": "Need a truck",
    "channel": "email_webhook",
    "sender": "dispatcher@example.test",
    "subject": "Load",
}
```

- [ ] **Step 2: Run the focused tests and confirm they fail for missing sender support.**

Run:

```powershell
pytest tests/test_intake.py tests/test_runtime.py -q
```

Expected: the new tests fail because `TrustedSource` does not accept `sender`,
source snapshots reject the new key, and runtime envelopes omit it.  Existing
tests must continue to pass before the implementation.

- [ ] **Step 3: Extend only the optional internal snapshot shape.**

In `app/policy/models.py`, add sender as the final defaulted member and
recognize all four compatible snapshot shapes; do not relax arbitrary keys:

```python
@dataclass(frozen=True, slots=True)
class TrustedSource:
    channel: str
    subject: str
    body: str
    document: NormalizedRateConfirmationDocument | None = None
    sender: str | None = None


_SOURCE_KEYS = (
    {"channel", "subject", "body"},
    {"channel", "subject", "body", "document"},
    {"channel", "subject", "body", "sender"},
    {"channel", "subject", "body", "document", "sender"},
)
```

`trusted_source_from_snapshot` must require string values for channel/subject/
body and, when present, a string sender.  It must still validate `document`
with `NormalizedRateConfirmationDocument.model_validate`, convert all decode
errors to `RuntimeError("source snapshot is invalid")`, and preserve the
legacy/document-only shapes verbatim.

In `app/domain/job_repository.py`, append the key only when the value is not
`None`:

```python
if source.sender is not None:
    snapshot["sender"] = source.sender
if source.document is not None:
    snapshot["document"] = source.document.model_dump(mode="json")
```

In `canonical_intake_hash`, start from the legacy three-key mapping and add
the same optional keys in this order before canonical JSON encoding:

```python
if source.sender is not None:
    payload["sender"] = source.sender
if source.document is not None:
    payload["document"] = source.document.model_dump(mode="json")
```

In `AgentRuntimeFactory._initial_messages`, build the current body/document
source envelope first, then append `sender` only when non-`None`.  Never add a
provider ID, attachment bytes/base64, filename, document digest, tenant ID, or
risk signals to this envelope.

- [ ] **Step 4: Run the focused compatibility tests.**

Run:

```powershell
pytest tests/test_intake.py tests/test_runtime.py -q
```

Expected: all tests pass.  In particular, the old canonical hash and the exact
three-key source snapshot remain unchanged.

## Task 2: Define the closed inbound contracts and common enqueue service

**Files:**
- Create: `app/inbound/__init__.py`
- Create: `app/inbound/models.py`
- Create: `app/inbound/service.py`
- Test: `tests/test_inbound_webhook.py`

- [ ] **Step 1: Write failing pure-contract and service tests.**

Create `tests/test_inbound_webhook.py` with fixtures for a UUID tenant and a
`_RecordingEnqueueService` whose async `enqueue(command)` retains the command
and returns `EnqueueResult(job_id=..., created=True)`.  Add tests that prove:

- a strict payload with a blank subject normalizes only its subject to
  `"(no subject)"`, receives `channel == "email_webhook"`, and receives its
  tenant ID/slug from the constructor, not payload data;
- payload fields `tenant_id`, `channel`, `filename`, and any other unknown
  key raise one sanitized `InvalidInboundPayload` type;
- a body-only message calls the existing enqueue service exactly once with
  `EnqueueIntakeCommand`, source sender, and idempotency key
  `email_webhook:provider-123`;
- no-attachment plus blank body, invalid base64, invalid UTF-8 plaintext,
  empty decoded document, unsupported MIME, a decoded payload above
  `MAX_DOCUMENT_BYTES`, or two attachments raise `InvalidInboundPayload`;
- a PDF attachment enters a fake normalizer only through the injected
  `to_thread` seam, produces a source with the normalized document, preserves
  the email body/sender, and has no `bytes` or `content_base64` in the command
  snapshot representation;
- a normalizer result with `extraction_error="pdf_malformed"` is still
  enqueued as a document source rather than rejected by the webhook contract.

Use a small fake normalizer returning a known
`NormalizedRateConfirmationDocument`; no test calls a network or a real
provider.

- [ ] **Step 2: Run the new tests and confirm the module is absent.**

Run:

```powershell
pytest tests/test_inbound_webhook.py -q
```

Expected: collection fails because `app.inbound` does not exist.

- [ ] **Step 3: Implement strict payload and internal message models.**

Create `app/inbound/models.py` with these public constants and classes:

```python
MAX_INBOUND_WEBHOOK_BODY_BYTES = 8 * 1024 * 1024
MAX_INBOUND_BODY_CHARS = 100_000
MAX_PROVIDER_ID_CHARS = 200
MAX_BASE64_ATTACHMENT_CHARS = 4 * ((MAX_DOCUMENT_BYTES + 2) // 3)


class InvalidInboundPayload(ValueError):
    """A caller-visible invalid inbound envelope without source details."""


@dataclass(frozen=True, slots=True)
class InboundAttachment:
    media_type: DocumentMediaType
    text: str | None = None
    content: bytes | None = None

    def as_document_input(self, *, channel: str, subject: str, body: str) -> RateConfirmationDocumentInput:
        return RateConfirmationDocumentInput(
            channel=channel, subject=subject, body=body,
            media_type=self.media_type, text=self.text, content=self.content,
        )


@dataclass(frozen=True, slots=True)
class InboundMessage:
    tenant_id: UUID
    tenant_slug: str
    channel: str
    from_addr: str
    subject: str
    body: str
    attachments: tuple[InboundAttachment, ...]
    provider_id: str
```

Use frozen Pydantic models with `ConfigDict(extra="forbid", frozen=True,
strict=True)` for the wire envelope and attachment.  The wire envelope accepts
only `provider_id`, `from_addr`, `subject`, `body`, and `attachments`; it has
at most one attachment.  It validates nonblank provider ID/from address,
subject length 500, body length 100,000, and attachment-only blank body.  Its
`to_message(tenant_id, tenant_slug)` method must decode base64 with
`base64.b64decode(value, validate=True)`, reject values longer than
`MAX_BASE64_ATTACHMENT_CHARS` before decoding, reject decoded values over
`MAX_DOCUMENT_BYTES`, decode `text/plain` as strict UTF-8, and create the
server-owned message with `channel="email_webhook"`.  Catch JSON/Pydantic/
base64/Unicode validation failures at the public parser boundary and raise
only `InvalidInboundPayload("invalid inbound payload")` without chaining
source text into the message.

Export the message, attachment, payload parser, error, and bounds from
`app/inbound/__init__.py`; do not export an HTTP provider-specific API from a
generic application package.

- [ ] **Step 4: Implement the one-message application service.**

Create `app/inbound/service.py` with a dependency-injectable normalizer and
thread seam:

```python
class InboundIntakeService:
    def __init__(
        self,
        enqueue_service: IntakeEnqueueService,
        *,
        normalizer: DocumentNormalizer | None = None,
        to_thread: Callable[..., Awaitable[NormalizedRateConfirmationDocument]] = asyncio.to_thread,
    ) -> None: ...

    async def enqueue(self, message: InboundMessage) -> EnqueueResult:
        key = f"email_webhook:{message.provider_id}"
        document = None
        if message.attachments:
            input_value = message.attachments[0].as_document_input(
                channel=message.channel,
                subject=message.subject,
                body=message.body,
            )
            document = await self._to_thread(self._normalizer.normalize, input_value)
        source = TrustedSource(
            channel=message.channel,
            sender=message.from_addr,
            subject=message.subject,
            body=message.body,
            document=document,
        )
        return await self._enqueue_service.enqueue(
            EnqueueIntakeCommand(
                tenant_id=message.tenant_id,
                tenant_slug=message.tenant_slug,
                source=source,
                idempotency_key=key,
            )
        )
```

Use a typed wrapper if necessary so the `asyncio.to_thread` annotation does
not weaken the runtime contract.  Never call `enqueue_document()` here: that
internal document-only helper intentionally replaces the body, while this
email adapter must preserve its bounded body.  Never persist or return the
incoming attachment object after normalization.

- [ ] **Step 5: Run contract/service tests.**

Run:

```powershell
pytest tests/test_inbound_webhook.py tests/test_intake.py tests/test_documents.py -q
```

Expected: the new contract/service tests pass, Phase-9 document tests remain
green, and no test needs an SMTP server, provider account, or network.

## Task 3: Add pure signed-webhook authentication with server-side tenant lookup

**Files:**
- Create: `app/auth/inbound_webhook.py`
- Edit: `app/auth/__init__.py`
- Test: `tests/test_auth.py`
- Test: `tests/test_inbound_webhook.py`

- [ ] **Step 1: Write failing signing and dependency tests.**

In `tests/test_inbound_webhook.py`, add pure tests for a fixed raw body and
fixed UTC clock proving that `sign_inbound_webhook(api_key, timestamp, body)`
returns a `v1=` SHA-256 HMAC and that verification rejects changed body,
changed timestamp, malformed signature, non-integer timestamp, and timestamps
more than 300 seconds in either direction.  Assert verification uses no
exception text or API key in its failure result.

Extend `tests/test_auth.py`'s small FastAPI fixture with an endpoint depending
on `get_current_inbound_tenant`.  Verify an active valid key plus valid
signature resolves the joined tenant.  Verify missing/malformed/inactive/
unknown keys and missing/bad/stale signatures return only 401.  Include a
request whose payload claims a different tenant ID; its tenant response must
still be the one associated with the key.  The `StatementAwareSession` may
reuse the existing service-key query branch; do not add a new database table
or credential type.

- [ ] **Step 2: Run authentication tests and confirm failure.**

Run:

```powershell
pytest tests/test_auth.py tests/test_inbound_webhook.py -q
```

Expected: imports fail because the signing helper and inbound tenant dependency
do not yet exist.

- [ ] **Step 3: Implement the dependency without storing any raw secret.**

Create `app/auth/inbound_webhook.py`.  Keep the cryptographic primitive pure:

```python
SIGNATURE_VERSION = "v1"
MAX_SIGNATURE_AGE_SECONDS = 300

def signature_payload(timestamp: int, body: bytes) -> bytes:
    return b"v1." + str(timestamp).encode("ascii") + b"." + body

def sign_inbound_webhook(api_key: str, timestamp: int, body: bytes) -> str:
    digest = hmac.new(
        api_key.encode("utf-8"), signature_payload(timestamp, body), hashlib.sha256
    ).hexdigest()
    return f"v1={digest}"
```

`get_current_inbound_tenant` receives `Request`, `X-API-Key`,
`X-Inbound-Timestamp`, `X-Inbound-Signature`, and the normal async DB session.
Before reading the body, reject a valid integer `Content-Length` greater than
`MAX_INBOUND_WEBHOOK_BODY_BYTES` with 413.  Read `await request.body()`, check
the actual byte length again, validate timestamp/signature using
`hmac.compare_digest`, then call the existing active service-key tenant lookup
logic.  Refactor that lookup into one private helper in `api_keys.py` if needed
so `get_current_tenant` retains precisely its current behavior.  Every invalid
credential/signature/timestamp condition raises:

```python
HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid inbound webhook")
```

Do not put the raw key, signed body, expected signature, or timestamp value in
logs, exceptions, dataclasses, a database row, or a returned object.  Export
only `get_current_inbound_tenant`, `sign_inbound_webhook`, and stable public
limits/constants from `app/auth/__init__.py`.

- [ ] **Step 4: Run authentication tests.**

Run:

```powershell
pytest tests/test_auth.py tests/test_inbound_webhook.py -q
```

Expected: valid signed requests resolve only their API-key tenant; all invalid
auth variants are generic 401; no test fixture requires a raw key to be stored
outside its in-memory provisioning tuple.

## Task 4: Expose the thin webhook route and preserve API contracts

**Files:**
- Create: `app/api/inbound.py`
- Edit: `app/main.py`
- Test: `tests/test_inbound_webhook.py`
- Test: `tests/test_intake.py`

- [ ] **Step 1: Write failing API adapter tests.**

Use `TestClient(app)` dependency overrides for the database session,
`get_current_inbound_tenant`, and an injectable inbound service factory.  Add
tests with an actual signed raw JSON body that assert:

- `POST /v1/inbound/email/webhook` returns 202 and
  `{"job_id": "<uuid>", "status": "queued"}` for body-only and one-PDF
  payloads;
- the dependency-derived tenant and fixed `email_webhook` channel are what the
  recording service receives for every valid payload; separate requests that
  include `tenant_id` or `channel` are rejected as unknown fields and never
  reach the service;
- `application/json; charset=utf-8` is accepted, a non-JSON media type is
  415, invalid JSON/payload/base64 is 422, and raw oversize is 413;
- a fake enqueue `IdempotencyConflict` becomes exactly 409 with
  `"Idempotency-Key conflicts with request"`, a profile-unavailable error is
  503 with `"Tenant profile unavailable"`, and other input `ValueError`s are
  sanitized 422 responses;
- existing `POST /v1/intake` still rejects an omitted `Idempotency-Key` and
  keeps its response contract.

- [ ] **Step 2: Run API tests and confirm the route is missing.**

Run:

```powershell
pytest tests/test_inbound_webhook.py tests/test_intake.py -q
```

Expected: the new route tests receive 404 or fail dependency imports; legacy
intake tests still pass.

- [ ] **Step 3: Implement only an adapter route.**

Create `app/api/inbound.py` with a router prefix `/inbound/email` and the
following route shape:

```python
@router.post("/webhook", response_model=IntakeAcceptedResponse, status_code=status.HTTP_202_ACCEPTED)
async def receive_email_webhook(
    request: Request,
    tenant: Tenant = Depends(get_current_inbound_tenant),
    service: InboundIntakeService = Depends(get_inbound_intake_service),
) -> IntakeAcceptedResponse:
    if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
        raise HTTPException(status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, detail="Unsupported media type")
    try:
        payload = parse_webhook_email_payload(await request.body())
        message = payload.to_message(tenant.id, tenant.slug)
        result = await service.enqueue(message)
    except InvalidInboundPayload as error:
        raise HTTPException(status_code=422, detail="Invalid inbound payload") from error
    except IdempotencyConflict as error:
        raise HTTPException(status_code=409, detail="Idempotency-Key conflicts with request") from error
    except TenantProfileUnavailableError as error:
        raise HTTPException(status_code=503, detail="Tenant profile unavailable") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail="Invalid inbound payload") from error
    return IntakeAcceptedResponse(job_id=result.job_id, status="queued")
```

`get_inbound_intake_service` builds exactly one `IntakeEnqueueService` with
the injected async session and `TenantProfileResolver(settings.tenant_profiles_directory)`,
then wraps it in `InboundIntakeService`.  Reuse `IntakeAcceptedResponse` from
`app.api.intake`; do not fork the response schema.  Register this router in
`app/main.py` under `/v1`.  The route must not import a worker, provider,
`AgentLoop`, or direct document storage.

- [ ] **Step 4: Run adapter and public-contract tests.**

Run:

```powershell
pytest tests/test_inbound_webhook.py tests/test_intake.py tests/test_auth.py -q
```

Expected: the new endpoint maps only its closed contract to a queued job and
all legacy endpoints retain their current tests/statuses.

## Task 5: Add a bounded local sender demonstration

**Files:**
- Create: `scripts/send_inbound_webhook.py`
- Test: `tests/test_inbound_webhook.py`

- [ ] **Step 1: Write a failing script-contract test.**

Add a pure test that imports the script helpers without sending HTTP.  Given a
fixed clock, temporary plaintext attachment, and key supplied through
`INBOUND_WEBHOOK_API_KEY`, assert that `build_request` produces only the
approved envelope keys, uses `text/plain`, base64 encodes the file, and sets
the exact `X-API-Key`, `X-Inbound-Timestamp`, and
`X-Inbound-Signature` calculated by `sign_inbound_webhook`.  Assert the raw
key is neither printed by the helper nor written to a file.

- [ ] **Step 2: Run the script test and confirm it fails.**

Run:

```powershell
pytest tests/test_inbound_webhook.py -q
```

Expected: import fails because the local sender script does not exist.

- [ ] **Step 3: Implement the local-only sender.**

Create `scripts/send_inbound_webhook.py` using only the standard library plus
the project auth helper.  Accept `--url`, `--provider-id`, `--from-addr`,
`--subject`, `--body`, and optional `--attachment`.  Require
`INBOUND_WEBHOOK_API_KEY` from the environment.  Permit attachment paths only
with `.txt` or `.pdf`, derive their exact allowed MIME type, and let the server
remain the authoritative size validator.  Build JSON with compact UTF-8 bytes,
use `int(time.time())`, sign exact bytes, and send through
`urllib.request.Request`.  Print only HTTP status and response body; never
print request headers, the key, base64, or attachment content.

The manual demonstration command is:

```powershell
$env:INBOUND_WEBHOOK_API_KEY = '<key printed once by local provisioning>'
python scripts/send_inbound_webhook.py --url http://127.0.0.1:8000/v1/inbound/email/webhook --provider-id local-demo-1 --from-addr dispatcher@example.test --subject 'Rate confirmation' --body 'Please process the attachment.' --attachment .\evals\fixtures\docs\01-complete-en.txt
```

Run that same command twice only after the deterministic suite passes; both
responses must contain the same queued job ID.  This is manual supplemental
evidence, never a test dependency.

- [ ] **Step 4: Run the script's offline test.**

Run:

```powershell
pytest tests/test_inbound_webhook.py -q
```

Expected: all script assertions pass without opening a network connection.

## Task 6: Prove duplicate delivery, document privacy, and downstream invariants

**Files:**
- Edit: `tests/test_inbound_webhook.py`
- Edit: `tests/test_intake.py`
- Edit: `tests/test_runtime.py`
- Verify: `tests/test_documents.py`
- Verify: `tests/test_agent_worker.py`
- Verify: `tests/test_agent_loop.py`

- [ ] **Step 1: Add deterministic end-to-end seam tests.**

Use the existing `_IntakeSession` / `_ProfileResolver` pattern from
`tests/test_intake.py` with two `InboundMessage` objects.  Assert that two
otherwise identical messages for the same tenant and `provider_id` return the
same `job_id`, create exactly one `AgentJob` and one `IdempotencyRecord`, and
store the key `email_webhook:<provider_id>`.  Change each of body, sender, and
normalized document in separate parametrized variants while retaining that
provider ID; each must raise `IdempotencyConflict`.  Use the same provider ID
for a second tenant and assert a distinct job is allowed.

For an attachment job, inspect the source snapshot, runtime initial messages,
and `AgentWorker._result_summary` input.  Assert all of these are absent from
their representations: decoded attachment bytes, base64, filename,
`provider_id`, parser exception text, and raw API key.  The normalized
document snapshot may contain its existing safe SHA-256 digest, but the digest
must remain absent from prompts, transcripts, and worker results.  Assert that
normalizer error metadata remains the existing stable enum and that the
existing worker redacts document final response.

Run a fake `LLMClient` and `InMemoryTenantToolPort` against the reconstructed
body-only and document sources.  Assert it uses the unchanged synchronous
loop/policy path; `MAX_STEPS` remains 8, a third identical call remains
`REPEATED_TOOL`, and a `None` tool result remains present in the transcript.
Do not duplicate the existing agent-loop tests: add only an inbound-created
source regression that invokes their existing public interfaces.

- [ ] **Step 2: Run the new regression tests after Tasks 1-5.**

Run:

```powershell
pytest tests/test_inbound_webhook.py tests/test_intake.py tests/test_runtime.py tests/test_agent_worker.py tests/test_agent_loop.py -q
```

Expected: all new assertions pass after the preceding TDD tasks, and existing
Phase-9 and loop invariants do not regress.  If an assertion fails, stop at
that assertion and correct only the Task 1-4 implementation that owns the
broken contract.  The correction must retain these explicit properties:

```text
no raw binary/base64/filename/native payload in durable records or prompts
no tenant ID from webhook JSON
no document normalization inside an open database transaction
no new table, migration, dependency, worker invocation, or provider call
no change to MAX_STEPS, repeated-tool, None-result, policy, or public /v1/intake contracts
```

- [ ] **Step 3: Run the full focused Phase-10 regression.**

Run:

```powershell
pytest tests/test_inbound_webhook.py tests/test_auth.py tests/test_intake.py tests/test_documents.py tests/test_runtime.py tests/test_agent_worker.py tests/test_agent_loop.py tests/test_policy.py tests/test_tools.py -q
```

Expected: all selected suites pass without a real provider, secret, SMTP/IMAP
server, or external network.  The existing fixture corpus continues to prove
document extraction/policy behavior.

## Plan self-review

| Approved specification requirement | Planned task(s) |
| --- | --- |
| One signed local webhook, no IMAP/OAuth/provider SaaS | 2-5 |
| Internal `InboundMessage` with server-derived tenant/channel | 2, 3, 4 |
| HMAC authenticity, freshness, replay/duplicate behavior | 3, 4, 6 |
| Existing idempotency keyed by provider ID with conflict detection | 1, 2, 6 |
| One bounded plaintext/PDF attachment through Phase 9 | 2, 4, 6 |
| No raw binary in job/transcript/audit/approval/result | 1, 2, 6 |
| Public API compatibility and tenant isolation | 1, 3, 4, 6 |
| Existing worker/agent-loop/policy invariants | 1, 6 |
| Offline deterministic verification plus optional local proof | 2-6 |

The plan has no placeholders, introduces no unapproved external integration,
and has explicit names/contracts for every later reference.  The project has
no configured linter, formatter, or type checker; its defined automated gates
are pytest and the repository whitespace check.

## Task 7: Run final regression and repository quality gates

**Files:**
- Verify: all files changed by Tasks 1-6

- [ ] **Step 1: Run the complete configured test suite from a fresh command.**

Run:

```powershell
pytest -q
```

Expected: all unit/API tests pass.  PostgreSQL-only tests may report their
existing explicit skips when `DATABASE_URL` is absent; a skip is not evidence
that a database integration passed.  Stop execution and report the exact
failing test if any non-PostgreSQL test fails; do not suppress, xfail, or
delete it.

- [ ] **Step 2: Check whitespace and inspect the approved scope.**

Run:

```powershell
git diff --check
git status --short
git diff --stat
git diff -- app scripts tests docs pyproject.toml .env.example alembic
```

Expected: `git diff --check` has no output; the changed paths are limited to
the approved inbound source/auth/adapter/script/test/docs work.  Stop and ask
the user before proceeding if the diff adds a dependency, migration, config
key, IMAP/Gmail/Mailgun/Resend integration, outbound delivery, storage,
MCP, worker invocation from HTTP, LangChain, or an unrelated change.

## Task 8: Obtain independent review of the final Phase-10 diff

**Files:**
- Review: all files changed by Tasks 1-7

- [ ] **Step 1: Call the `code-review` skill on the final diff.**

No project-local code-review subagent exists in `.agents`, `.codex`, or the
available local agent directory, so call the `code-review` skill on the changes
made by this plan.  Supply the complete final diff and this review context:

```text
Review for tenant isolation on every persisted/query path; server-only tenant
routing; HMAC compare_digest and timestamp/body limits; provider-ID reuse via
existing tenant idempotency; no raw attachment bytes/base64/filename/parser
exceptions in jobs, prompts, transcript, audit, approval, or result; no inline
worker/agent run; unchanged /v1/intake and agent-loop invariants; no migration,
provider SaaS, IMAP/OAuth, storage, outbound delivery, MCP, LangChain, or
other Phase expansion.
```

Do not make corrections based on review findings.  Summarize them and discuss
them with the user first.
