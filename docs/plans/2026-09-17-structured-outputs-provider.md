# Implementation plan: Phase 5 structured outputs and provider adapter

> **For agents:** Use the `executing-plans` skill to implement this plan task by task. Steps use checkbox syntax (`- [ ]`) for tracking.

**Purpose:** Make the synchronous `LLMClient` contract return a strict,
validated intake proposal and provide a real OpenAI-compatible implementation
without coupling the Phase-4 loop to an HTTP provider.

**Architecture:** `app.agent` owns immutable proposal, transcript, and loop
control types. `app.providers.openai_compatible.OpenAICompatibleLLMClient`
implements the existing synchronous `LLMClient` protocol with `httpx`, strict
JSON Schema, and exactly one validation retry. No HTTP route calls it in this
phase; a later bounded worker pool is the runtime owner of blocking LLM work.

**Key files/stack:** Python 3.12+, Pydantic v2, pydantic-settings, `httpx`,
pytest, `httpx.MockTransport`; existing `AgentLoop`, `LLMClient`, and tests.

---

## File map

| File | Responsibility |
| --- | --- |
| `app/agent/models.py` | Strict immutable `AgentProposal`, proposal tool-call DTO, and transcript/run-result proposal support. |
| `app/agent/loop.py` | Preserve the eight-step and repeated-tool controls while consuming zero-or-one structured tool calls. |
| `app/agent/__init__.py` | Export the structured proposal public API. |
| `app/providers/__init__.py` | Export the provider adapter and typed validation error. |
| `app/providers/openai_compatible.py` | Isolate OpenAI-compatible request/response handling, JSON Schema, retry, and sanitized failure. |
| `app/core/config.py` | Add safe `OPENAI_MODEL` and hide `LLM_API_KEY` in representations. |
| `.env.example` | Document the model ID only; retain an empty key placeholder. |
| `tests/test_agent_loop.py` | Update fakes to build valid structured proposals and preserve Phase-4 behavior tests. |
| `tests/test_openai_compatible_provider.py` | Offline mock-transport coverage for provider request, validation, retry, and secret safety. |

No file under `app/api/`, `app/db/`, `app/domain/`, `app/tenants/`, Alembic,
or `examples/` is changed by this plan.

## Task 1: Define strict structured proposal domain types

**Files:**

- Edit: `app/agent/models.py`
- Edit: `app/agent/__init__.py`

- [ ] **Step 1: Replace the control-only proposal with strict schema-capable types.**

Use Pydantic v2 types in `app/agent/models.py` while keeping the existing
`ToolCall`, `ToolData`, `_freeze_data`, message roles, status values, and
stop-reason values. Add the following definitions after `ToolCall`:

```python
from enum import Enum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ProposalPriority(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class _StrictProposalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


type ProposalScalar = None | bool | int | float | str
type ProposalValueData = ProposalScalar | list[ProposalScalar]


class ProposalValue(_StrictProposalModel):
    name: Annotated[str, Field(min_length=1)]
    value: ProposalValueData


class ProposalToolCall(_StrictProposalModel):
    name: Annotated[str, Field(min_length=1)]
    arguments: list[ProposalValue]

    @field_validator("arguments")
    @classmethod
    def require_unique_arguments(cls, value: list[ProposalValue]) -> list[ProposalValue]:
        names = [item.name for item in value]
        if len(names) != len(set(names)):
            raise ValueError("tool argument names must be unique")
        return value

    def as_tool_call(self) -> ToolCall:
        return ToolCall(
            name=self.name,
            arguments={item.name: item.value for item in self.arguments},
        )


class AgentProposal(_StrictProposalModel):
    intake_type: Annotated[str, Field(min_length=1)] | None
    fields: list[ProposalValue]
    missing_required_fields: list[Annotated[str, Field(min_length=1)]]
    priority: ProposalPriority
    contains_injection_or_override_attempt: bool
    rationale_short: Annotated[str, Field(min_length=1, max_length=1000)]
    tool_calls: list[ProposalToolCall] = Field(max_length=1)
    confidence: Annotated[float, Field(ge=0.0, le=1.0)]

    @field_validator("fields")
    @classmethod
    def require_unique_fields(cls, value: list[ProposalValue]) -> list[ProposalValue]:
        names = [item.name for item in value]
        if len(names) != len(set(names)):
            raise ValueError("field names must be unique")
        return value

    @field_validator("missing_required_fields")
    @classmethod
    def require_unique_missing_fields(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("missing_required_fields must be unique")
        return value

    @property
    def tool_call(self) -> ToolCall | None:
        if not self.tool_calls:
            return None
        return self.tool_calls[0].as_tool_call()

    @property
    def is_terminal(self) -> bool:
        return not self.tool_calls
```

