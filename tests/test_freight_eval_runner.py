from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.agent import (
    AgentLoop,
    AgentMessage,
    AgentProposal,
    MessageRole,
    ProposalPriority,
    StopReason,
    ToolCall,
    ToolExecutionResult,
)
from app.documents import DocumentMediaType
from app.observability import EventName, ObservationContext, RecordingObserver
from app.policy import (
    PolicyInput,
    PolicyEngine,
    RiskSignals,
    TrustedSource,
    TrustedToolRuntimeContext,
)
from app.tenants.config import TenantConfig
from app.tools import InMemoryTenantToolPort
from evals.freight.loader import ResolvedDocumentFixture
from evals.freight.models import (
    FreightEvalCase,
)
from evals.core import RecordingPolicy, ScriptedLLM
from evals.freight.runner import FreightEvalExecutionError, run_freight_eval_case


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCUMENT_FIXTURES = PROJECT_ROOT / "evals" / "fixtures" / "docs"
TENANT_ID = UUID("00000000-0000-0000-0000-000000000011")


def _proposal(
    *,
    intake_type: str = "load_request",
    fields: Mapping[str, object] | None = None,
    tool_name: str | None = "create_case",
    injection: bool = False,
    tool_arguments: Sequence[dict[str, object]] = (),
    rationale: str = "Synthetic typed proposal.",
) -> AgentProposal:
    values = fields or {}
    tool_calls: list[dict[str, object]] = []
    if tool_name is not None:
        tool_calls.append(
            {
                "name": tool_name,
                "arguments": list(tool_arguments),
            }
        )
    return AgentProposal.model_validate(
        {
            "intake_type": intake_type,
            "fields": [
                {"name": name, "value": value}
                for name, value in values.items()
            ],
            "missing_required_fields": [],
            "priority": ProposalPriority.NORMAL,
            "contains_injection_or_override_attempt": injection,
            "rationale_short": rationale,
            "tool_calls": tool_calls,
            "confidence": 0.9,
        }
    )


def _complete_load_proposal(*, injection: bool = False) -> AgentProposal:
    return _proposal(
        injection=injection,
        fields={
            "origin": "Chicago",
            "destination": "Detroit",
            "equipment": "dry_van",
            "pickup_window": "2026-10-01T09:00:00Z",
            "commodity": "appliances",
            "contact": "ops@example.test",
        },
    )


def _complete_document_proposal() -> AgentProposal:
    excerpts = {
        "origin": "Origin: Chicago",
        "destination": "Destination: Detroit",
        "equipment": "Equipment: dry_van",
        "pickup_window": "Pickup window: 2026-10-01T09:00:00Z",
        "commodity": "Commodity: appliances",
        "contact": "Contact: ops@example.test",
        "quoted_rate": "Quoted rate: 2500 USD",
        "valid_until": "Valid until: 2026-09-30T17:00:00Z",
    }
    fields = {
        name: value
        for name, value in (
            ("origin", "Chicago"),
            ("destination", "Detroit"),
            ("equipment", "dry_van"),
            ("pickup_window", "2026-10-01T09:00:00Z"),
            ("commodity", "appliances"),
            ("contact", "ops@example.test"),
            ("quoted_rate", 2500),
            ("valid_until", "2026-09-30T17:00:00Z"),
        )
    }
    return AgentProposal.model_validate(
        {
            **_complete_load_proposal().model_dump(mode="python"),
            "intake_type": "rate_confirmation",
            "fields": [
                {
                    "name": name,
                    "value": value,
                    "source_excerpt": excerpts[name],
                }
                for name, value in fields.items()
            ],
        }
    )


def _case(
    *,
    case_id: str,
    source: dict[str, object],
    proposal: AgentProposal | None = None,
    proposals: Sequence[AgentProposal] | None = None,
    kind: str = "agent",
    category: str = "body_only",
    expected: dict[str, object] | None = None,
    adversarial: bool = False,
    claim_level: str | None = None,
) -> FreightEvalCase:
    if proposals is None:
        proposals = () if proposal is None else (proposal,)
    if claim_level is None:
        claim_level = (
            "downstream_from_typed_proposal"
            if kind == "agent"
            else "deterministic_server_invariant"
        )
    expected_values = dict(
        expected
        or {
            "intake_type": "load_request",
            "missing_required_fields": [],
            "policy_decision": "allow",
            "routing_status": "ready",
            "routing_reason": "action_allowed",
            "tool_name": "create_case",
            "approval_required": False,
            "case_created": True,
            "provider_calls": 0,
            "llm_calls": 2,
        }
    )
    if source.get("kind") == "inbound_webhook":
        expected_values.setdefault("signature_verified", True)
    return FreightEvalCase.model_validate(
        {
            "id": case_id,
            "category": category,
            "language": "en",
            "kind": kind,
            "source": source,
            "scripted_proposals": list(proposals),
            "expected": expected_values,
            "metadata": {
                "adversarial": adversarial,
                "claim_level": claim_level,
            },
        }
    )


