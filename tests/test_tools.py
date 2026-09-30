from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
import inspect
from typing import Any
from datetime import datetime, timezone
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from app.agent import (
    AgentLoop,
    AgentMessage,
    AgentProposal,
    MessageRole,
    ProposalPriority,
    ProposalToolCall,
    ProposalValue,
    ToolCall,
    ToolExecutionResult,
)
from app.documents import (
    DocumentMediaType,
    DocumentNormalizer,
    DocumentInput,
)
from app.policy import RiskSignals, TrustedSource, TrustedToolRuntimeContext
from app.observability import (
    EventName,
    ObservationContext,
    Observer,
    OutcomeCode,
    PolicyReason,
    RecordingObserver,
)
from app.tenants.config import ActionRule, TenantConfig
from app.tools import (
    ApprovalRequested,
    CreateCaseArgs,
    CreateReplyDraftArgs,
    CustomerLookupResult,
    FindCustomerArgs,
    FlagForReviewArgs,
    InMemoryTenantToolPort,
    PendingAction,
    PolicyGatedToolExecutor,
    TenantToolPort,
    ToolDefinition,
    UpdateCaseFieldsArgs,
)
from app.tools.models import ReviewFlag
from app.tools.registry import ActionCapability, ActionRegistry, BUILTIN_ACTION_REGISTRY


TENANT_A = UUID("00000000-0000-0000-0000-000000000001")
TENANT_B = UUID("00000000-0000-0000-0000-000000000002")


def _action_capability(key: str) -> ActionCapability:
    return ActionCapability(
        key=key,
        arguments_model=FindCustomerArgs,
        requires_complete_fields=False,
        commits_side_effect=False,
    )


def test_action_registry_rejects_duplicate_keys() -> None:
    capability = _action_capability("find_customer")

    with pytest.raises(ValueError):
        ActionRegistry((capability, capability))


@pytest.mark.parametrize("key", ["BadKey", "bad-key", "bad/key", "1bad", ""])
def test_action_registry_rejects_unsafe_keys(key: str) -> None:
    with pytest.raises(ValueError):
        ActionRegistry((_action_capability(key),))


def test_builtin_action_registry_contains_exact_current_catalog() -> None:
    assert BUILTIN_ACTION_REGISTRY.keys == frozenset(
        {
            "find_customer",
            "create_case",
            "update_case_fields",
            "create_reply_draft",
            "flag_for_review",
        }
    )

    expected_models = {
        "find_customer": FindCustomerArgs,
        "create_case": CreateCaseArgs,
        "update_case_fields": UpdateCaseFieldsArgs,
        "create_reply_draft": CreateReplyDraftArgs,
        "flag_for_review": FlagForReviewArgs,
    }
    for key, arguments_model in expected_models.items():
        assert BUILTIN_ACTION_REGISTRY.require(key).arguments_model is arguments_model

    expected_metadata = {
        "find_customer": (False, False, 1),
        "create_case": (True, True, 1),
        "update_case_fields": (True, True, 1),
        "create_reply_draft": (False, False, 1),
        "flag_for_review": (False, False, 1),
    }
    for key, (requires_complete, commits_side_effect, pending_version) in (
        expected_metadata.items()
    ):
        descriptor = BUILTIN_ACTION_REGISTRY.require(key)
        assert descriptor.requires_complete_fields is requires_complete
        assert descriptor.commits_side_effect is commits_side_effect
        assert descriptor.pending_action_version == pending_version


def test_executor_definitions_are_supplied_by_registry_descriptors(
    tenant_config: TenantConfig,
) -> None:
    executor, _ = build_executor(tenant_config)

    assert set(executor.definitions) == BUILTIN_ACTION_REGISTRY.keys
    for key, definition in executor.definitions.items():
        descriptor = BUILTIN_ACTION_REGISTRY.require(key)
        assert definition.arguments_model is descriptor.arguments_model
        assert definition.requires_complete_fields is descriptor.requires_complete_fields
        assert definition.commits_side_effect is descriptor.commits_side_effect
        assert definition.pending_action_version == descriptor.pending_action_version


