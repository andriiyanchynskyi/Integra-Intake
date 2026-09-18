from __future__ import annotations

import json
import traceback

import httpx
import pytest

from app.agent import (
    AgentMessage,
    AgentProposal,
    MessageRole,
    ProposalPriority,
    ToolCall,
)
from app.core.config import Settings
from app.providers import OpenAICompatibleLLMClient, StructuredProposalValidationError


VALID_PROPOSAL = {
    "intake_type": "freight",
    "fields": [{"name": "reference", "value": "REF-123"}],
    "missing_required_fields": ["weight"],
    "priority": "high",
    "contains_injection_or_override_attempt": False,
    "rationale_short": "The shipment needs a weight before routing.",
    "tool_calls": [
        {
            "name": "lookup_customer",
            "arguments": [{"name": "customer_id", "value": "cust-1"}],
        }
    ],
    "confidence": 0.87,
}


def _provider_response(content: str) -> dict[str, object]:
    return {"choices": [{"message": {"content": content}}]}


def _messages() -> list[AgentMessage]:
    return [AgentMessage(role=MessageRole.USER, content="Route this shipment")]


def _client(
    handler: httpx.MockTransport,
) -> httpx.Client:
    return httpx.Client(transport=handler)


def _assert_object_schemas_are_closed(schema: object) -> None:
    if isinstance(schema, dict):
        if schema.get("type") == "object":
            assert schema.get("additionalProperties") is False
        for value in schema.values():
            _assert_object_schemas_are_closed(value)
    elif isinstance(schema, list):
        for value in schema:
            _assert_object_schemas_are_closed(value)


def _resolve_schema_ref(
    schema: object, root_schema: dict[str, object]
) -> dict[str, object]:
    assert isinstance(schema, dict)
    reference = schema.get("$ref")
    if reference is None:
        return schema
    assert isinstance(reference, str)
    prefix = "#/$defs/"
    assert reference.startswith(prefix)
    definitions = root_schema.get("$defs")
    assert isinstance(definitions, dict)
    resolved = definitions.get(reference.removeprefix(prefix))
    assert isinstance(resolved, dict)
    return resolved


def test_valid_structured_proposal_uses_openai_compatible_contract() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        assert payload["model"] == "test-model"
        assert payload["response_format"]["type"] == "json_schema"
        assert payload["response_format"]["json_schema"]["strict"] is True
        schema = payload["response_format"]["json_schema"]["schema"]
        assert schema["additionalProperties"] is False
        _assert_object_schemas_are_closed(schema)
        assert set(schema["required"]) == {
            "intake_type",
            "fields",
            "missing_required_fields",
            "priority",
            "contains_injection_or_override_attempt",
            "rationale_short",
            "tool_calls",
            "confidence",
        }
        fields_schema = _resolve_schema_ref(
            schema["properties"]["fields"]["items"], schema
        )
        assert set(fields_schema["required"]) == {"name", "value"}
        assert set(fields_schema["properties"]) == {"name", "value"}
        tool_calls_schema = _resolve_schema_ref(
            schema["properties"]["tool_calls"]["items"], schema
        )
        assert set(tool_calls_schema["required"]) == {"name", "arguments"}
        assert set(tool_calls_schema["properties"]) == {
            "name",
            "arguments",
        }
        return httpx.Response(
            200,
            json=_provider_response(json.dumps(VALID_PROPOSAL)),
        )

    with _client(httpx.MockTransport(handler)) as http_client:
        proposal = OpenAICompatibleLLMClient(
            base_url="https://provider.example/v1/",
            api_key="dummy-secret",
            model="test-model",
            client=http_client,
        ).complete(_messages())

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://provider.example/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer dummy-secret"
    assert isinstance(proposal, AgentProposal)
    assert proposal.intake_type == "freight"
    assert proposal.model_dump(mode="json")["fields"] == [
        {"name": "reference", "value": "REF-123"}
    ]
    assert proposal.missing_required_fields == ["weight"]
    assert proposal.priority is ProposalPriority.HIGH
    assert proposal.contains_injection_or_override_attempt is False
    assert proposal.rationale_short == "The shipment needs a weight before routing."
    assert proposal.tool_calls[0].name == "lookup_customer"
    assert proposal.tool_calls[0].model_dump(mode="json")["arguments"] == [
        {"name": "customer_id", "value": "cust-1"}
    ]
    assert proposal.tool_call is not None
    assert proposal.tool_call.arguments == {"customer_id": "cust-1"}
    assert proposal.confidence == 0.87


