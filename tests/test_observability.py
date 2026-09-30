from __future__ import annotations

import json
import logging
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from app.agent import StopReason
from app.documents import DocumentExtractionError, DocumentMediaType
from app.observability.context import ObservationContext
from app.observability.events import (
    CapabilityErrorCode,
    Component,
    EventLevel,
    EventName,
    ObservationEvent,
    OutcomeCode,
    PolicyReason,
    PreflightOutcome,
    RouteName,
)
from app.observability.logging import configure_json_logging
from app.observability.observer import StructlogObserver, safe_emit
from app.tenants.config import RoutingDecision, RoutingStatus


TRACE_ID = UUID("11111111-1111-1111-1111-111111111111")
REQUEST_ID = UUID("22222222-2222-2222-2222-222222222222")


def _event(**changes: object) -> ObservationEvent:
    payload: dict[str, object] = {
        "event": EventName.AGENT_STEP_COMPLETED,
        "level": EventLevel.INFO,
        "trace_id": TRACE_ID,
        "request_id": REQUEST_ID,
        "component": Component.AGENT,
        "outcome": OutcomeCode.TOOL_REQUESTED,
        "step": 1,
        "stop_reason": StopReason.FINAL,
        "action_key": "create_case",
        "action_known": True,
        "policy_decision": RoutingDecision.ALLOW,
        "routing_status": RoutingStatus.READY,
        "policy_reason": PolicyReason.ACTION_ALLOWED,
        "document_media_type": DocumentMediaType.PDF,
        "document_error": DocumentExtractionError.PDF_MALFORMED,
        "missing_fields": ("destination", "origin"),
        "duration_ms": 12,
        "result_is_none": True,
    }
    payload.update(changes)
    return ObservationEvent.model_validate(payload)


def test_observation_event_is_frozen_closed_and_json_safe() -> None:
    event = _event()

    assert event.event_version == 1
    assert event.missing_fields == ("destination", "origin")
    assert json.loads(event.model_dump_json())["trace_id"] == str(TRACE_ID)
    with pytest.raises(ValidationError):
        event.outcome = OutcomeCode.FAILED  # type: ignore[misc]


def test_event_rejects_unknown_fields_negative_counts_and_free_text_codes() -> None:
    with pytest.raises(ValidationError):
        _event(source_text="SOURCE_BODY_SECRET")
    with pytest.raises(ValidationError):
        _event(duration_ms=-1)
    with pytest.raises(ValidationError):
        _event(outcome="arbitrary_free_text")
    with pytest.raises(ValidationError):
        ObservationEvent(
            event=EventName.AGENT_STEP_COMPLETED,
            trace_id=TRACE_ID,
            component=Component.AGENT,
            missing_fields=("Origin",),
        )


def test_context_bind_returns_a_copy_without_mutating_server_trace() -> None:
    context = ObservationContext(trace_id=TRACE_ID, request_id=REQUEST_ID)

    bound = context.bind(job_id=UUID("33333333-3333-3333-3333-333333333333"))

    assert bound is not context
    assert bound.trace_id == TRACE_ID
    assert bound.job_id is not None
    assert context.job_id is None


def test_context_and_profile_event_bind_safe_scenario_and_fingerprint() -> None:
    context = ObservationContext(trace_id=TRACE_ID, request_id=REQUEST_ID).bind(
        scenario_key="repair_service",
        profile_fingerprint="a" * 64,
    )

    event = _event(
        event=EventName.PROFILE_RESOLUTION_COMPLETED,
        component=Component.PROFILE,
        scenario_key=context.scenario_key,
        profile_fingerprint=context.profile_fingerprint,
    )

    assert context.scenario_key == "repair_service"
    assert context.profile_fingerprint == "a" * 64
    assert event.scenario_key == "repair_service"
    assert event.profile_fingerprint == "a" * 64


