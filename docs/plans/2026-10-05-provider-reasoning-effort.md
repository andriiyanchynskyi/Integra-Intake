# Implementation plan: Provider reasoning effort

> **For agents:** Use the executing-plans skill to implement this plan task by task. Steps use checkbox syntax (`- [ ]`) for tracking.

**Purpose:** Add optional `LLM_REASONING_EFFORT` configuration and expose unsupported provider requests through a stable, source-free job error.

**Architecture:** `Settings` validates the closed Chat Completions value set and converts an empty environment value to `None`. `AgentRuntimeFactory` passes the option to `OpenAICompatibleLLMClient`, which omits the payload field when unset. The worker converts non-retryable provider `4xx` responses into `provider_request_rejected`; existing retry and side-effect uncertainty rules remain authoritative.

**Key files/stack:** Python 3.12, Pydantic settings, httpx, async worker, FastAPI job projection, Docker Compose, pytest.

**Design:** `docs/specs/2026-10-05-provider-reasoning-effort-design.md`

---

## File map

- `app/core/config.py`: validate and normalize `LLM_REASONING_EFFORT`.
- `app/providers/openai_compatible.py`: add the optional Chat Completions payload field.
- `app/runtime/factory.py`: pass the setting into the default provider adapter.
- `app/workers/agent_worker.py`: classify non-retryable provider rejections.
- `app/observability/events.py`: add the stable public worker error code.
- `tests/test_openai_compatible_provider.py`: cover settings and provider payload behavior.
- `tests/test_runtime.py`: cover runtime-to-provider propagation.
- `tests/test_agent_worker.py`: cover retryable and non-retryable HTTP classifications.
- `tests/test_jobs_api.py`: cover safe projection of the new error code.
- `tests/test_demo_compose.py`: enforce worker-only environment propagation.
- `.env.example`, `docker-compose.yml`, `README.md`: document and deploy the optional setting.

### Task 1: Validate and send optional reasoning effort

**Files:**

- Edit: `app/core/config.py`
- Edit: `app/providers/openai_compatible.py`
- Edit: `app/runtime/factory.py`
- Test: `tests/test_openai_compatible_provider.py`
- Test: `tests/test_runtime.py`

- [ ] **Step 1: Load the `unit-test-writer` skill before adding tests**

Read `C:\Users\Andriy\.codex\skills\unit-test-writer\SKILL.md` and apply its test-design rules. Work in the primary agent because this session does not authorize subagent delegation.

- [ ] **Step 2: Add failing settings and payload tests**

Extend `tests/test_openai_compatible_provider.py` with `ValidationError` and these cases:

```python
from pydantic import ValidationError


@pytest.mark.parametrize(
    "value",
    ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
)
def test_settings_accept_supported_reasoning_effort(value: str) -> None:
    assert Settings(llm_reasoning_effort=value).llm_reasoning_effort == value


@pytest.mark.parametrize("value", ["", "   "])
def test_settings_treat_empty_reasoning_effort_as_unset(value: str) -> None:
    assert Settings(llm_reasoning_effort=value).llm_reasoning_effort is None


def test_settings_reject_unknown_reasoning_effort() -> None:
    with pytest.raises(ValidationError):
        Settings(llm_reasoning_effort="very_high")
```

In `test_valid_structured_proposal_uses_openai_compatible_contract`, add this assertion inside the mock handler:

```python
assert "reasoning_effort" not in payload
```

Add a retry test that proves both provider attempts receive the configured value:

```python
def test_configured_reasoning_effort_is_sent_on_validation_retry() -> None:
    requests: list[dict[str, object]] = []
    responses = iter(
        [
            _provider_response("{invalid"),
            _provider_response(json.dumps(VALID_PROPOSAL)),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=next(responses))

    with _client(httpx.MockTransport(handler)) as http_client:
        OpenAICompatibleLLMClient(
            base_url="https://provider.example/v1",
            api_key="dummy-secret",
            model="test-model",
            reasoning_effort="low",
            client=http_client,
        ).complete(_messages())

    assert [request["reasoning_effort"] for request in requests] == ["low", "low"]
```

Update `test_agent_runtime_factory_binds_trace_to_default_provider` in `tests/test_runtime.py` to construct the factory with an explicit setting and assert propagation:

```python
runtime_settings = Settings(llm_reasoning_effort="high")
runtime = AgentRuntimeFactory(
    object(),
    runtime_settings=runtime_settings,
    observer=observer,
).build(job, WorkerAsyncGateway(owner_loop))

assert provider.kwargs["reasoning_effort"] == "high"
```

- [ ] **Step 3: Run the focused tests and confirm the expected failures**

Run:

```powershell
pytest tests/test_openai_compatible_provider.py tests/test_runtime.py -q
```

Expected: failures report the missing `llm_reasoning_effort` setting, the unexpected provider constructor argument, or the absent runtime keyword. Existing unrelated cases remain green.

- [ ] **Step 4: Add the validated setting**

Update `app/core/config.py`:

```python
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


ReasoningEffort = Literal[
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
]


class Settings(BaseSettings):
    """Application configuration loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql+asyncpg://integra:integra@localhost:5432/integra"
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-5.4-mini-2026-03-17"
    llm_reasoning_effort: ReasoningEffort | None = None
    app_env: str = "development"
    tenant_profiles_directory: str = "examples"
    worker_poll_interval_seconds: float = Field(default=0.5, gt=0)
    worker_lease_seconds: int = Field(default=60, ge=1)
    worker_max_retries: int = Field(default=4, ge=0)
    approval_timeout_seconds: int = Field(default=86_400, ge=1)

    @field_validator("llm_reasoning_effort", mode="before")
    @classmethod
    def empty_reasoning_effort_is_unset(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value


settings = Settings()
```

Place `llm_reasoning_effort` next to `openai_model`. Do not add a default effort such as `none` or `low`; `None` preserves provider defaults.

- [ ] **Step 5: Add conditional provider payload serialization**

Extend `OpenAICompatibleLLMClient.__init__` in `app/providers/openai_compatible.py`:

```python
reasoning_effort: str | None = None,
```

Place the parameter after `client` and before `observer`. Store it after
`self._model = model`:

```python
self._reasoning_effort = reasoning_effort
```

In `_complete_once`, add the field after constructing the existing payload:

```python
if self._reasoning_effort is not None:
    payload["reasoning_effort"] = self._reasoning_effort
```

Do not send `None`, add `temperature`, or change the strict JSON Schema block.

- [ ] **Step 6: Pass the setting through the runtime factory**

Update `AgentRuntimeFactory.create_llm` in `app/runtime/factory.py`:

```python
return OpenAICompatibleLLMClient(
    base_url=self._settings.llm_base_url,
    api_key=self._settings.llm_api_key.get_secret_value(),
    model=self._settings.openai_model,
    reasoning_effort=self._settings.llm_reasoning_effort,
    client=client,
    observer=observer or self._observer,
    context=context,
)
```

- [ ] **Step 7: Run the focused tests**

Run:

```powershell
pytest tests/test_openai_compatible_provider.py tests/test_runtime.py -q
```

Expected: all tests in both files pass without network access.

### Task 2: Classify rejected provider requests safely

**Files:**

- Edit: `app/workers/agent_worker.py`
- Edit: `app/observability/events.py`
- Test: `tests/test_agent_worker.py`
- Test: `tests/test_jobs_api.py`

- [ ] **Step 1: Use the loaded `unit-test-writer` guidance to add worker classification tests**

Add `import httpx` to `tests/test_agent_worker.py` and this helper:

```python
def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
    response = httpx.Response(
        status_code,
        request=request,
        text="provider-body-secret",
    )
    return httpx.HTTPStatusError(
        "provider request failed",
        request=request,
        response=response,
    )
```

Add a non-retryable rejection test:

```python
@pytest.mark.asyncio
async def test_provider_client_error_is_non_retryable_and_source_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_repository(monkeypatch)
    claimed = _claimed()
    _FakeRepository.next_claimed = claimed
    observer = RecordingObserver()
    worker = AgentWorker(
        _SessionFactory(),
        runtime_factory=_RuntimeFactory(_Runtime(_http_status_error(400))),
        settings=_settings(),
        observer=observer,
        to_thread=asyncio.to_thread,
    )

    assert await worker.serve_once() is True

    repository = _FakeRepository.instances[-1]
    failed = [entry for entry in repository.calls if entry[0] == "mark_failed"]
    assert failed[-1][1][0][1] == "provider_request_rejected"  # type: ignore[index]
    assert not [entry for entry in repository.calls if entry[0] == "schedule_retry"]
    rendered = "".join(event.model_dump_json() for event in observer.events)
    assert "provider-body-secret" not in rendered
```

Add a parameterized test proving `408`, `429`, and `5xx` keep the existing retry path:

```python
@pytest.mark.parametrize("status_code", [408, 429, 500, 503])
@pytest.mark.asyncio
async def test_transient_provider_http_error_is_scheduled_for_retry(
    monkeypatch: pytest.MonkeyPatch,
    status_code: int,
) -> None:
    _install_repository(monkeypatch)
    _FakeRepository.next_claimed = _claimed()
    worker = AgentWorker(
        _SessionFactory(),
        runtime_factory=_RuntimeFactory(_Runtime(_http_status_error(status_code))),
        settings=_settings(),
        random_uniform=lambda _lower, _upper: 0.0,
        to_thread=asyncio.to_thread,
    )

    assert await worker.serve_once() is True
    repository = _FakeRepository.instances[-1]
    retry = [entry for entry in repository.calls if entry[0] == "schedule_retry"]
    assert retry[-1][1][0][1] == "provider_unavailable"  # type: ignore[index]
```

Add a projection test to `tests/test_jobs_api.py`:

```python
def test_project_job_read_exposes_provider_request_rejected(freight_profile) -> None:
    from app.domain.jobs import project_job_read

    row = _row(freight_profile, status="failed")
    row.error_code = "provider_request_rejected"

    safe = project_job_read(row)

    assert safe.error_code == "provider_request_rejected"
```

Add a side-effect precedence test to `tests/test_agent_worker.py`:

```python
@pytest.mark.asyncio
async def test_provider_rejection_after_side_effect_is_failed_uncertain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_repository(monkeypatch)
    _FakeRepository.next_claimed = _claimed(
        side_effect_committed_at=datetime.now(timezone.utc)
    )
    worker = AgentWorker(
        _SessionFactory(),
        runtime_factory=_RuntimeFactory(_Runtime(_http_status_error(400))),
        settings=_settings(),
        to_thread=asyncio.to_thread,
    )

    assert await worker.serve_once() is True
    repository = _FakeRepository.instances[-1]
    uncertain = [
        entry for entry in repository.calls
        if entry[0] == "mark_failed_uncertain"
    ]
    assert uncertain[-1][1][0][1] == "unexpected_after_side_effect"  # type: ignore[index]
    assert not [entry for entry in repository.calls if entry[0] == "schedule_retry"]
```

- [ ] **Step 2: Run the new tests and confirm the expected failures**

Run:

```powershell
pytest tests/test_agent_worker.py tests/test_jobs_api.py -q
```

Expected: the `400` case records `worker_error` instead of `provider_request_rejected`, and the job projection drops the unknown error code. Transient cases remain retryable.

- [ ] **Step 3: Add the closed error code**

Extend `WorkerErrorCode` in `app/observability/events.py`:

```python
PROVIDER_REQUEST_REJECTED = "provider_request_rejected"
```

Place the new member immediately after
`PROVIDER_UNAVAILABLE = "provider_unavailable"`; do not change the existing
members.

`app/domain/jobs.py` already validates error codes through this enum, so it needs no production change.

- [ ] **Step 4: Convert permanent provider HTTP failures to a typed worker error**

Add this exception next to `RetryableJobError` in `app/workers/agent_worker.py`:

```python
class ProviderRequestRejectedError(RuntimeError):
    """A provider rejected a request and retrying it unchanged cannot help."""
```

Change the non-transient branch in `_run_claimed_job`:

```python
except httpx.HTTPStatusError as error:
    status_code = error.response.status_code
    if status_code == 408 or status_code == 429 or status_code >= 500:
        raise RetryableJobError(
            error_code="provider_unavailable"
        ) from error
    raise ProviderRequestRejectedError(
        "provider rejected request"
    ) from error
```

Do not include the provider body, URL query, headers, or original exception text in the new exception message.

- [ ] **Step 5: Persist the stable failure while preserving side-effect safety**

Add an `except ProviderRequestRejectedError` branch in `serve_once`, immediately after `except RetryableJobError` and before lease/generic handlers:

```python
except ProviderRequestRejectedError:
    run_duration_ms = self._elapsed_ms(run_started_ns)
    has_side_effect = await self._has_side_effect(claimed)
    if has_side_effect:
        await self._mark_failed_uncertain(
            claimed,
            "unexpected_after_side_effect",
        )
        self._emit_job_finished(
            claimed,
            outcome=OutcomeCode.FAILED_UNCERTAIN,
            duration_ms=run_duration_ms,
            worker_error_code=WorkerErrorCode.UNEXPECTED_AFTER_SIDE_EFFECT,
            side_effect_committed=True,
        )
    else:
        await self._mark_failed(claimed, "provider_request_rejected")
        self._emit_job_finished(
            claimed,
            outcome=OutcomeCode.FAILED,
            duration_ms=run_duration_ms,
            worker_error_code=WorkerErrorCode.PROVIDER_REQUEST_REJECTED,
        )
    finished = True
```

This branch must not schedule a retry. If a side effect already committed, the existing `failed_uncertain` contract takes precedence over the provider classification.

- [ ] **Step 6: Run worker and projection tests**

Run:

```powershell
pytest tests/test_agent_worker.py tests/test_jobs_api.py -q
```

Expected: all tests pass. Provider `400` is non-retryable and source-free; `408`, `429`, and `5xx` remain retryable.

### Task 3: Expose deployment configuration and update documentation

**Files:**

- Edit: `.env.example`
- Edit: `docker-compose.yml`
- Edit: `README.md`
- Test: `tests/test_demo_compose.py`

- [ ] **Step 1: Add a failing Compose boundary assertion**

Extend `test_compose_passes_optional_provider_settings_only_to_worker` in `tests/test_demo_compose.py`:

```python
assert worker_environment["LLM_REASONING_EFFORT"] == "${LLM_REASONING_EFFORT:-}"
assert "LLM_REASONING_EFFORT" not in api_environment
```

- [ ] **Step 2: Run the Compose test and confirm failure**

Run:

```powershell
pytest tests/test_demo_compose.py -q
```

Expected: failure with `KeyError: 'LLM_REASONING_EFFORT'`.

- [ ] **Step 3: Pass the optional value only to the worker**

Add this entry to `services.worker.environment` in `docker-compose.yml`, next to `OPENAI_MODEL`:

```yaml
LLM_REASONING_EFFORT: ${LLM_REASONING_EFFORT:-}
```

Do not add it to `services.api.environment`.

- [ ] **Step 4: Document the environment variable**

Add this line after `OPENAI_MODEL` in `.env.example`:

```dotenv
LLM_REASONING_EFFORT=
```

Add this row after `OPENAI_MODEL` in the README configuration table:

```markdown
| `LLM_REASONING_EFFORT` | empty | Optional provider reasoning effort: `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, or `max`. Unsupported model/value combinations fail the job with `provider_request_rejected`. |
```

Add one sentence to the provider setup text near the existing `LLM_API_KEY`, `LLM_BASE_URL`, and `OPENAI_MODEL` instructions:

```markdown
Leave `LLM_REASONING_EFFORT` empty to use the selected model's provider default.
```

- [ ] **Step 5: Run the focused deployment and documentation checks**

Run:

```powershell
pytest tests/test_demo_compose.py tests/test_observability.py -q
```

Expected: all tests pass and the README contract checks remain green.

### Task 4: Verify the complete change

**Files:**

- Verify: all files changed by Tasks 1-3

- [ ] **Step 1: Run the focused regression set**

Run:

```powershell
pytest tests/test_openai_compatible_provider.py tests/test_runtime.py tests/test_agent_worker.py tests/test_jobs_api.py tests/test_demo_compose.py tests/test_observability.py -q
```

Expected: all focused tests pass without provider credentials or network access.

- [ ] **Step 2: Run the complete provider-free suite**

Run:

```powershell
pytest -q
```

Expected: all unit, API, and deterministic eval tests pass. Report PostgreSQL integration skips separately when `DATABASE_URL` is absent.

- [ ] **Step 3: Check formatting and unintended scope**

Run:

```powershell
git diff --check
git status --short
git diff -- app/core/config.py app/providers/openai_compatible.py app/runtime/factory.py app/workers/agent_worker.py app/observability/events.py tests/test_openai_compatible_provider.py tests/test_runtime.py tests/test_agent_worker.py tests/test_jobs_api.py tests/test_demo_compose.py .env.example docker-compose.yml README.md docs/specs/2026-10-05-provider-reasoning-effort-design.md docs/plans/2026-10-05-provider-reasoning-effort.md
```

Expected: `git diff --check` exits with code 0. The diff contains no `temperature`, model-name capability parsing, provider response bodies, live network tests, or unrelated refactors.

### Task 5: Run final code-quality checks

**Files:**

- Verify: all implementation and test files changed by Tasks 1-3

- [ ] **Step 1: Run the project verification commands after the final diff is assembled**

Run:

```powershell
pytest -q
pytest evals/test_freight.py -q
git diff --check
```

Expected: pytest and the deterministic freight gate pass; PostgreSQL tests may skip only when `DATABASE_URL` is absent; `git diff --check` exits with code 0. If a command fails, fix the implementation or test causing the failure, then rerun the complete set.

### Task 6: Review the final diff

**Files:**

- Verify: the complete working-tree diff for this feature

- [ ] **Step 1: Call the `code-review` skill on the changes made by this plan**

Give the reviewer the complete diff and the design/plan context. The review must check provider error classification, secret/source redaction, worker retry and side-effect semantics, configuration validation, API projection, Compose scoping, and test coverage.

- [ ] **Step 2: Resolve review findings with the user before editing**

Do not make corrections automatically from review comments. Discuss each finding with the user first; apply only the changes the user approves, then rerun Task 5.