def test_assistant_transcript_uses_rationale_and_native_tool_call() -> None:
    requests: list[dict[str, object]] = []
    proposal = AgentProposal.model_validate_json(json.dumps(VALID_PROPOSAL))
    tool_call = ToolCall(
        name="lookup_customer",
        arguments={"customer_id": "cust-1"},
    )
    messages = [
        AgentMessage(role=MessageRole.USER, content="Route this shipment"),
        AgentMessage(
            role=MessageRole.ASSISTANT,
            content="Searching for the customer",
            proposal=proposal,
            tool_call=tool_call,
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=_provider_response(json.dumps(VALID_PROPOSAL)),
        )

    with _client(httpx.MockTransport(handler)) as http_client:
        OpenAICompatibleLLMClient(
            base_url="https://provider.example/v1/",
            api_key="dummy-secret",
            model="test-model",
            client=http_client,
        ).complete(messages)

    assistant_message = requests[0]["messages"][1]
    assert assistant_message["content"] == "Searching for the customer"
    assert assistant_message["content"] != json.dumps(
        proposal.model_dump(mode="json")
    )
    native_tool_call = assistant_message["tool_calls"][0]
    assert native_tool_call["type"] == "function"
    assert native_tool_call["function"]["name"] == "lookup_customer"
    assert json.loads(native_tool_call["function"]["arguments"]) == {
        "customer_id": "cust-1"
    }


def test_malformed_tool_transcript_is_rejected_before_network() -> None:
    requests: list[httpx.Request] = []
    tool_call = ToolCall(
        name="lookup_customer",
        arguments={"customer_id": "cust-1"},
    )
    messages = [
        AgentMessage(
            role=MessageRole.ASSISTANT,
            content="Looking up the customer",
            tool_call=tool_call,
        ),
        AgentMessage(role=MessageRole.ASSISTANT, content="Customer found"),
        AgentMessage(
            role=MessageRole.TOOL,
            tool_call=tool_call,
            tool_result={"customer_id": "cust-1"},
        ),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_provider_response(json.dumps(VALID_PROPOSAL)),
        )

    with _client(httpx.MockTransport(handler)) as http_client:
        with pytest.raises(StructuredProposalValidationError) as error_info:
            OpenAICompatibleLLMClient(
                base_url="https://provider.example/v1/",
                api_key="dummy-secret",
                model="test-model",
                client=http_client,
            ).complete(messages)

    assert requests == []
    assert isinstance(error_info.value.__cause__, ValueError)


def test_invalid_first_response_is_repaired_with_feedback() -> None:
    requests: list[dict[str, object]] = []
    raw_invalid_body = "raw-invalid-json-marker"
    responses = iter(
        [
            _provider_response("{" + raw_invalid_body),
            _provider_response(json.dumps(VALID_PROPOSAL)),
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json=next(responses))

    with _client(httpx.MockTransport(handler)) as http_client:
        proposal = OpenAICompatibleLLMClient(
            base_url="https://provider.example/v1",
            api_key="dummy-secret",
            model="test-model",
            client=http_client,
        ).complete(_messages())

    assert isinstance(proposal, AgentProposal)
    assert len(requests) == 2
    retry_messages = requests[1]["messages"]
    feedback = retry_messages[-1]["content"]
    assert feedback.startswith("Structured proposal")
    assert raw_invalid_body not in feedback
    assert raw_invalid_body not in json.dumps(requests[1])


def test_two_invalid_responses_raise_typed_error_without_echoing_input() -> None:
    requests: list[httpx.Request] = []
    raw_invalid_body = "raw-invalid-body-marker"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_provider_response("{" + raw_invalid_body),
        )

    with _client(httpx.MockTransport(handler)) as http_client:
        with pytest.raises(StructuredProposalValidationError) as error_info:
            OpenAICompatibleLLMClient(
                base_url="https://provider.example/v1/",
                api_key="dummy-secret",
                model="test-model",
                client=http_client,
            ).complete(_messages())

    assert len(requests) == 2
    error_text = str(error_info.value)
    assert "dummy-secret" not in error_text
    assert raw_invalid_body not in error_text
    assert "dummy-secret" not in repr(error_info.value)
    assert raw_invalid_body not in repr(error_info.value)
    chained_errors = tuple(
        chained
        for chained in (
            error_info.value.__cause__,
            error_info.value.__context__,
        )
        if chained is not None
    )
    assert all("dummy-secret" not in repr(chained) for chained in chained_errors)
    assert all(raw_invalid_body not in repr(chained) for chained in chained_errors)
    formatted_traceback = "".join(traceback.format_exception(error_info.value))
    assert "dummy-secret" not in formatted_traceback
    assert raw_invalid_body not in formatted_traceback


def test_top_level_unknown_property_is_rejected_after_retry() -> None:
    requests: list[httpx.Request] = []
    invalid_proposal = dict(VALID_PROPOSAL)
    invalid_proposal["unexpected_top_level"] = "must-not-be-accepted"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json=_provider_response(json.dumps(invalid_proposal)),
        )

    with _client(httpx.MockTransport(handler)) as http_client:
        with pytest.raises(StructuredProposalValidationError) as error_info:
            OpenAICompatibleLLMClient(
                base_url="https://provider.example/v1",
                api_key="dummy-secret",
                model="test-model",
                client=http_client,
            ).complete(_messages())

    assert len(requests) == 2
    assert "unexpected_top_level" in str(error_info.value)
    assert "must-not-be-accepted" not in str(error_info.value)