@pytest.fixture
def tenant_config() -> TenantConfig:
    from app.tenants.loader import load_tenant_config

    return load_tenant_config(PROJECT_ROOT / "examples" / "freight-broker.yaml")


@pytest.fixture
def document_fixtures() -> dict[str, ResolvedDocumentFixture]:
    return {
        "complete-en": ResolvedDocumentFixture(
            id="complete-en",
            path=DOCUMENT_FIXTURES / "01-complete-en.txt",
            media_type=DocumentMediaType.TEXT,
        ),
        "malformed-pdf": ResolvedDocumentFixture(
            id="malformed-pdf",
            path=DOCUMENT_FIXTURES / "08-malformed.pdf",
            media_type=DocumentMediaType.PDF,
        ),
    }


class _NoneExecutor:
    def execute(
        self,
        proposal: AgentProposal,
        tool_call: ToolCall,
    ) -> ToolExecutionResult:
        del proposal, tool_call
        return ToolExecutionResult(data=None)


class _StopAfterToolExecutor:
    def execute(
        self,
        proposal: AgentProposal,
        tool_call: ToolCall,
    ) -> ToolExecutionResult:
        del proposal, tool_call
        return ToolExecutionResult(
            data=None,
            continue_run=False,
            final_response="stopped",
        )


def _initial_messages() -> tuple[AgentMessage, ...]:
    return (AgentMessage(role=MessageRole.USER, content="synthetic"),)


def test_scripted_llm_returns_copies_and_rejects_exhaustion() -> None:
    first = _proposal(tool_name=None)
    llm = ScriptedLLM([first])

    returned = llm.complete(_initial_messages())

    assert returned == first
    assert returned is not first
    assert llm.calls == 1
    llm.assert_consumed()

    with pytest.raises(FreightEvalExecutionError, match="scripted_proposals_exhausted"):
        llm.complete(_initial_messages())


def test_scripted_llm_reports_unused_proposals() -> None:
    llm = ScriptedLLM([_proposal(tool_name=None), _proposal(tool_name=None)])

    llm.complete(_initial_messages())

    with pytest.raises(FreightEvalExecutionError, match="scripted_proposals_unused"):
        llm.assert_consumed()


def test_scripted_llm_validates_each_proposal_through_agent_schema() -> None:
    invalid = _proposal(tool_name=None).model_dump(mode="python")
    invalid["confidence"] = "not-a-number"

    with pytest.raises(ValidationError):
        _case(
            case_id="invalid-scripted-proposal",
            source={
                "kind": "body",
                "channel": "email",
                "subject": "Load",
                "body": "Need a truck",
            },
            proposals=[invalid],  # type: ignore[list-item]
        )


@pytest.mark.asyncio
async def test_runner_invokes_real_loop_and_policy_for_body_source(
    tenant_config: TenantConfig,
    document_fixtures: dict[str, ResolvedDocumentFixture],
) -> None:
    case = _case(
        case_id="body-runner-complete",
        source={
            "kind": "body",
            "channel": "email",
            "subject": "Load request",
            "body": "Need a dry van from Chicago to Detroit.",
        },
        proposals=[_complete_load_proposal(), _proposal(tool_name=None)],
    )

    result = await run_freight_eval_case(
        case,
        tenant_config=tenant_config,
        document_fixtures=document_fixtures,
    )

    assert result.passed, result.safe_failure_message()
    assert result.observation.intake_type == "load_request"
    assert result.observation.missing_required_fields == ()
    assert result.observation.policy_decision == "allow"
    assert result.observation.routing_status == "ready"
    assert result.observation.routing_reason == "action_allowed"
    assert result.observation.tool_name == "create_case"
    assert result.observation.approval_required is False
    assert result.observation.case_created is True
    assert result.observation.provider_calls == 0
    assert result.observation.llm_calls == 2


