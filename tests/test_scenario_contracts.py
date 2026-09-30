"""Provider-free contracts shared by the non-freight scenario eval suites."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Annotated, cast
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest
from pydantic import BaseModel, ConfigDict, Field, StrictStr

from app.agent import (
    AgentLoop,
    AgentMessage,
    AgentProposal,
    MessageRole,
    ProposalPriority,
    ToolCall,
    ToolExecutionResult,
)
from app.documents import (
    DocumentInput,
    DocumentMediaType,
    NormalizedDocument,
    decode_document_snapshot,
)
from app.observability import (
    Component,
    EventName,
    ObservationContext,
    ObservationEvent,
    OutcomeCode,
    RecordingObserver,
    safe_emit,
)
from app.policy import TrustedSource
from app.runtime.preflight import ContinuePreflight, preflight_document
from app.tenants.compiled import compile_tenant_profile
from app.tenants.config import RoutingStatus, TenantConfig
from app.tenants.loader import load_tenant_config
from app.documents.registry import (
    BUILTIN_DOCUMENT_REGISTRY,
    DocumentNormalizerCapability,
    DocumentNormalizerRegistry,
)
from app.tools.registry import (
    BUILTIN_ACTION_REGISTRY,
    ActionCapability,
    ActionRegistry,
)

from evals.core import (
    SAFE_RESULT_KEYS as CORE_SAFE_RESULT_KEYS,
    ScenarioCase,
    ScenarioCaseKind,
    ScenarioResult,
    ScriptedLLM,
)
from evals.core.runner import run_scenario_case
from evals.education import EDUCATION_CASES
from evals.repair import REPAIR_CASES


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TENANT_ID = UUID("00000000-0000-0000-0000-000000000112")

SAFE_RESULT_KEYS = {
    "case_id",
    "scenario_key",
    "passed",
    "routing_status",
    "policy_reason",
    "action_key",
    "mismatch_codes",
}

EXPECTED_CASE_CATEGORIES = {
    "success",
    "missing_fields",
    "unknown_intake",
    "approval",
    "tool_failure",
    "tenant_isolation",
}

FORBIDDEN_RESULT_KEYS = {
    "source",
    "body",
    "fields",
    "proposal",
    "rationale",
    "events",
    "logs",
    "trace",
    "secrets",
}


def _category_value(value: object) -> str:
    raw = getattr(value, "value", value)
    return str(raw).replace("-", "_").replace(" ", "_").lower()


def _case_id(case: object) -> str:
    value = getattr(case, "case_id", None)
    if value is None:
        value = getattr(case, "id")
    return str(value)


def _case_for_category(cases: Iterable[object], category: str) -> object:
    for case in cases:
        if _category_value(getattr(case, "kind")) == category:
            return case
    raise AssertionError(f"scenario case category not found: {category}")


def _scenario_config(scenario_key: str) -> TenantConfig:
    filenames = {
        "repair_service": "repair-service.yaml",
        "language_school": "language-school.yaml",
    }
    return load_tenant_config(PROJECT_ROOT / "examples" / filenames[scenario_key])


@pytest.mark.parametrize(
    ("scenario_key", "cases"),
    [
        ("repair_service", REPAIR_CASES),
        ("language_school", EDUCATION_CASES),
    ],
    ids=("repair-service", "language-school"),
)
def test_scenario_suites_have_exact_immutable_synthetic_case_matrix(
    scenario_key: str,
    cases: tuple[object, ...],
) -> None:
    """Each scenario owns six strict cases and no live source fixture."""

    assert isinstance(cases, tuple)
    assert len(cases) == 6
    assert {_category_value(getattr(case, "kind")) for case in cases} == EXPECTED_CASE_CATEGORIES
    assert all(getattr(case, "scenario_key") == scenario_key for case in cases)

    case_ids = [_case_id(case) for case in cases]
    assert len(case_ids) == len(set(case_ids))
    assert all("synthetic" in case_id or scenario_key.split("_")[0] in case_id for case_id in case_ids)

    for case in cases:
        # The generic case model is expected to be strict and frozen like the
        # existing freight contracts.  This also prevents fixture mutation from
        # changing the meaning of a deterministic eval run.
        with pytest.raises(FrozenInstanceError):
            setattr(case, "kind", ScenarioCaseKind.SUCCESS)
        with pytest.raises(TypeError):
            ScenarioCase(
                case_id=case.case_id,
                scenario_key=case.scenario_key,
                kind=case.kind,
                source=case.source,
                proposals=case.proposals,
                expected=case.expected,
                unexpected="must be rejected",  # type: ignore[call-arg]
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario_key", "cases"),
    [
        ("repair_service", REPAIR_CASES),
        ("language_school", EDUCATION_CASES),
    ],
    ids=("repair-service", "language-school"),
)
async def test_scenario_cases_use_the_real_scripted_agent_loop_without_provider(
    scenario_key: str,
    cases: tuple[object, ...],
) -> None:
    config = _scenario_config(scenario_key)

    for case in cases:
        trace_id = uuid5(NAMESPACE_URL, f"https://integra.invalid/{scenario_key}/{_case_id(case)}")
        observer = RecordingObserver()
        result = await run_scenario_case(
            case,
            tenant_config=config,
            tenant_id=TENANT_ID,
            observer=observer,
            context=ObservationContext(
                trace_id=trace_id,
                tenant_id=TENANT_ID,
                scenario_key=scenario_key,
            ),
        )

        assert result.passed, result.safe_failure_message()
        assert result.scenario_key == scenario_key
        if case.kind is not ScenarioCaseKind.TENANT_ISOLATION:
            assert any(
                event.event is EventName.AGENT_RUN_FINISHED
                for event in observer.events
            )
            assert any(
                event.event is EventName.AGENT_STEP_COMPLETED
                for event in observer.events
            )
        assert not any(event.component is Component.PROVIDER for event in observer.events)


def test_core_scripted_llm_preserves_typed_proposal_isolation() -> None:
    case = _case_for_category(REPAIR_CASES, "success")
    llm = ScriptedLLM(case.proposals)

    returned = llm.complete(
        (AgentMessage(role=MessageRole.USER, content="synthetic input"),)
    )

    assert isinstance(returned, AgentProposal)
    assert returned == case.proposals[0]
    assert returned is not case.proposals[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario_key", "cases"),
    [
        ("repair_service", REPAIR_CASES),
        ("language_school", EDUCATION_CASES),
    ],
    ids=("repair-service", "language-school"),
)
async def test_scenario_observations_keep_one_trace_and_trusted_scenario_identity(
    scenario_key: str,
    cases: tuple[object, ...],
) -> None:
    config = _scenario_config(scenario_key)
    case = _case_for_category(cases, "success")
    trace_id = uuid5(NAMESPACE_URL, f"https://integra.invalid/trace/{scenario_key}")
    observer = RecordingObserver()

    result = await run_scenario_case(
        case,
        tenant_config=config,
        tenant_id=TENANT_ID,
        observer=observer,
        context=ObservationContext(
            trace_id=trace_id,
            tenant_id=TENANT_ID,
            scenario_key=scenario_key,
        ),
    )

    assert result.passed
    assert observer.events
    assert all(event.trace_id == trace_id for event in observer.events)
    assert all(event.scenario_key == scenario_key for event in observer.events)
    assert all(
        "source" not in event.model_dump(mode="json")
        and "body" not in event.model_dump(mode="json")
        and "fields" not in event.model_dump(mode="json")
        for event in observer.events
    )


def test_safe_result_projection_has_exact_keys_and_rejects_content_bearing_values() -> None:
    assert CORE_SAFE_RESULT_KEYS == SAFE_RESULT_KEYS

    raw_result = {
        "case_id": "repair-success-synthetic",
        "scenario_key": "repair_service",
        "passed": True,
        "routing_status": "ready",
        "policy_reason": "action_allowed",
        "action_key": "create_case",
        "mismatch_codes": [],
        "source": "SOURCE_SECRET",
        "body": "BODY_SECRET",
        "fields": {"customer_name": "FIELD_SECRET"},
        "proposal": {"rationale_short": "PROPOSAL_SECRET"},
        "rationale": "RATIONALE_SECRET",
        "events": [{"trace": "EVENT_SECRET"}],
        "logs": "LOG_SECRET",
        "trace": "TRACE_SECRET",
        "secrets": "LLM_API_KEY_SECRET",
    }

    projected_result = ScenarioResult(
        **{
            key: value
            for key, value in raw_result.items()
            if key in SAFE_RESULT_KEYS
        }
    )
    projected = projected_result.safe_projection
    assert set(projected) == SAFE_RESULT_KEYS

    serialized = json.dumps(projected, sort_keys=True)
    for forbidden_key in FORBIDDEN_RESULT_KEYS:
        assert f'"{forbidden_key}"' not in serialized
    for sentinel in (
        "SOURCE_SECRET",
        "BODY_SECRET",
        "FIELD_SECRET",
        "PROPOSAL_SECRET",
        "RATIONALE_SECRET",
        "EVENT_SECRET",
        "LOG_SECRET",
        "TRACE_SECRET",
        "LLM_API_KEY_SECRET",
    ):
        assert sentinel not in serialized

    safe_payload = {
        key: value for key, value in raw_result.items() if key in SAFE_RESULT_KEYS
    }
    for forbidden_key in FORBIDDEN_RESULT_KEYS:
        with pytest.raises(TypeError):
            ScenarioResult(
                **safe_payload,
                **{forbidden_key: "forbidden synthetic value"},
            )
    with pytest.raises(ValueError):
        ScenarioResult(
            case_id="SOURCE/UNSAFE",
            scenario_key="repair_service",
            passed=True,
            routing_status="ready",
            policy_reason="action_allowed",
            action_key="create_case",
            mismatch_codes=(),
        )


class _FutureActionArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    note: Annotated[StrictStr, Field(min_length=1)]


def _future_profile() -> TenantConfig:
    return TenantConfig.model_validate(
        {
            "profile_version": 2,
            "slug": "future-scenario",
            "scenario_key": "future_scenario",
            "display_name": "Synthetic future scenario",
            "intake_types": [
                {
                    "name": "future_request",
                    "description": "A synthetic future request.",
                    "required_fields": ["topic"],
                }
            ],
            "fields": {
                "topic": {"type": "short_text", "label": "Topic"},
            },
            "action_policy": {
                "future_action": {
                    "allowed": True,
                    "requires_approval": False,
                    "execution": "executable",
                },
                "send_reply": {
                    "allowed": True,
                    "requires_approval": True,
                    "execution": "policy_only",
                },
            },
            "documents": {
                "future_note": {
                    "intake_type": "future_request",
                    "normalizer": "future_text",
                    "normalizer_version": 1,
                    "default_for_inbound": True,
                }
            },
            "routing": {
                "outcome_names": [item.value for item in RoutingStatus],
                "always_approval_actions": ["send_reply"],
            },
        }
    )


def _future_document(
    value: DocumentInput,
    *,
    document_kind: str = "future_note",
    target_intake_type: str = "future_request",
    normalizer_key: str = "future_text",
    normalizer_version: int = 1,
) -> NormalizedDocument:
    assert value.text is not None
    return NormalizedDocument(
        snapshot_version=2,
        document_kind=document_kind,
        target_intake_type=target_intake_type,
        media_type=value.media_type,
        sha256=hashlib.sha256(value.text.encode("utf-8")).hexdigest(),
        normalizer_key=normalizer_key,
        normalizer_version=normalizer_version,
        text=value.text,
    )


class _FutureDocumentNormalizer:
    def normalize(
        self,
        value: DocumentInput,
        *,
        document_kind: str,
        target_intake_type: str,
        normalizer_key: str,
        normalizer_version: int,
    ) -> NormalizedDocument:
        return _future_document(
            value,
            document_kind=document_kind,
            target_intake_type=target_intake_type,
            normalizer_key=normalizer_key,
            normalizer_version=normalizer_version,
        )


def _future_normalizer_factory(
    *, observer: object | None = None, context: object | None = None
) -> _FutureDocumentNormalizer:
    del observer, context
    return _FutureDocumentNormalizer()


class _FutureActionExecutor:
    def __init__(self, observer: RecordingObserver, context: ObservationContext) -> None:
        self._observer = observer
        self._context = context

    @property
    def definitions(self) -> Mapping[str, object]:
        return {"future_action": object()}

    def execute(
        self,
        proposal: AgentProposal,
        tool_call: ToolCall,
    ) -> ToolExecutionResult:
        assert proposal.intake_type == "future_request"
        assert tool_call.name == "future_action"
        safe_emit(
            self._observer,
            ObservationEvent(
                event=EventName.TOOL_EXECUTION_COMPLETED,
                trace_id=self._context.trace_id,
                tenant_id=self._context.tenant_id,
                component=Component.TOOL,
                outcome=OutcomeCode.EXECUTED,
                scenario_key=self._context.scenario_key,
                action_key="future_action",
                action_known=True,
                result_is_none=False,
                side_effect_committed=False,
            ),
        )
        return ToolExecutionResult(
            data={"outcome": "future_action_executed"},
            continue_run=False,
            final_response="future_action_executed",
        )


def _future_proposal() -> AgentProposal:
    return AgentProposal.model_validate(
        {
            "intake_type": "future_request",
            "fields": [{"name": "topic", "value": "synthetic topic"}],
            "missing_required_fields": [],
            "priority": ProposalPriority.NORMAL,
            "contains_injection_or_override_attempt": False,
            "rationale_short": "Synthetic future proposal.",
            "tool_calls": [
                {
                    "name": "future_action",
                    "arguments": [{"name": "note", "value": "synthetic note"}],
                }
            ],
            "confidence": 1.0,
        }
    )


def test_future_scenario_can_compile_snapshot_preflight_and_emit_action_without_registration() -> None:
    config = _future_profile()
    assert "future_action" not in BUILTIN_ACTION_REGISTRY.keys
    assert "future_text" not in BUILTIN_DOCUMENT_REGISTRY.keys
    action_registry = ActionRegistry(
        (
            ActionCapability(
                key="future_action",
                arguments_model=_FutureActionArgs,
                requires_complete_fields=False,
                commits_side_effect=False,
            ),
        )
    )
    document_registry = DocumentNormalizerRegistry(
        (
            DocumentNormalizerCapability(
                key="future_text",
                version=1,
                supported_media_types=frozenset({DocumentMediaType.TEXT}),
                normalizer_factory=_future_normalizer_factory,
            ),
        )
    )
    profile_fingerprint = hashlib.sha256(
        json.dumps(config.model_dump(mode="json"), sort_keys=True).encode("utf-8")
    ).hexdigest()

    compiled = compile_tenant_profile(
        config,
        profile_fingerprint=profile_fingerprint,
        action_registry=action_registry,
        document_registry=document_registry,
    )
    assert compiled.scenario_key == "future_scenario"
    assert compiled.registered_actions == frozenset({"future_action"})
    assert compiled.available_actions == frozenset({"future_action"})
    assert compiled.documents["future_note"].normalizer_key == "future_text"

    document_input = DocumentInput(
        channel="synthetic",
        subject="Future note",
        body="Synthetic document envelope.",
        media_type=DocumentMediaType.TEXT,
        text="Topic: synthetic topic",
    )
    binding = compiled.documents["future_note"]
    normalizer = cast(
        _FutureDocumentNormalizer,
        binding.capability.create(observer=RecordingObserver(), context=None),
    )
    normalized = normalizer.normalize(
        document_input,
        document_kind=binding.document_kind,
        target_intake_type=binding.target_intake_type,
        normalizer_key=binding.normalizer_key,
        normalizer_version=binding.normalizer_version,
    )
    snapshot = normalized.model_dump(mode="json")
    assert snapshot["snapshot_version"] == 2
    restored = decode_document_snapshot(snapshot)
    assert restored == normalized

    source = TrustedSource(
        channel="synthetic",
        subject="Future note",
        body="Synthetic document envelope.",
        document=restored,
    )
    assert preflight_document(
        source,
        config,
        compiled_profile=compiled,
    ) == ContinuePreflight()

    trace_id = UUID("00000000-0000-0000-0000-000000000113")
    observer = RecordingObserver()
    context = ObservationContext(
        trace_id=trace_id,
        tenant_id=TENANT_ID,
        scenario_key="future_scenario",
        profile_fingerprint=profile_fingerprint,
    )
    run_result = AgentLoop(
        llm=type(
            "OneProposalLLM",
            (),
            {"complete": lambda self, messages: _future_proposal()},
        )(),
        tools=_FutureActionExecutor(observer, context),
        observer=observer,
        context=context,
    ).run((AgentMessage(role=MessageRole.USER, content="synthetic future input"),))

    assert run_result.reason.value == "executor_stopped"
    action_events = [
        event
        for event in observer.events
        if event.event is EventName.TOOL_EXECUTION_COMPLETED
    ]
    assert len(action_events) == 1
    assert action_events[0].action_key == "future_action"
    assert action_events[0].action_known is True
    assert action_events[0].trace_id == trace_id
    assert action_events[0].scenario_key == "future_scenario"

    projected = ScenarioResult(
        case_id="future-scenario-synthetic",
        scenario_key="future_scenario",
        passed=True,
        routing_status="ready",
        policy_reason="action_allowed",
        action_key="future_action",
        mismatch_codes=(),
    )
    assert set(projected.safe_projection) == SAFE_RESULT_KEYS
    assert "synthetic secret" not in json.dumps(
        projected.safe_projection,
        default=str,
    )