Define `ProposalValue` as a closed object with `name` and a strict scalar-or-
scalar-list `value`. This keeps dynamic tenant field names while ensuring every
object in the generated strict schema has `additionalProperties: false`.
Convert proposal values to immutable mappings only in `as_tool_call` or a
dedicated application accessor; do not put open mapping schemas in the provider
wire contract.

- [ ] **Step 2: Retain the typed proposal in transcript and result objects.**

Extend the existing frozen dataclasses exactly as follows:

```python
@dataclass(frozen=True, slots=True)
class AgentMessage:
    role: MessageRole
    content: str | None = None
    proposal: AgentProposal | None = None
    tool_call: ToolCall | None = None
    tool_result: ToolData = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_result", _freeze_data(self.tool_result))


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    status: RunStatus
    reason: StopReason
    messages: tuple[AgentMessage, ...]
    steps: int
    final_response: str | None
    proposal: AgentProposal | None = None
```

Do not add a second permission or routing field. `priority`, injection state,
and `confidence` remain information only at this boundary.

- [ ] **Step 3: Export the new public model types.**

Add `ProposalPriority` and `ProposalToolCall` to the import from
`app.agent.models` and to `__all__` in `app/agent/__init__.py`. Preserve every
existing export.

- [ ] **Step 4: Check syntax and strict JSON Schema before touching the loop.**

Run:

```powershell
python -m compileall -q app/agent
python -c "from app.agent import AgentProposal; import json; print(json.dumps(AgentProposal.model_json_schema()))"
```

Expected: both commands exit `0`; schema output contains all eight required
proposal fields and closed object definitions for the proposal and tool call.

## Task 2: Adapt the controlled loop without weakening its safeguards

**Files:**

- Edit: `app/agent/loop.py`

- [ ] **Step 1: Append the complete immutable proposal on every LLM step.**

Inside `AgentLoop.run`, replace the existing assistant-message construction
and final-response branch with:

```python
proposal = self.llm.complete(deepcopy(tuple(messages)))
steps += 1
tool_call = deepcopy(proposal.tool_call)
messages.append(
    AgentMessage(
        role=MessageRole.ASSISTANT,
        content=proposal.rationale_short,
        proposal=deepcopy(proposal),
        tool_call=tool_call,
    )
)

if proposal.is_terminal:
    return AgentRunResult(
        status=RunStatus.COMPLETED,
        reason=StopReason.FINAL,
        messages=tuple(deepcopy(messages)),
        steps=steps,
        final_response=proposal.rationale_short,
        proposal=deepcopy(proposal),
    )
```

`LLMClient.complete(messages) -> AgentProposal` remains unchanged. A valid
strict proposal cannot be the old empty control proposal, so remove only the
unreachable `INVALID_PROPOSAL` branch; retain the `StopReason` enum member for
backward compatibility unless it has an existing caller that requires removal.

- [ ] **Step 2: Preserve tool execution and all failed-result shapes.**

Keep the existing consecutive-name counter, the condition
`consecutive_tool_calls == MAX_CONSECUTIVE_TOOL_CALLS`, the hard cap
`MAX_STEPS = 8`, and `deepcopy` calls. In every `REPEATED_TOOL` and
`MAX_STEPS` result, pass `proposal=None` explicitly or rely on its default.
The third same call must still be appended as an assistant transcript entry but
must not reach `self.tools.execute`. Tool results, including `None`, continue
to be stored in a `MessageRole.TOOL` message.

- [ ] **Step 3: Compile and inspect public imports.**

Run:

```powershell
python -m compileall -q app/agent
python -c "from app.agent import AgentLoop, AgentProposal, ProposalPriority; print(AgentLoop, AgentProposal, ProposalPriority.NORMAL.value)"
```

Expected: exit `0` and the final token is `normal`.

## Task 3: Implement the isolated OpenAI-compatible adapter

**Files:**

- Create: `app/providers/__init__.py`
- Create: `app/providers/openai_compatible.py`

- [ ] **Step 1: Add a typed sanitized validation error and adapter constructor.**

Create `app/providers/openai_compatible.py` with this public skeleton:

```python
"""OpenAI-compatible structured-output implementation of the LLM boundary."""

from __future__ import annotations

import json
from collections.abc import Sequence
from copy import deepcopy
from hashlib import sha256

import httpx
from pydantic import ValidationError

from app.agent import AgentMessage, AgentProposal, MessageRole


class StructuredProposalValidationError(RuntimeError):
    """Raised after the adapter exhausts its single schema-repair retry."""


class OpenAICompatibleLLMClient:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        client: httpx.Client,
    ) -> None:
        self._endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self._api_key = api_key
        self._model = model
        self._client = client

    def complete(self, messages: Sequence[AgentMessage]) -> AgentProposal:
        original = deepcopy(tuple(messages))
        try:
            return self._complete_once(original)
        except (
            json.JSONDecodeError,
            ValidationError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
        ) as error:
            feedback = _validation_feedback(error)
            retry_messages = original + (
                AgentMessage(role=MessageRole.USER, content=feedback),
            )
            try:
                return self._complete_once(retry_messages)
            except (
                json.JSONDecodeError,
                ValidationError,
                ValueError,
                KeyError,
                IndexError,
                TypeError,
            ) as retry_error:
                raise StructuredProposalValidationError(
                    "provider returned an invalid structured proposal after one retry: "
                    + _validation_feedback(retry_error)
                ) from retry_error
```