@pytest.mark.asyncio
async def test_runner_observer_uses_one_fixed_trace_without_changing_report(
    tenant_config: TenantConfig,
    document_fixtures: dict[str, ResolvedDocumentFixture],
) -> None:
    case = _case(
        case_id="body-runner-observability",
        source={
            "kind": "body",
            "channel": "email",
            "subject": "Load request",
            "body": "Need a dry van from Chicago to Detroit.",
        },
        proposals=[_complete_load_proposal(), _proposal(tool_name=None)],
    )
    observer = RecordingObserver()
    trace_id = UUID("00000000-0000-0000-0000-000000000077")

    result = await run_freight_eval_case(
        case,
        tenant_config=tenant_config,
        document_fixtures=document_fixtures,
        observer=observer,
        context=ObservationContext(trace_id=trace_id),
    )

    assert result.passed
    names = {event.event for event in observer.events}
    assert {
        EventName.AGENT_STEP_COMPLETED,
        EventName.AGENT_RUN_FINISHED,
        EventName.POLICY_EVALUATED,
        EventName.TOOL_EXECUTION_COMPLETED,
    } <= names
    assert all(event.trace_id == trace_id for event in observer.events)
    assert "trace_id" not in result.model_dump_json()
    assert "events" not in result.model_dump_json()
    assert "Need a dry van" not in result.model_dump_json()
@pytest.mark.asyncio
async def test_runner_document_source_uses_normalizer_and_manifest_fixture(
    tenant_config: TenantConfig,
    document_fixtures: dict[str, ResolvedDocumentFixture],
) -> None:
    case = _case(
        case_id="document-runner-complete",
        category="document",
        source={
            "kind": "document",
            "fixture_id": "complete-en",
            "subject": "Rate confirmation",
        },
        proposals=[_complete_document_proposal(), _proposal(tool_name=None)],
        expected={
            "intake_type": "rate_confirmation",
            "missing_required_fields": [],
            "policy_decision": "allow",
            "routing_status": "ready",
            "routing_reason": "action_allowed",
            "tool_name": "create_case",
            "approval_required": False,
            "case_created": True,
            "provider_calls": 0,
            "llm_calls": 2,
        },
    )

    result = await run_freight_eval_case(
        case,
        tenant_config=tenant_config,
        document_fixtures=document_fixtures,
    )

    assert result.passed, result.safe_failure_message()
    assert result.observation.intake_type == "rate_confirmation"
    assert result.observation.case_created is True


@pytest.mark.asyncio
async def test_runner_webhook_source_uses_inbound_mapping_without_network(
    tenant_config: TenantConfig,
    document_fixtures: dict[str, ResolvedDocumentFixture],
) -> None:
    case = _case(
        case_id="webhook-runner-complete",
        category="signed_webhook",
        source={
            "kind": "inbound_webhook",
            "provider_id": "provider-runner-1",
            "from_addr": "dispatcher@example.test",
            "subject": "Load request",
            "body": "Need a dry van from Chicago to Detroit.",
        },
        proposals=[_complete_load_proposal(), _proposal(tool_name=None)],
    )

    result = await run_freight_eval_case(
        case,
        tenant_config=tenant_config,
        document_fixtures=document_fixtures,
    )

    assert result.passed, result.safe_failure_message()
    assert result.observation.policy_decision == "allow"
    assert result.observation.case_created is True
    assert result.observation.llm_calls == 2
    assert "dispatcher@example.test" not in repr(result)


@pytest.mark.asyncio
async def test_runner_malformed_document_is_safe_and_has_no_side_effects(
    tenant_config: TenantConfig,
    document_fixtures: dict[str, ResolvedDocumentFixture],
) -> None:
    proposal = AgentProposal.model_validate(
        {
            "intake_type": "rate_confirmation",
            "fields": [],
            "missing_required_fields": [],
            "priority": ProposalPriority.NORMAL,
            "contains_injection_or_override_attempt": False,
            "rationale_short": "document_unreadable",
            "tool_calls": [{"name": "create_case", "arguments": []}],
            "confidence": 0.0,
        }
    )
    case = _case(
        case_id="document-runner-malformed",
        category="document",
        source={
            "kind": "document",
            "fixture_id": "malformed-pdf",
            "subject": "Rate confirmation",
        },
        proposal=proposal,
        expected={
            "intake_type": "rate_confirmation",
            "missing_required_fields": [
                "origin",
                "destination",
                "equipment",
                "pickup_window",
                "commodity",
                "contact",
                "quoted_rate",
                "valid_until",
            ],
            "policy_decision": "deny",
            "routing_status": "awaiting_input",
            "routing_reason": "document_unreadable",
            "tool_name": None,
            "approval_required": False,
            "case_created": False,
            "document_extraction_error": "pdf_malformed",
            "provider_calls": 0,
            "llm_calls": 0,
            "steps": 0,
            "stop_reason": None,
        },
    )

    result = await run_freight_eval_case(
        case,
        tenant_config=tenant_config,
        document_fixtures=document_fixtures,
    )

    assert result.passed, result.safe_failure_message()
    assert result.observation.routing_reason == "document_unreadable"
    assert result.observation.case_created is False
    assert result.observation.approval_required is False
    assert result.observation.document_extraction_error == "pdf_malformed"
    assert result.observation.provider_calls == 0
    assert "PdfReadError" not in repr(result)
    assert "SECRET" not in repr(result)
    assert "Origin: Chicago" not in repr(result)