def test_preflight_event_uses_closed_enums_and_optional_capability_fields() -> None:
    event = _event(
        event=EventName.RUNTIME_PREFLIGHT_COMPLETED,
        component=Component.RUNTIME,
        preflight_outcome=PreflightOutcome.TERMINAL,
        capability_error=CapabilityErrorCode.SNAPSHOT_INCOMPATIBLE,
        document_kind="repair_request",
        normalizer_key="bounded_text_pdf",
        normalizer_version=1,
        intake_type="repair_request",
        intake_type_known=True,
        action_key="create_case",
        action_known=True,
    )

    assert PreflightOutcome.CONTINUE.value == "continue"
    assert PreflightOutcome.TERMINAL.value == "terminal"
    assert CapabilityErrorCode.UNAVAILABLE.value == "capability_unavailable"
    assert (
        CapabilityErrorCode.SNAPSHOT_INCOMPATIBLE.value
        == "snapshot_incompatible"
    )
    assert event.preflight_outcome is PreflightOutcome.TERMINAL
    assert event.capability_error is CapabilityErrorCode.SNAPSHOT_INCOMPATIBLE
    assert event.normalizer_version == 1

    minimal = _event(
        event=EventName.RUNTIME_PREFLIGHT_COMPLETED,
        component=Component.RUNTIME,
        preflight_outcome=PreflightOutcome.CONTINUE,
    )
    assert minimal.document_kind is None
    assert minimal.normalizer_key is None
    assert minimal.normalizer_version is None


@pytest.mark.parametrize(
    ("dimension", "known_flag", "unknown_name"),
    (
        ("intake_type", "intake_type_known", "model_invented_intake"),
        ("action_key", "action_known", "model_invented_action"),
    ),
)
def test_unknown_model_dimensions_are_closed_without_raw_names(
    dimension: str,
    known_flag: str,
    unknown_name: str,
) -> None:
    event = _event(**{dimension: None, known_flag: False})

    assert getattr(event, dimension) is None
    assert getattr(event, known_flag) is False
    assert unknown_name not in event.model_dump_json()

    with pytest.raises(ValidationError):
        _event(**{dimension: unknown_name, known_flag: False})


def test_safe_emit_swallows_sink_failure_without_logging_exception_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class FailingObserver:
        def emit(self, event: ObservationEvent) -> None:
            del event
            raise RuntimeError("DATABASE_URL_SECRET and SOURCE_BODY_SECRET")

    with caplog.at_level(logging.ERROR, logger="app.observability"):
        safe_emit(FailingObserver(), _event())

    assert "observability_emit_failed" in caplog.text
    assert "DATABASE_URL_SECRET" not in caplog.text
    assert "SOURCE_BODY_SECRET" not in caplog.text


def test_structlog_observer_outputs_one_json_object_with_only_model_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_json_logging()

    StructlogObserver().emit(_event(event=EventName.POLICY_EVALUATED))

    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    assert len(lines) == 1
    rendered = json.loads(lines[0])
    assert rendered["event"] == EventName.POLICY_EVALUATED.value
    assert rendered["level"] == EventLevel.INFO.value
    assert rendered["trace_id"] == str(TRACE_ID)
    assert "source_text" not in rendered
    assert "metadata" not in rendered


def test_serialized_event_excludes_secret_source_and_transcript_sentinels() -> None:
    event = _event()
    serialized = event.model_dump_json()
    rendered = repr(event)

    for sentinel in (
        "ik_SECRET",
        "Bearer SECRET",
        "SOURCE_BODY_SECRET",
        "DOCUMENT_TEXT_SECRET",
        "RATIONALE_SECRET",
        "TOOL_RESULT_SECRET",
    ):
        assert sentinel not in serialized
        assert sentinel not in rendered


def test_json_logging_configuration_is_idempotent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_json_logging()
    configure_json_logging()

    StructlogObserver().emit(
        _event(event=EventName.HTTP_REQUEST_COMPLETED, component=Component.API)
    )

    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    assert len(lines) == 1


def test_product_docs_describe_local_observability_without_training_context() -> None:
    root = Path(__file__).resolve().parents[1]
    for name in ("README.md", "AGENTS.md"):
        text = (root / name).read_text(encoding="utf-8").lower()
        for phrase in ("trace", "structured", "json", "source-free", "external"):
            assert phrase in text, (name, phrase)
        for forbidden in (
            "phase 12",
            "roadmap phase",
            "interview",
            "vacancy",
            "learning project",
            "tutorial",
            "training context",
        ):
            assert forbidden not in text, (name, forbidden)