def test_api_key_in_schema_error_is_redacted_from_feedback_and_traceback() -> None:
    requests: list[dict[str, object]] = []
    invalid_proposal = dict(VALID_PROPOSAL)
    invalid_proposal["field-secret"] = "must-not-leak"

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=_provider_response(json.dumps(invalid_proposal)),
        )

    with _client(httpx.MockTransport(handler)) as http_client:
        with pytest.raises(StructuredProposalValidationError) as error_info:
            OpenAICompatibleLLMClient(
                base_url="https://provider.example/v1/",
                api_key="field-secret",
                model="test-model",
                client=http_client,
            ).complete(_messages())

    assert len(requests) == 2
    retry_messages = requests[1]["messages"]
    feedback = retry_messages[-1]["content"]
    assert isinstance(feedback, str)
    assert feedback.startswith("Structured proposal")
    assert "field-secret" not in feedback
    assert "field-secret" not in str(error_info.value)
    assert "field-secret" not in repr(error_info.value)
    formatted_traceback = "".join(traceback.format_exception(error_info.value))
    assert "field-secret" not in formatted_traceback


def test_settings_repr_and_provider_error_mask_api_secret() -> None:
    settings = Settings(llm_api_key="dummy-secret")
    assert "dummy-secret" not in repr(settings)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_provider_response("{invalid-provider-body"),
        )

    with _client(httpx.MockTransport(handler)) as http_client:
        with pytest.raises(StructuredProposalValidationError) as error_info:
            OpenAICompatibleLLMClient(
                base_url=settings.llm_base_url,
                api_key=settings.llm_api_key.get_secret_value(),
                model=settings.openai_model,
                client=http_client,
            ).complete(_messages())

    assert "dummy-secret" not in str(error_info.value)
    assert "dummy-secret" not in repr(error_info.value)
