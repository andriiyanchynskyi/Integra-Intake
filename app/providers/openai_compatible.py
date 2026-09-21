"""OpenAI-compatible structured-output implementation of the LLM boundary."""

from __future__ import annotations

import json
from collections.abc import Sequence
from copy import deepcopy

import httpx
from pydantic import ValidationError

from app.agent import AgentMessage, AgentProposal, MessageRole, ToolCall


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
            feedback = _redact_secret(
                _validation_feedback(error), self._api_key
            )
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
            retry_feedback = _redact_secret(
                _validation_feedback(retry_error), self._api_key
            )
        # Keep the provider exception out of the public error context. It may
        # contain untrusted response details, so expose only the sanitized
        # validator summary above.
        raise StructuredProposalValidationError(
            "provider returned an invalid structured proposal after one retry: "
            + retry_feedback
        ) from _SanitizedValidationCause(retry_feedback)

    def _complete_once(
        self, messages: Sequence[AgentMessage]
    ) -> AgentProposal:
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