def test_recording_policy_delegates_to_policy_engine(
    tenant_config: TenantConfig,
) -> None:
    source = TrustedSource(
        channel="email",
        subject="Load",
        body="Need a truck",
    )
    runtime = TrustedToolRuntimeContext(
        tenant_id=TENANT_ID,
        tenant_config=tenant_config,
        source=source,
        risk_signals=RiskSignals(),
    )
    proposal = _complete_load_proposal()
    value = PolicyInput(
        proposal=proposal,
        requested_action="create_case",
        requires_complete_fields=True,
        registered_actions=frozenset({"create_case"}),
        runtime=runtime,
    )
    recorder = RecordingPolicy()

    result = recorder.evaluate(value)

    assert result == PolicyEngine().evaluate(value)
    assert recorder.outcomes == [result]


def test_loop_preserves_none_tool_results_and_stops_at_final_proposal() -> None:
    llm = ScriptedLLM(
        [_proposal(tool_name="find_customer"), _proposal(tool_name=None)]
    )
    result = AgentLoop(llm, _NoneExecutor()).run(_initial_messages())

    assert result.reason is StopReason.FINAL
    tool_messages = [
        message for message in result.messages if message.role is MessageRole.TOOL
    ]
    assert len(tool_messages) == 1
    assert tool_messages[0].tool_result is None


def test_loop_preserves_third_identical_tool_guard() -> None:
    proposal = _proposal(tool_name="find_customer")
    llm = ScriptedLLM([proposal, proposal, proposal, _proposal(tool_name=None)])

    result = AgentLoop(llm, _NoneExecutor()).run(_initial_messages())

    assert result.reason is StopReason.REPEATED_TOOL
    assert result.steps == 3
    assert llm.calls == 3


def test_loop_preserves_eight_step_cap() -> None:
    proposals = [
        _proposal(tool_name=name)
        for name in ("find_customer", "create_case", "flag_for_review") * 3
    ][:8]
    llm = ScriptedLLM(proposals)

    result = AgentLoop(llm, _StopAfterToolExecutor()).run(_initial_messages())

    assert result.reason is StopReason.EXECUTOR_STOPPED

    llm = ScriptedLLM(proposals)
    result = AgentLoop(llm, _NoneExecutor()).run(_initial_messages())
    assert result.reason is StopReason.MAX_STEPS
    assert result.steps == 8
    assert llm.calls == 8


@pytest.mark.asyncio
async def test_runner_reports_exhausted_scripted_proposals(
    tenant_config: TenantConfig,
    document_fixtures: dict[str, ResolvedDocumentFixture],
) -> None:
    case = _case(
        case_id="runner-script-exhausted",
        source={
            "kind": "body",
            "channel": "email",
            "subject": "Load",
            "body": "Need a truck",
        },
        proposal=_complete_load_proposal(),
    )

    result = await run_freight_eval_case(
        case,
        tenant_config=tenant_config,
        document_fixtures=document_fixtures,
    )
    assert result.passed is False
    assert "scripted_proposals_exhausted" in result.mismatches


@pytest.mark.asyncio
async def test_runner_reports_unused_scripted_proposals(
    tenant_config: TenantConfig,
    document_fixtures: dict[str, ResolvedDocumentFixture],
) -> None:
    case = _case(
        case_id="runner-script-unused",
        source={
            "kind": "body",
            "channel": "email",
            "subject": "Load",
            "body": "Need a truck",
        },
        proposals=[_proposal(tool_name=None), _proposal(tool_name=None)],
    )

    result = await run_freight_eval_case(
        case,
        tenant_config=tenant_config,
        document_fixtures=document_fixtures,
    )
    assert result.passed is False
    assert "scripted_proposals_unused" in result.mismatches


