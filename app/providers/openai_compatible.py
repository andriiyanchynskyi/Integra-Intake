"""OpenAI-compatible structured-output implementation of the LLM boundary."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Sequence
from copy import deepcopy
from uuid import uuid4

import httpx
from pydantic import ValidationError

from app.agent import AgentMessage, AgentProposal, MessageRole, ToolCall
from app.observability import (
    Component,
    EventLevel,
    EventName,
    NULL_OBSERVER,
    ObservationContext,
    ObservationEvent,
    Observer,
    OutcomeCode,
    safe_emit,
)


class StructuredProposalValidationError(RuntimeError):
    """Raised after the adapter exhausts its single schema-repair retry."""


class _SanitizedValidationCause(ValueError):
    """Cause type that carries only already-redacted validation feedback."""


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _validation_feedback(error: Exception) -> str:
    if isinstance(error, ValidationError):
        details = error.errors(include_input=False, include_url=False)
        messages: list[str] = []
        for detail in details:
            location = ".".join(str(part) for part in detail["loc"])
            messages.append(f"{location or '<root>'}: {detail['msg']}")
        summary = "; ".join(messages) or "schema validation failed"
        return f"Structured proposal validation failed: {summary}"
    if isinstance(error, json.JSONDecodeError):
        return f"Structured proposal JSON decoding failed at character {error.pos}"
    if isinstance(error, KeyError):
        return "Provider response envelope is missing a required value"
    if isinstance(error, IndexError):
        return "Provider response envelope has no usable choice"
    if isinstance(error, TypeError):
        return "Provider response envelope has an invalid shape"
    return "Provider response content is invalid"


def _redact_secret(value: str, secret: str) -> str:
    if not secret:
        return value
    return value.replace(secret, "[REDACTED]")


def _provider_messages(messages: Sequence[AgentMessage]) -> list[dict[str, object]]:
    provider_messages: list[dict[str, object]] = []
    pending_tool_call_id: str | None = None
    pending_tool_call: ToolCall | None = None

    for index, message in enumerate(messages):
        if (
            pending_tool_call_id is not None
            and message.role is not MessageRole.TOOL
        ):
            raise ValueError(
                "assistant tool call must be followed by its tool message"
            )

        if message.role is MessageRole.SYSTEM:
            provider_messages.append(
                {"role": "system", "content": message.content or ""}
            )
            continue

        if message.role is MessageRole.USER:
            content: object = message.content
            if content is None and message.tool_result is not None:
                content = _canonical_json(message.tool_result)
            provider_messages.append(
                {"role": "user", "content": content or ""}
            )
            continue

        if message.role is MessageRole.ASSISTANT:
            assistant_content = message.content
            if assistant_content is None and message.proposal is not None:
                assistant_content = message.proposal.rationale_short
            assistant_message: dict[str, object] = {
                "role": "assistant",
                "content": assistant_content or "",
            }
            if message.tool_call is not None:
                pending_tool_call_id = f"call_{index}"
                pending_tool_call = message.tool_call
                assistant_message["tool_calls"] = [
                    {
                        "id": pending_tool_call_id,
                        "type": "function",
                        "function": {
                            "name": message.tool_call.name,
                            "arguments": _canonical_json(
                                message.tool_call.arguments
                            ),
                        },
                    }
                ]
            provider_messages.append(assistant_message)
            continue

        if message.role is MessageRole.TOOL:
            if (
                message.tool_call is None
                or pending_tool_call_id is None
                or message.tool_call != pending_tool_call
            ):
                raise ValueError("tool message has no preceding assistant call")
            provider_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": pending_tool_call_id,
                    "content": _canonical_json(message.tool_result),
                }
            )
            pending_tool_call_id = None
            pending_tool_call = None
            continue

        raise ValueError(f"unsupported agent message role: {message.role}")

    return provider_messages


class OpenAICompatibleLLMClient:
    """Synchronous OpenAI-compatible implementation of ``LLMClient``."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        client: httpx.Client,
        reasoning_effort: str | None = None,
        observer: Observer = NULL_OBSERVER,
        context: ObservationContext | None = None,
        clock: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        self._endpoint = f"{base_url.rstrip('/')}/chat/completions"
        self._api_key = api_key
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._client = client
        self._observer = observer
        self._context = context or ObservationContext(trace_id=uuid4())
        self._clock = clock

    def complete(self, messages: Sequence[AgentMessage]) -> AgentProposal:
        original = deepcopy(tuple(messages))
        try:
            proposal, _usage = self._attempt(
                original,
                call_index=1,
                validation_retry=False,
            )
            return proposal
        except (
            json.JSONDecodeError,
            ValidationError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
        ) as error:
            feedback = _redact_secret(
                _validation_feedback(error), self._api_key
            )
        retry_messages = original + (
            AgentMessage(role=MessageRole.USER, content=feedback),
        )
        try:
            proposal, _usage = self._attempt(
                retry_messages,
                call_index=2,
                validation_retry=True,
            )
            return proposal
        except (
            json.JSONDecodeError,
            ValidationError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
        ) as retry_error:
            retry_feedback = _redact_secret(
                _validation_feedback(retry_error), self._api_key
            )
        self._emit_structured_failure()
        # Keep the provider exception out of the public error context. It may
        # contain untrusted response details, so expose only the sanitized
        # validator summary above.
        raise StructuredProposalValidationError(
            "provider returned an invalid structured proposal after one retry: "
            + retry_feedback
        ) from _SanitizedValidationCause(retry_feedback)

    def _attempt(
        self,
        messages: Sequence[AgentMessage],
        *,
        call_index: int,
        validation_retry: bool,
    ) -> tuple[AgentProposal, dict[str, int]]:
        started_ns = self._clock()
        try:
            proposal, usage = self._complete_once(messages)
        except (
            json.JSONDecodeError,
            ValidationError,
            ValueError,
            KeyError,
            IndexError,
            TypeError,
        ):
            self._emit_call(
                call_index=call_index,
                validation_retry=validation_retry,
                started_ns=started_ns,
                outcome=OutcomeCode.INVALID_SCHEMA,
            )
            raise
        except httpx.HTTPStatusError as error:
            self._emit_call(
                call_index=call_index,
                validation_retry=validation_retry,
                started_ns=started_ns,
                outcome=OutcomeCode.HTTP_ERROR,
                status_code=error.response.status_code,
            )
            raise
        except httpx.TimeoutException:
            self._emit_call(
                call_index=call_index,
                validation_retry=validation_retry,
                started_ns=started_ns,
                outcome=OutcomeCode.TIMEOUT,
            )
            raise
        except httpx.RequestError:
            self._emit_call(
                call_index=call_index,
                validation_retry=validation_retry,
                started_ns=started_ns,
                outcome=OutcomeCode.NETWORK_ERROR,
            )
            raise
        except Exception:
            self._emit_call(
                call_index=call_index,
                validation_retry=validation_retry,
                started_ns=started_ns,
                outcome=OutcomeCode.FAILED,
            )
            raise
        self._emit_call(
            call_index=call_index,
            validation_retry=validation_retry,
            started_ns=started_ns,
            outcome=OutcomeCode.SUCCESS,
            usage=usage,
        )
        return proposal, usage

    def _complete_once(
        self, messages: Sequence[AgentMessage]
    ) -> tuple[AgentProposal, dict[str, int]]:
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
        if self._reasoning_effort is not None:
            payload["reasoning_effort"] = self._reasoning_effort
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
        usage = _validated_usage(body.get("usage"))
        return AgentProposal.model_validate_json(content), usage

    def _emit_call(
        self,
        *,
        call_index: int,
        validation_retry: bool,
        started_ns: int,
        outcome: OutcomeCode,
        status_code: int | None = None,
        usage: dict[str, int] | None = None,
    ) -> None:
        payload: dict[str, object] = {
            "event": EventName.PROVIDER_CALL_COMPLETED,
            "level": (
                EventLevel.INFO
                if outcome is OutcomeCode.SUCCESS
                else EventLevel.WARNING
            ),
            "trace_id": self._context.trace_id,
            "request_id": self._context.request_id,
            "tenant_id": self._context.tenant_id,
            "job_id": self._context.job_id,
            "component": Component.PROVIDER,
            "scenario_key": self._context.scenario_key,
            "profile_fingerprint": self._context.profile_fingerprint,
            "outcome": outcome,
            "duration_ms": max(0, (self._clock() - started_ns) // 1_000_000),
            "provider_call_index": call_index,
            "validation_retry": validation_retry,
            "status_code": status_code,
        }
        if usage:
            payload.update(usage)
        safe_emit(self._observer, ObservationEvent.model_validate(payload))

    def _emit_structured_failure(self) -> None:
        safe_emit(
            self._observer,
            ObservationEvent(
                event=EventName.PROVIDER_STRUCTURED_OUTPUT_FAILED,
                level=EventLevel.ERROR,
                trace_id=self._context.trace_id,
                request_id=self._context.request_id,
                tenant_id=self._context.tenant_id,
                job_id=self._context.job_id,
                component=Component.PROVIDER,
                scenario_key=self._context.scenario_key,
                profile_fingerprint=self._context.profile_fingerprint,
                outcome=OutcomeCode.INVALID_SCHEMA,
            ),
        )


def _validated_usage(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    usage: dict[str, int] = {}
    for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
        token_value = value.get(name)
        if (
            isinstance(token_value, int)
            and not isinstance(token_value, bool)
            and token_value >= 0
        ):
            usage[name] = token_value
    if {
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
    } <= usage.keys() and usage["total_tokens"] != (
        usage["prompt_tokens"] + usage["completion_tokens"]
    ):
        return {}
    return usage