def test_pending_action_accepts_safe_unknown_name_and_positive_strict_version() -> None:
    action = PendingAction(
        name="future_action",
        version=2,
        arguments={},
        known_fields={},
    )

    assert action.name == "future_action"
    assert action.version == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "Bad Name"),
        ("name", "bad/name"),
        ("version", 0),
        ("version", -1),
        ("version", "1"),
        ("version", True),
    ],
)
def test_pending_action_rejects_unsafe_name_or_invalid_version(
    field: str,
    value: object,
) -> None:
    payload: dict[str, object] = {
        "name": "future_action",
        "version": 1,
        "arguments": {},
        "known_fields": {},
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        PendingAction.model_validate(payload)


def test_tenant_tool_port_contract_remains_synchronous_and_tenant_scoped() -> None:
    """The async database adapter must stay behind the unchanged Phase-6 port."""
    expected_parameters = {
        "find_customer": ("self", "tenant_id", "email", "external_id"),
        "create_case": ("self", "tenant_id", "source", "customer_id", "fields"),
        "update_case_fields": ("self", "tenant_id", "case_id", "fields"),
        "case_exists": ("self", "tenant_id", "case_id"),
    }

    for method_name, parameter_names in expected_parameters.items():
        method = getattr(TenantToolPort, method_name)
        signature = inspect.signature(method)
        assert tuple(signature.parameters) == parameter_names
        assert not inspect.iscoroutinefunction(method)
        assert signature.parameters["tenant_id"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD

    assert inspect.signature(TenantToolPort.find_customer).parameters[
        "email"
    ].kind is inspect.Parameter.KEYWORD_ONLY
    assert inspect.signature(TenantToolPort.find_customer).parameters[
        "external_id"
    ].kind is inspect.Parameter.KEYWORD_ONLY
    assert inspect.signature(TenantToolPort.create_case).parameters[
        "customer_id"
    ].kind is inspect.Parameter.KEYWORD_ONLY
    assert inspect.signature(TenantToolPort.create_case).parameters[
        "fields"
    ].kind is inspect.Parameter.KEYWORD_ONLY
    assert inspect.signature(TenantToolPort.update_case_fields).parameters[
        "fields"
    ].kind is inspect.Parameter.KEYWORD_ONLY
    assert not inspect.iscoroutinefunction(PolicyGatedToolExecutor.execute)


@pytest.fixture
def tenant_config() -> TenantConfig:
    return TenantConfig.model_validate(
        {
            "profile_version": 2,
            "slug": "acme",
            "scenario_key": "test_scenario",
            "display_name": "Acme",
            "intake_types": [
                {
                    "name": "request",
                    "description": "A customer request",
                    "required_fields": ["summary"],
                }
            ],
            "fields": {
                "summary": {"type": "short_text", "label": "Summary"},
                "contact": {"type": "email", "label": "Contact"},
            },
            "action_policy": {
                "find_customer": {
                    "allowed": True,
                    "requires_approval": False,
                    "execution": "executable",
                },
                "create_case": {
                    "allowed": True,
                    "requires_approval": False,
                    "execution": "executable",
                },
                "update_case_fields": {
                    "allowed": True,
                    "requires_approval": False,
                    "execution": "executable",
                },
                "create_reply_draft": {
                    "allowed": True,
                    "requires_approval": False,
                    "execution": "executable",
                },
                "flag_for_review": {
                    "allowed": True,
                    "requires_approval": False,
                    "execution": "executable",
                },
                "send_reply": {
                    "allowed": True,
                    "requires_approval": False,
                    "execution": "policy_only",
                },
            },
            "documents": {},
            "routing": {
                "outcome_names": [
                    "urgent",
                    "awaiting_input",
                    "pending_approval",
                    "ready",
                    "rejected",
                ],
                "always_approval_actions": ["send_reply"],
            },
        }
    )


@pytest.fixture
def document_tenant_config(tenant_config: TenantConfig) -> TenantConfig:
    profile = tenant_config.model_dump(mode="json")
    profile["intake_types"].append(  # type: ignore[union-attr]
        {
            "name": "rate_confirmation",
            "description": "A freight rate confirmation document",
            "required_fields": ["summary"],
        }
    )
    return TenantConfig.model_validate(profile)


def make_proposal(
    *,
    intake_type: str | None = "request",
    fields: Iterable[tuple[str, Any]] = (("summary", "Customer request"),),
    source_excerpts: Mapping[str, str | None] | None = None,
    tool_name: str | None = None,
    tool_arguments: Iterable[tuple[str, Any]] = (),
    injection: bool = False,
    missing_required_fields: Iterable[str] = (),
    rationale: str = "Structured tool proposal.",
) -> AgentProposal:
    tool_calls: list[dict[str, object]] = []
    if tool_name is not None:
        tool_calls.append(
            {
                "name": tool_name,
                "arguments": [
                    {"name": name, "value": value}
                    for name, value in tool_arguments
                ],
            }
        )
    return AgentProposal.model_validate(
        {
            "intake_type": intake_type,
            "fields": [
                {
                    "name": name,
                    "value": value,
                    **(
                        {"source_excerpt": source_excerpts[name]}
                        if source_excerpts is not None and name in source_excerpts
                        else {}
                    ),
                }
                for name, value in fields
            ],
            "missing_required_fields": list(missing_required_fields),
            "priority": ProposalPriority.NORMAL,
            "contains_injection_or_override_attempt": injection,
            "rationale_short": rationale,
            "tool_calls": tool_calls,
            "confidence": 0.8,
        }
    )


def make_runtime(
    tenant_config: TenantConfig,
    *,
    tenant_id: UUID = TENANT_A,
    risk: bool = False,
    source: TrustedSource | None = None,
) -> TrustedToolRuntimeContext:
    return TrustedToolRuntimeContext(
        tenant_id=tenant_id,
        tenant_config=tenant_config,
        source=source
        or TrustedSource(
            channel="email",
            subject="Trusted subject",
            body="Trusted source body",
        ),
        risk_signals=RiskSignals(safety_or_legal_risk=risk),
    )


def make_document(
    *,
    text: str | None = "Summary: Customer request\nContact: customer@example.com",
    content: bytes | None = None,
    media_type: DocumentMediaType = DocumentMediaType.TEXT,
):
    payload: dict[str, object] = {
        "channel": "email",
        "subject": "Rate confirmation",
        "body": "Please process this document.",
        "media_type": media_type,
    }
    if text is not None:
        payload["text"] = text
    if content is not None:
        payload["content"] = content
    return DocumentNormalizer().normalize(
        DocumentInput.model_validate(payload),
        document_kind="rate_confirmation",
        target_intake_type="rate_confirmation",
        normalizer_key="bounded_text_pdf",
        normalizer_version=1,
    )


class SpyPort(InMemoryTenantToolPort):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.approval_requests: list[tuple[UUID, PendingAction, str]] = []

    def create_case(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        self.calls.append("create_case")
        return super().create_case(*args, **kwargs)

    def update_case_fields(
        self, *args: Any, **kwargs: Any
    ):  # type: ignore[no-untyped-def]
        self.calls.append("update_case_fields")
        return super().update_case_fields(*args, **kwargs)

    def request_approval(
        self,
        tenant_id: UUID,
        *,
        action: PendingAction,
        policy_reason: str,
    ) -> ApprovalRequested:
        self.approval_requests.append((tenant_id, action, policy_reason))
        return ApprovalRequested(
            id=UUID("00000000-0000-0000-0000-000000000008"),
            expires_at=datetime(2026, 9, 24, tzinfo=timezone.utc),
        )


class ExplodingFindCustomerPort(InMemoryTenantToolPort):
    def find_customer(
        self,
        tenant_id: UUID,
        *,
        email: str | None,
        external_id: str | None,
    ) -> Any:
        raise RuntimeError("backend secret-token=do-not-leak")


def build_executor(
    tenant_config: TenantConfig,
    port: InMemoryTenantToolPort | None = None,
    *,
    tenant_id: UUID = TENANT_A,
    risk: bool = False,
    source: TrustedSource | None = None,
    observer: Observer | None = None,
    context: ObservationContext | None = None,
    clock: object | None = None,
) -> tuple[PolicyGatedToolExecutor, InMemoryTenantToolPort]:
    actual_port = port or InMemoryTenantToolPort()
    options: dict[str, object] = {}
    if observer is not None:
        options["observer"] = observer
    if context is not None:
        options["context"] = context
    if clock is not None:
        options["clock"] = clock
    return (
        PolicyGatedToolExecutor(
            runtime=make_runtime(
                tenant_config,
                tenant_id=tenant_id,
                risk=risk,
                source=source,
            ),
            port=actual_port,
            **options,
        ),
        actual_port,
    )


def test_find_customer_is_tenant_scoped(
    tenant_config: TenantConfig,
) -> None:
    port = InMemoryTenantToolPort()
    customer_a = port.add_customer(
        TENANT_A,
        external_id="shared-external",
        email="same@example.com",
        name="Tenant A",
    )
    port.add_customer(
        TENANT_B,
        external_id="other-external",
        email="other@example.com",
        name="Tenant B",
    )
    executor_a, _ = build_executor(tenant_config, port)

    found = executor_a.execute(
        make_proposal(fields=()),
        ToolCall(name="find_customer", arguments={"external_id": "shared-external"}),
    )
    assert found.data == {
        "found": True,
        "customer": customer_a.model_dump(mode="json"),
    }

    other_tenant = executor_a.execute(
        make_proposal(fields=()),
        ToolCall(name="find_customer", arguments={"email": "other@example.com"}),
    )
    assert other_tenant.data == {"found": False, "customer": None}


def test_update_case_fields_rejects_cross_tenant_case_without_mutation(
    tenant_config: TenantConfig,
) -> None:
    port = InMemoryTenantToolPort()
    case_id = port.seed_case(TENANT_B, fields={"summary": "original"})
    before = deepcopy(port.cases[case_id].extracted_fields)
    executor_a, _ = build_executor(tenant_config, port)

    result = executor_a.execute(
        make_proposal(fields=(("summary", "attacker update"),)),
        ToolCall(name="update_case_fields", arguments={"case_id": str(case_id)}),
    )

    assert result.data == {"outcome": "case_not_found"}
    assert result.continue_run is False
    assert result.final_response == "case_not_found"
    assert port.cases[case_id].extracted_fields == before


def test_create_case_uses_trusted_source_and_only_declared_fields(
    tenant_config: TenantConfig,
) -> None:
    port = InMemoryTenantToolPort()
    source = TrustedSource(
        channel="trusted-channel",
        subject="trusted-subject",
        body="trusted-body",
    )
    executor, _ = build_executor(tenant_config, port, source=source)
    proposal = make_proposal(
        fields=(
            ("summary", "declared summary"),
            ("contact", "customer@example.com"),
            ("not_a_profile_field", "must not persist"),
        )
    )

    created = executor.execute(
        proposal,
        ToolCall(name="create_case", arguments={}),
    )

    assert created.data is not None
    case_id = UUID(created.data["id"])
    record = port.cases[case_id]
    assert (record.channel, record.subject, record.body) == (
        source.channel,
        source.subject,
        source.body,
    )
    assert record.extracted_fields == {
        "summary": "declared summary",
        "contact": "customer@example.com",
    }


def test_document_candidates_with_exact_verified_excerpts_can_create_case(
    tenant_config: TenantConfig,
) -> None:
    port = SpyPort()
    source = TrustedSource(
        channel="trusted-channel",
        subject="trusted-subject",
        body="trusted-body",
        document=make_document(),
    )
    executor, _ = build_executor(tenant_config, port, source=source)
    proposal = make_proposal(
        fields=(
            ("summary", "Customer request"),
            ("contact", "customer@example.com"),
            ("not_a_profile_field", "must not persist"),
        ),
        source_excerpts={
            "summary": "Summary: Customer request",
            "contact": "Contact: customer@example.com",
            "not_a_profile_field": "Customer request",
        },
    )

    created = executor.execute(proposal, ToolCall(name="create_case", arguments={}))

    assert created.data is not None
    case_id = UUID(created.data["id"])
    assert port.calls == ["create_case"]
    assert port.cases[case_id].extracted_fields == {
        "summary": "Customer request",
        "contact": "customer@example.com",
    }


def test_document_candidates_without_matching_excerpts_await_input_without_mutation(
    tenant_config: TenantConfig,
) -> None:
    port = SpyPort()
    source = TrustedSource(
        channel="trusted-channel",
        subject="trusted-subject",
        body="trusted-body",
        document=make_document(),
    )
    executor, _ = build_executor(tenant_config, port, source=source)
    proposal = make_proposal(
        fields=(
            ("summary", "Customer request"),
            ("contact", "   "),
            ("not_a_profile_field", "attacker value"),
        ),
        source_excerpts={
            "summary": "not present in the trusted document",
            "contact": "Contact: customer@example.com",
            "not_a_profile_field": "Summary: Customer request",
        },
    )

    result = executor.execute(proposal, ToolCall(name="create_case", arguments={}))

    assert result.data == {
        "decision": "deny",
        "status": "awaiting_input",
        "reason": "missing_required_fields",
        "missing_required_fields": ["summary"],
    }
    assert result.continue_run is False
    assert result.final_response == "missing_required_fields"
    assert port.calls == []
    assert port.approval_requests == []
    assert port.cases == {}


def test_document_injection_signal_preserves_review_precedence(
    tenant_config: TenantConfig,
) -> None:
    port = SpyPort()
    source = TrustedSource(
        channel="trusted-channel",
        subject="trusted-subject",
        body="trusted-body",
        document=make_document(),
    )
    executor, _ = build_executor(tenant_config, port, source=source)
    proposal = make_proposal(
        injection=True,
        source_excerpts={"summary": "Summary: Customer request"},
    )

    result = executor.execute(proposal, ToolCall(name="create_case", arguments={}))

    assert result.data == {
        "decision": "needs_approval",
        "status": "urgent",
        "reason": "safety_or_legal_risk",
        "missing_required_fields": [],
        "approval_id": "00000000-0000-0000-0000-000000000008",
    }
    assert result.continue_run is False
    assert result.final_response == "approval_requested"
    assert port.calls == []
    assert port.cases == {}
    assert len(port.approval_requests) == 1


def test_unreadable_document_awaits_input_without_handler_or_approval(
    document_tenant_config: TenantConfig,
) -> None:
    port = SpyPort()
    source = TrustedSource(
        channel="trusted-channel",
        subject="trusted-subject",
        body="trusted-body",
        document=make_document(
            media_type=DocumentMediaType.PDF,
            text=None,
            content=b"malformed-rate-confirmation",
        ),
    )
    executor, _ = build_executor(document_tenant_config, port, source=source)

    result = executor.execute(
        make_proposal(source_excerpts={"summary": "Summary: Customer request"}),
        ToolCall(name="create_case", arguments={}),
    )

    assert result.data == {
        "decision": "deny",
        "status": "awaiting_input",
        "reason": "document_unreadable",
        "missing_required_fields": ["summary"],
    }
    assert result.continue_run is False
    assert result.final_response == "document_unreadable"
    assert port.calls == []
    assert port.approval_requests == []
    assert port.cases == {}


def test_create_case_rejects_malicious_source_arguments_and_extra_keys(
    tenant_config: TenantConfig,
) -> None:
    port = InMemoryTenantToolPort()
    executor, _ = build_executor(tenant_config, port)

    result = executor.execute(
        make_proposal(),
        ToolCall(
            name="create_case",
            arguments={
                "channel": "attacker-channel",
                "subject": "attacker-subject",
                "body": "attacker-body",
            },
        ),
    )

    assert result.data == {"outcome": "tool_arguments_invalid"}
    assert result.continue_run is False
    assert port.cases == {}


def test_create_case_rejects_cross_tenant_customer_without_mutation(
    tenant_config: TenantConfig,
) -> None:
    port = InMemoryTenantToolPort()
    customer = port.add_customer(
        TENANT_B,
        email="other-tenant@example.com",
        name="Other tenant customer",
    )
    executor, _ = build_executor(tenant_config, port)

    result = executor.execute(
        make_proposal(),
        ToolCall(
            name="create_case",
            arguments={"customer_id": customer.id},
        ),
    )

    assert result.data == {"outcome": "customer_not_found"}
    assert result.continue_run is False
    assert result.final_response == "customer_not_found"
    assert port.cases == {}


def test_flag_for_review_returns_typed_json_result(
    tenant_config: TenantConfig,
) -> None:
    executor, _ = build_executor(tenant_config)

    result = executor.execute(
        make_proposal(),
        ToolCall(
            name="flag_for_review",
            arguments={"note": "Manual review required"},
        ),
    )

    assert result.data == {
        "reason": "Manual review required",
        "persisted": False,
    }


def test_backend_exception_is_sanitized_and_stops_execution(
    tenant_config: TenantConfig,
) -> None:
    executor, _ = build_executor(tenant_config, ExplodingFindCustomerPort())

    result = executor.execute(
        make_proposal(fields=()),
        ToolCall(
            name="find_customer",
            arguments={"email": "customer@example.com"},
        ),
    )

    assert result.data == {"outcome": "tool_execution_failed"}
    assert result.continue_run is False
    assert result.final_response == "tool_execution_failed"
    assert "secret-token" not in repr(result.data)
    assert "do-not-leak" not in repr(result.data)


def test_policy_and_tool_events_are_typed_and_content_free(
    tenant_config: TenantConfig,
) -> None:
    observer = RecordingObserver()
    trace_id = UUID("00000000-0000-0000-0000-000000000099")
    executor, _ = build_executor(
        tenant_config,
        observer=observer,
        context=ObservationContext(trace_id=trace_id),
        source=TrustedSource(
            channel="email",
            subject="SOURCE_SUBJECT_SECRET",
            body="SOURCE_BODY_SECRET",
        ),
    )

    result = executor.execute(
        make_proposal(
            fields=(),
            rationale="RATIONALE_SECRET",
        ),
        ToolCall(name="find_customer", arguments={"email": "missing@example.test"}),
    )

    assert result.data == {"found": False, "customer": None}
    policy_events = [
        event
        for event in observer.events
        if event.event is EventName.POLICY_EVALUATED
    ]
    tool_events = [
        event
        for event in observer.events
        if event.event is EventName.TOOL_EXECUTION_COMPLETED
    ]
    assert len(policy_events) == len(tool_events) == 1
    assert policy_events[0].policy_decision.value == "allow"
    assert policy_events[0].routing_status.value == "ready"
    assert policy_events[0].policy_reason is PolicyReason.ACTION_ALLOWED
    assert tool_events[0].action_key == "find_customer"
    assert tool_events[0].action_known is True
    assert tool_events[0].outcome is OutcomeCode.EXECUTED
    assert tool_events[0].result_is_none is False
    assert tool_events[0].side_effect_committed is False
    assert trace_id in {event.trace_id for event in observer.events}
    for event in observer.events:
        serialized = event.model_dump_json()
        assert "SOURCE_BODY_SECRET" not in serialized
        assert "SOURCE_SUBJECT_SECRET" not in serialized
        assert "RATIONALE_SECRET" not in serialized
        assert "arguments" not in event.__class__.model_fields
        assert "result" not in event.__class__.model_fields


def test_unknown_model_action_emits_known_flag_without_raw_name(
    tenant_config: TenantConfig,
) -> None:
    observer = RecordingObserver()
    executor, _ = build_executor(tenant_config, observer=observer)
    unknown_action = "model_invented_action"

    result = executor.execute(
        make_proposal(tool_name=unknown_action),
        ToolCall(name=unknown_action, arguments={}),
    )

    assert result.data["reason"] == "action_not_configured"
    assert [event.event for event in observer.events] == [
        EventName.POLICY_EVALUATED
    ]
    assert observer.events[0].policy_decision.value == "deny"
    assert observer.events[0].action_key is None
    assert observer.events[0].action_known is False
    assert unknown_action not in observer.events[0].model_dump_json()


def test_approval_event_is_emitted_after_durable_request_without_payload(
    tenant_config: TenantConfig,
) -> None:
    observer = RecordingObserver()
    port = SpyPort()
    executor, _ = build_executor(
        tenant_config,
        port,
        risk=True,
        observer=observer,
    )

    result = executor.execute(
        make_proposal(rationale="APPROVAL_RATIONALE_SECRET"),
        ToolCall(name="create_case", arguments={}),
    )

    assert result.final_response == "approval_requested"
    assert len(port.approval_requests) == 1
    approval_events = [
        event
        for event in observer.events
        if event.event is EventName.APPROVAL_REQUESTED
    ]
    assert len(approval_events) == 1
    assert approval_events[0].approval_id == UUID(
        "00000000-0000-0000-0000-000000000008"
    )
    assert approval_events[0].action_key == "create_case"
    assert approval_events[0].action_known is True
    assert approval_events[0].policy_reason is PolicyReason.SAFETY_OR_LEGAL_RISK
    assert not any(
        event.event is EventName.TOOL_EXECUTION_COMPLETED
        for event in observer.events
    )
    assert all(
        "APPROVAL_RATIONALE_SECRET" not in event.model_dump_json()
        for event in observer.events
    )


def test_tool_events_distinguish_none_result_and_backend_failure(
    tenant_config: TenantConfig,
) -> None:
    observer = RecordingObserver()
    executor, _ = build_executor(tenant_config, observer=observer)

    class _EmptyArguments(BaseModel):
        model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    def return_none(
        arguments: BaseModel,
        proposal: AgentProposal,
        runtime: TrustedToolRuntimeContext,
    ) -> None:
        del arguments, proposal, runtime
        return None

    executor._definitions["find_customer"] = ToolDefinition(
        name="find_customer",
        requires_complete_fields=False,
        arguments_model=_EmptyArguments,
        handler=return_none,
    )
    none_result = executor.execute(
        make_proposal(fields=()),
        ToolCall(name="find_customer", arguments={}),
    )
    assert none_result.data is None
    none_event = [
        event
        for event in observer.events
        if event.event is EventName.TOOL_EXECUTION_COMPLETED
    ][-1]
    assert none_event.result_is_none is True
    assert none_event.outcome is OutcomeCode.NO_RESULT

    failure_observer = RecordingObserver()
    failure_executor, _ = build_executor(
        tenant_config,
        ExplodingFindCustomerPort(),
        observer=failure_observer,
    )
    failed = failure_executor.execute(
        make_proposal(fields=()),
        ToolCall(name="find_customer", arguments={"email": "x@example.test"}),
    )
    assert failed.data == {"outcome": "tool_execution_failed"}
    failure_event = next(
        event
        for event in failure_observer.events
        if event.event is EventName.TOOL_EXECUTION_COMPLETED
    )
    assert failure_event.outcome is OutcomeCode.BACKEND_FAILURE
    assert "backend secret-token=do-not-leak" not in failure_event.model_dump_json()


def test_tool_event_side_effect_comes_from_registry_descriptor(
    tenant_config: TenantConfig,
) -> None:
    observer = RecordingObserver()
    executor, _ = build_executor(tenant_config, observer=observer)

    result = executor.execute(
        make_proposal(),
        ToolCall(name="create_case", arguments={}),
    )

    assert result.data is not None
    event = next(
        event
        for event in observer.events
        if event.event is EventName.TOOL_EXECUTION_COMPLETED
    )
    descriptor = BUILTIN_ACTION_REGISTRY.require("create_case")
    assert event.action_key == descriptor.key
    assert event.action_known is True
    assert event.side_effect_committed is descriptor.commits_side_effect


def test_create_case_without_trusted_source_stops_without_mutation(
    tenant_config: TenantConfig,
) -> None:
    port = SpyPort()
    executor = PolicyGatedToolExecutor(
        runtime=TrustedToolRuntimeContext(
            tenant_id=TENANT_A,
            tenant_config=tenant_config,
            source=None,
        ),
        port=port,
    )

    result = executor.execute(
        make_proposal(),
        ToolCall(name="create_case", arguments={}),
    )

    assert result.data == {"outcome": "trusted_source_unavailable"}
    assert result.continue_run is False
    assert result.final_response == "trusted_source_unavailable"
    assert port.calls == []
    assert port.cases == {}


def test_invalid_closed_arguments_stop_without_mutating_existing_case(
    tenant_config: TenantConfig,
) -> None:
    port = InMemoryTenantToolPort()
    case_id = port.seed_case(TENANT_A, fields={"summary": "original"})
    before = deepcopy(port.cases[case_id].extracted_fields)
    executor, _ = build_executor(tenant_config, port)

    result = executor.execute(
        make_proposal(fields=(("summary", "should not write"),)),
        ToolCall(
            name="update_case_fields",
            arguments={"case_id": str(case_id), "fields": {"summary": "evil"}},
        ),
    )

    assert result.data == {"outcome": "tool_arguments_invalid"}
    assert result.continue_run is False
    assert port.cases[case_id].extracted_fields == before


def test_transient_draft_and_review_results_create_no_records(
    tenant_config: TenantConfig,
) -> None:
    port = InMemoryTenantToolPort()
    executor, _ = build_executor(tenant_config, port)
    before_customers = deepcopy(port.customers)
    before_cases = deepcopy(port.cases)

    draft = executor.execute(
        make_proposal(fields=(("contact", "customer@example.com"),)),
        ToolCall(name="create_reply_draft", arguments={}),
    )
    review = executor.execute(
        make_proposal(),
        ToolCall(name="flag_for_review", arguments={"note": "Needs review"}),
    )

    assert draft.data is not None
    assert draft.data["persisted"] is False
    assert review.data == {"reason": "Needs review", "persisted": False}
    assert port.customers == before_customers
    assert port.cases == before_cases


def test_create_reply_draft_rejects_cross_tenant_case(
    tenant_config: TenantConfig,
) -> None:
    port = InMemoryTenantToolPort()
    case_id = port.seed_case(TENANT_B, fields={"summary": "Other tenant"})
    executor, _ = build_executor(tenant_config, port)

    result = executor.execute(
        make_proposal(fields=(
            ("summary", "Customer request"),
            ("contact", "customer@example.com"),
        )),
        ToolCall(
            name="create_reply_draft",
            arguments={"case_id": str(case_id)},
        ),
    )

    assert result.data == {"outcome": "case_not_found"}
    assert result.continue_run is False
    assert result.final_response == "case_not_found"


def test_reply_draft_ignores_unknown_contact_and_model_missing_fields(
    tenant_config: TenantConfig,
) -> None:
    config_data = tenant_config.model_dump()
    config_data["fields"].pop("contact")
    config = TenantConfig.model_validate(config_data)
    executor, _ = build_executor(config)

    result = executor.execute(
        make_proposal(
            fields=(("contact", "attacker@example.com"),),
            missing_required_fields=("model_fabricated_field",),
            rationale="Please provide the missing request details.",
        ),
        ToolCall(name="create_reply_draft", arguments={}),
    )

    assert result.data is not None
    assert result.data["recipient"] is None
    assert "summary" in result.data["body"]
    assert "model_fabricated_field" not in result.data["body"]


@pytest.mark.parametrize(
    "summary_value",
    [None, "", []],
    ids=["null", "blank_string", "empty_list"],
)
def test_incomplete_required_value_denies_create_case_without_mutation(
    tenant_config: TenantConfig,
    summary_value: object,
) -> None:
    port = SpyPort()
    executor, _ = build_executor(tenant_config, port)

    result = executor.execute(
        make_proposal(
            fields=(("summary", summary_value),),
            missing_required_fields=(),
        ),
        ToolCall(name="create_case", arguments={}),
    )

    assert result.data == {
        "decision": "deny",
        "status": "awaiting_input",
        "reason": "missing_required_fields",
        "missing_required_fields": ["summary"],
    }
    assert result.continue_run is False
    assert result.final_response == "missing_required_fields"
    assert port.calls == []
    assert port.cases == {}


@pytest.mark.parametrize(
    ("found", "customer"),
    [(True, None), (False, {"id": "customer-id"})],
    ids=["found_without_customer", "customer_without_found"],
)
def test_customer_lookup_result_rejects_inconsistent_found_flag(
    found: bool,
    customer: dict[str, str] | None,
) -> None:
    with pytest.raises(ValidationError):
        CustomerLookupResult.model_validate(
            {"found": found, "customer": customer}
        )


@pytest.mark.parametrize(
    "reason",
    ["", "x" * 1001],
    ids=["blank", "overlong"],
)
def test_review_flag_rejects_blank_or_overlong_reason(reason: str) -> None:
    with pytest.raises(ValidationError):
        ReviewFlag.model_validate({"reason": reason})


def test_risk_review_stops_before_handler(
    tenant_config: TenantConfig,
) -> None:
    port = SpyPort()
    executor, _ = build_executor(tenant_config, port, risk=True)

    result = executor.execute(
        make_proposal(),
        ToolCall(name="create_case", arguments={}),
    )

    assert result.data["decision"] == "needs_approval"
    assert result.data["reason"] == "safety_or_legal_risk"
    assert result.data["approval_id"] == "00000000-0000-0000-0000-000000000008"
    assert result.continue_run is False
    assert result.final_response == "approval_requested"
    assert port.calls == []
    assert port.cases == {}
    assert len(port.approval_requests) == 1


def test_yaml_approval_for_registered_create_case_persists_closed_pending_action(
    tenant_config: TenantConfig,
) -> None:
    """A policy-held registered action is frozen for approval and never runs its handler."""
    config_data = tenant_config.model_dump()
    config_data["action_policy"]["create_case"] = {
        "allowed": True,
        "requires_approval": True,
        "execution": "executable",
    }
    config = TenantConfig.model_validate(config_data)
    port = SpyPort()
    executor, _ = build_executor(config, port)
    proposal = make_proposal(
        fields=(
            ("summary", "declared summary"),
            ("contact", "customer@example.com"),
            ("not_a_profile_field", "must not persist"),
        )
    )

    result = executor.execute(proposal, ToolCall(name="create_case", arguments={}))

    assert result.data == {
        "decision": "needs_approval",
        "status": "pending_approval",
        "reason": "approval_required",
        "missing_required_fields": [],
        "approval_id": "00000000-0000-0000-0000-000000000008",
    }
    assert result.continue_run is False
    assert result.final_response == "approval_requested"
    assert port.calls == []
    assert port.cases == {}
    assert len(port.approval_requests) == 1
    tenant_id, pending, policy_reason = port.approval_requests[0]
    assert tenant_id == TENANT_A
    assert policy_reason == "approval_required"
    assert pending.name == "create_case"
    assert set(pending.arguments) <= {"customer_id"}
    assert pending.known_fields == {
        "summary": "declared summary",
        "contact": "customer@example.com",
    }


def test_critical_registered_create_case_requires_approval_without_running_handler(
    tenant_config: TenantConfig,
) -> None:
    """A critical proposal reaches the approval port after normal validation gates."""
    port = SpyPort()
    executor, _ = build_executor(tenant_config, port)
    proposal = make_proposal().model_copy(update={"priority": ProposalPriority.CRITICAL})

    result = executor.execute(proposal, ToolCall(name="create_case", arguments={}))

    assert result.data is not None
    assert result.data["decision"] == "needs_approval"
    assert result.data["status"] == "pending_approval"
    assert result.data["reason"] == "approval_required"
    assert result.data["approval_id"] == "00000000-0000-0000-0000-000000000008"
    assert result.continue_run is False
    assert port.calls == []
    assert port.cases == {}
    assert len(port.approval_requests) == 1


def test_invalid_create_case_arguments_do_not_create_an_approval(
    tenant_config: TenantConfig,
) -> None:
    """Approval persistence happens only after the registered tool arguments validate."""
    config_data = tenant_config.model_dump()
    config_data["action_policy"]["create_case"] = {
        "allowed": True,
        "requires_approval": True,
        "execution": "executable",
    }
    config = TenantConfig.model_validate(config_data)
    port = SpyPort()
    executor, _ = build_executor(config, port)

    result = executor.execute(
        make_proposal(),
        ToolCall(name="create_case", arguments={"body": "untrusted"}),
    )

    assert result.data == {"outcome": "tool_arguments_invalid"}
    assert result.continue_run is False
    assert result.final_response == "tool_arguments_invalid"
    assert port.approval_requests == []
    assert port.calls == []
    assert port.cases == {}


def test_disallowed_action_stops_before_handler(
    tenant_config: TenantConfig,
) -> None:
    disallowed_data = tenant_config.model_dump()
    disallowed_data["action_policy"]["create_case"] = {
        "allowed": False,
        "requires_approval": False,
        "execution": "executable",
    }
    disallowed = TenantConfig.model_validate(disallowed_data)
    port = SpyPort()
    executor, _ = build_executor(disallowed, port)

    result = executor.execute(
        make_proposal(),
        ToolCall(name="create_case", arguments={}),
    )

    assert result.data["decision"] == "deny"
    assert result.data["reason"] == "action_not_allowed"
    assert result.continue_run is False
    assert port.calls == []


@pytest.mark.parametrize("action", ["send_reply", "unknown_action"])
def test_send_reply_and_unknown_action_never_run(
    tenant_config: TenantConfig,
    action: str,
) -> None:
    port = SpyPort()
    executor, _ = build_executor(tenant_config, port)

    result = executor.execute(make_proposal(), ToolCall(name=action))

    assert result.data["decision"] == "deny"
    assert result.data["reason"] == "action_not_configured"
    assert result.continue_run is False
    assert port.calls == []
    assert port.cases == {}


class EmptyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SequenceLLM:
    def __init__(self, proposals: Sequence[AgentProposal]) -> None:
        self.proposals = list(proposals)
        self.requests: list[tuple[AgentMessage, ...]] = []

    def complete(self, messages: Sequence[AgentMessage]) -> AgentProposal:
        self.requests.append(tuple(messages))
        return self.proposals.pop(0)


def test_executed_handler_none_is_preserved_in_loop_transcript(
    tenant_config: TenantConfig,
) -> None:
    config_data = tenant_config.model_dump()
    config_data["action_policy"]["return_none"] = {
        "allowed": True,
        "requires_approval": False,
        "execution": "executable",
    }
    config = TenantConfig.model_validate(config_data)
    executor, _ = build_executor(config)

    def return_none(
        arguments: BaseModel,
        proposal: AgentProposal,
        runtime: TrustedToolRuntimeContext,
    ) -> None:
        return None

    executor._definitions["return_none"] = ToolDefinition(
        name="return_none",
        requires_complete_fields=False,
        arguments_model=EmptyArguments,
        handler=return_none,
    )
    tool_proposal = make_proposal(tool_name="return_none")
    final_proposal = make_proposal(rationale="Finished after an empty result")
    loop = AgentLoop(SequenceLLM([tool_proposal, final_proposal]), executor)

    result = loop.run([])

    assert result.messages[1].role is MessageRole.TOOL
    assert result.messages[1].tool_call == ToolCall(name="return_none")
    assert result.messages[1].tool_result is None
    assert result.final_response == final_proposal.rationale_short