`_validation_feedback` must use Pydantic errors with `include_input=False` and
`include_url=False`; for a JSON decode error it returns only the exception type
and position. It must not interpolate a response body, request headers, or the
API key. Do not catch `httpx.HTTPError` in this validation-retry branch.

- [ ] **Step 2: Build strict request payloads and parse only provider content.**

Implement `_complete_once` so it calls `self._client.post` with:

```python
payload = {
    "model": self._model,
    "messages": _provider_messages(messages),
    "response_format": {
        "type": "json_schema",
        "json_schema": {
            "name": "agent_proposal",
            "strict": True,
            "schema": AgentProposal.model_json_schema(),
        },
    },
}
response = self._client.post(
    self._endpoint,
    headers={"Authorization": f"Bearer {self._api_key}"},
    json=payload,
)
response.raise_for_status()
body = response.json()
content = body["choices"][0]["message"]["content"]
if not isinstance(content, str):
    raise ValueError("provider response content must be a JSON string")
return AgentProposal.model_validate_json(content)
```

Map each `AgentMessage` to a normal chat message without concatenating a tool
result into a free-form instruction. For an assistant message that has a tool
call, emit a native assistant `tool_calls` object. Generate its deterministic
ID as `call_<message-index>`. For its following tool message, emit role `tool`,
the canonical JSON representation of `tool_result` as `content`, and the
matching `tool_call_id`. Use the assistant message's `rationale_short` as its
assistant content. A user message maps directly to a user content message.
Raise `ValueError` for an orphaned tool message, because the internal
transcript invariant requires the preceding assistant call.

Use `json.dumps(..., allow_nan=False, sort_keys=True, separators=(",", ":"))`
for tool arguments and tool results. The SHA-256 import is available only if a
stable ID needs an argument-derived suffix; do not include secrets in any ID or
error. Do not add native provider `tools` definitions or business-tool
implementations.

- [ ] **Step 3: Export only the provider boundary.**

Create `app/providers/__init__.py`:

```python
"""Provider adapters kept outside the agent control-flow package."""

from app.providers.openai_compatible import (
    OpenAICompatibleLLMClient,
    StructuredProposalValidationError,
)

__all__ = [
    "OpenAICompatibleLLMClient",
    "StructuredProposalValidationError",
]
```

- [ ] **Step 4: Compile without sending a network request.**

Run:

```powershell
python -m compileall -q app/providers
python -c "from app.providers import OpenAICompatibleLLMClient, StructuredProposalValidationError; print(OpenAICompatibleLLMClient.__name__, StructuredProposalValidationError.__name__)"
```

Expected: exit `0`, and no request is sent because the command constructs no
client instance.

## Task 4: Add safe provider configuration

**Files:**

- Edit: `app/core/config.py`
- Edit: `.env.example`

- [ ] **Step 1: Use a secret-aware API-key setting and model ID.**

Change `app/core/config.py` to import `SecretStr` and define these settings:

```python
from pydantic import SecretStr


class Settings(BaseSettings):
    # Preserve the current model_config exactly.
    database_url: str = "postgresql+asyncpg://integra:integra@localhost:5432/integra"
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "https://api.openai.com/v1"
    openai_model: str = "gpt-5.4-mini-2026-03-17"
    app_env: str = "development"
```

The automatic Pydantic settings mapping makes `OPENAI_MODEL` populate
`openai_model`; do not introduce another API-key environment variable. Code
that constructs the adapter must use `settings.llm_api_key.get_secret_value()`
only at the authorization-header boundary.

- [ ] **Step 2: Document only non-secret configuration.**

Keep the existing empty `LLM_API_KEY=` line in `.env.example` and add:

```dotenv
OPENAI_MODEL=gpt-5.4-mini-2026-03-17
```

Do not place a raw key, a bearer token, or a copied local `.env` value in any
tracked file.

- [ ] **Step 3: Validate the configuration boundary.**

Run:

```powershell
python -c "from app.core.config import Settings; value = Settings(llm_api_key='not-for-logs'); print(Settings(openai_model='test-model')); print(value.llm_api_key.get_secret_value() == 'not-for-logs')"
```