@pytest.mark.asyncio
async def test_runner_reuses_duplicate_webhook_delivery(
    tenant_config: TenantConfig,
    document_fixtures: dict[str, ResolvedDocumentFixture],
) -> None:
    case = _case(
        case_id="idempotency-duplicate",
        category="tenant_idempotency",
        kind="webhook_idempotency",
        source={
            "kind": "inbound_webhook",
            "provider_id": "provider-duplicate",
            "from_addr": "dispatcher@example.test",
            "subject": "Load",
            "body": "Need a truck",
            "second_body": "Need a truck",
        },
        expected={"duplicate_reused": True, "conflict_raised": False},
    )

    result = await run_freight_eval_case(
        case,
        tenant_config=tenant_config,
        document_fixtures=document_fixtures,
    )

    assert result.passed, result.safe_failure_message()
    assert result.observation.duplicate_reused is True
    assert result.observation.conflict_raised is False
    assert "dispatcher@example.test" not in repr(result)
    assert "provider-duplicate" not in repr(result)


@pytest.mark.asyncio
async def test_runner_reports_changed_webhook_payload_conflict(
    tenant_config: TenantConfig,
    document_fixtures: dict[str, ResolvedDocumentFixture],
) -> None:
    case = _case(
        case_id="idempotency-conflict",
        category="tenant_idempotency",
        kind="webhook_idempotency",
        source={
            "kind": "inbound_webhook",
            "provider_id": "provider-conflict",
            "from_addr": "dispatcher@example.test",
            "subject": "Load",
            "body": "Need a truck",
            "second_body": "Changed body",
        },
        expected={"duplicate_reused": False, "conflict_raised": True},
    )

    result = await run_freight_eval_case(
        case,
        tenant_config=tenant_config,
        document_fixtures=document_fixtures,
    )

    assert result.passed, result.safe_failure_message()
    assert result.observation.duplicate_reused is False
    assert result.observation.conflict_raised is True
    assert "Changed body" not in repr(result)


@pytest.mark.asyncio
async def test_runner_keeps_same_provider_id_independent_across_tenants(
    tenant_config: TenantConfig,
    document_fixtures: dict[str, ResolvedDocumentFixture],
) -> None:
    case = _case(
        case_id="idempotency-other-tenant",
        category="tenant_idempotency",
        kind="webhook_idempotency",
        source={
            "kind": "inbound_webhook",
            "provider_id": "provider-shared",
            "from_addr": "dispatcher@example.test",
            "subject": "Load",
            "body": "Need a truck",
            "second_tenant": True,
        },
        expected={"duplicate_reused": False, "conflict_raised": False},
    )

    result = await run_freight_eval_case(
        case,
        tenant_config=tenant_config,
        document_fixtures=document_fixtures,
    )

    assert result.passed, result.safe_failure_message()
    assert result.observation.duplicate_reused is False
    assert result.observation.conflict_raised is False


@pytest.mark.asyncio
async def test_runner_keeps_tool_port_tenant_isolation_as_hard_invariant(
    tenant_config: TenantConfig,
    document_fixtures: dict[str, ResolvedDocumentFixture],
) -> None:
    case = _case(
        case_id="tenant-isolation",
        category="tenant_idempotency",
        kind="tenant_isolation",
        source={
            "kind": "inbound_webhook",
            "provider_id": "provider-tenant-isolation",
            "from_addr": "tenant-a@example.test",
            "subject": "Load",
            "body": "Tenant A must not see Tenant B",
            "second_tenant": True,
        },
        expected={"tenant_isolated": True},
    )

    result = await run_freight_eval_case(
        case,
        tenant_config=tenant_config,
        document_fixtures=document_fixtures,
    )

    assert result.passed, result.safe_failure_message()
    safe_observation = result.observation.model_dump(exclude_none=True)
    assert set(safe_observation) <= {
        "duplicate_reused",
        "conflict_raised",
        "tenant_isolated",
        "signature_verified",
        "provider_calls",
        "llm_calls",
    }
    assert safe_observation["tenant_isolated"] is True


def test_in_memory_tool_port_rejects_cross_tenant_customer_and_case_access() -> None:
    port = InMemoryTenantToolPort()
    tenant_a = UUID("00000000-0000-0000-0000-000000000021")
    tenant_b = UUID("00000000-0000-0000-0000-000000000022")
    customer_b = port.add_customer(
        tenant_b,
        external_id="shared-customer",
        email="tenant-b@example.test",
    )
    case_b = port.seed_case(tenant_b, fields={"origin": "Tenant B"})

    assert (
        port.find_customer(
            tenant_a,
            email="tenant-b@example.test",
            external_id="shared-customer",
        )
        is None
    )
    before = dict(port.cases[case_b].extracted_fields)
    assert port.update_case_fields(
        tenant_a,
        case_b,
        fields={"origin": "attacker"},
    ) is None
    assert port.cases[case_b].extracted_fields == before
    assert customer_b.id not in repr(port.find_customer(tenant_a, email=None, external_id=None))