Expected: the printed settings representation masks the key, and the final
line is `True`.

## Task 5: Invoke `unit-test-writer` for all new and changed behavior

**Files:**

- Create: `tests/test_openai_compatible_provider.py`
- Edit: `tests/test_agent_loop.py`

- [ ] **Step 1: Call the `unit-test-writer` skill through an Agent task.**

Give the test-writing agent this exact context. It may edit only the two test
files listed above; it must not change product code or configuration.

```text
Write pytest unit tests for Phase 5 of IntegraIntake.

Product interfaces:
- app.agent.AgentProposal is strict and has intake_type, fields,
  missing_required_fields, priority (low|normal|high|urgent),
  contains_injection_or_override_attempt, rationale_short, tool_calls (max 1),
  and confidence [0,1]. Fixed-shape unknown keys are invalid.
- app.providers.OpenAICompatibleLLMClient implements complete(messages) with
  an injected synchronous httpx.Client and uses POST /chat/completions plus
  response_format.type=json_schema and json_schema.strict=true.
- invalid JSON/schema response triggers exactly one request with validation
  feedback; a second invalid result raises StructuredProposalValidationError.
- API keys must never appear in Settings repr or that exception string.
- AgentLoop remains synchronous and must preserve MAX_STEPS=8, third
  consecutive same-tool break before execution, immutable ordered transcript,
  and None tool results.

In tests/test_openai_compatible_provider.py use only httpx.MockTransport and
a dummy value such as 'test-secret-never-log'. Assert no network service is
contacted. Cover: valid proposal and outgoing JSON Schema; invalid JSON then
valid response with two requests and feedback; two invalid responses yielding
the typed error after two requests; a top-level unexpected field rejected;
secret absent from repr(error) and str(error); endpoint normalization.

In tests/test_agent_loop.py add a helper that creates a fully valid proposal
with defaults. Update existing fakes/tests to use tool_calls=[...] for tool
steps and tool_calls=[] for terminal steps. Keep existing assertions for None,
the third identical tool call, eight steps, transcript order, and mutation
snapshots. Add an assertion that terminal AgentRunResult.proposal is the same
validated proposal and confidence does not alter loop behavior.
```

- [ ] **Step 2: Run the focused tests after the test agent returns.**

Run:

```powershell
pytest tests/test_openai_compatible_provider.py tests/test_agent_loop.py -q
```

Expected: all focused tests pass without DNS, HTTP service, or real credential
access.

## Task 6: Review diff scope and run regression checks

**Files:**

- Review: every file changed by Tasks 1–5

- [ ] **Step 1: Run focused and full tests.**

Run:

```powershell
pytest tests/test_openai_compatible_provider.py tests/test_agent_loop.py -q
pytest -q
```

Expected: focused tests pass; the full suite passes, with PostgreSQL-only tests
allowed to skip only when their existing `DATABASE_URL` condition applies.

- [ ] **Step 2: Check whitespace and inspect scope.**

Run:

```powershell
git diff --check
git diff -- app/agent app/providers app/core/config.py .env.example tests
```

Expected: no whitespace diagnostics; the diff contains no endpoint, migration,
tenant profile/routing, persistence, queue, worker, policy, approval, email,
or real business-tool changes.

- [ ] **Step 3: Call the `code-review` skill on the final diff.**

Provide the reviewer with the approved specification, this plan, and the final
diff. Ask it to inspect schema closure, retry cardinality, error sanitization,
secret exposure, provider/agent separation, loop regressions, and phase-scope
violations. Do not make corrections based on findings before discussing them
with the user.

## Task 7: Verify before declaring completion

**Files:**

- Verify: final implementation and test output

- [ ] **Step 1: Call the `verification-before-completion` skill.**

Give it the completed diff, focused test output, full `pytest -q` output,
`git diff --check` output, and the code-review result. Do not claim the phase
is complete until the skill confirms evidence for every acceptance criterion.

## Plan self-review

| Specification requirement | Implementing task |
| --- | --- |
| Provider-independent `AgentLoop`/`LLMClient` | 1–3 |
| Eight proposal fields, strict schema, at most one tool | 1 |
| `priority` and `confidence` do not grant authority | 1–2 and test-agent prompt |
| One validation feedback retry; second typed failure | 3 and 5 |
| `LLM_API_KEY`, `LLM_BASE_URL`, `OPENAI_MODEL` safety | 3–4 |
| No network-dependent unit tests | 5–6 |
| Phase-4 loop behavior preserved | 2 and 5–6 |
| Tenant/API/YAML behavior unchanged | file map and 6 |
| Required unit-test writer, review, and verification gates | 5–7 |

The plan contains no placeholders, names all public types and configuration
keys consistently, and limits modifications to the approved Phase-5 boundary.
