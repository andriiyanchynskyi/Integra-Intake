from datetime import datetime, timezone
from uuid import UUID

import pytest

from app.agent import AgentProposal, FieldSource, ProposalPriority
from app.documents import DocumentInput, DocumentMediaType, DocumentNormalizer
from app.policy import RiskSignals, TrustedSource, TrustedToolRuntimeContext
from app.tenants.config import TenantConfig
from app.tenants.field_values import validate_proposal_fields


TENANT_ID = UUID("00000000-0000-0000-0000-000000000001")


@pytest.fixture
def typed_profile() -> TenantConfig:
    return TenantConfig.model_validate(
        {
            "profile_version": 2,
            "slug": "typed-fields",
            "scenario_key": "typed_fields",
            "display_name": "Typed fields",
            "intake_types": [
                {
                    "name": "request",
                    "description": "A typed request",
                    "required_fields": ["short_text"],
                }
            ],
            "fields": {
                "short_text": {"type": "short_text", "label": "Short text"},
                "long_text": {"type": "long_text", "label": "Long text"},
                "email": {"type": "email", "label": "Email"},
                "phone": {"type": "phone", "label": "Phone"},
                "address": {"type": "address", "label": "Address"},
                "date_time": {"type": "date_time", "label": "Date/time"},
                "number": {"type": "number", "label": "Number"},
                "boolean": {"type": "boolean", "label": "Boolean"},
                "single": {
                    "type": "single_select",
                    "label": "Single",
                    "options": ["one", "two"],
                },
                "multi": {
                    "type": "multi_select",
                    "label": "Multi",
                    "options": ["one", "two"],
                },
            },
            "action_policy": {
                "create_case": {
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


def _runtime(
    profile: TenantConfig,
    *,
    document_text: str | None = None,
    body: str = "Body excerpt",
) -> TrustedToolRuntimeContext:
    document = None
    if document_text is not None:
        document = DocumentNormalizer().normalize(
            DocumentInput.model_validate(
                {
                    "channel": "email",
                    "subject": "Request",
                    "body": body,
                    "media_type": DocumentMediaType.TEXT,
                    "text": document_text,
                }
            ),
            document_kind="request_document",
            target_intake_type="request",
            normalizer_key="bounded_text_pdf",
            normalizer_version=1,
        )
    return TrustedToolRuntimeContext(
        tenant_id=TENANT_ID,
        tenant_config=profile,
        source=TrustedSource(
            channel="email",
            subject="Request",
            body=body,
            document=document,
        ),
        risk_signals=RiskSignals(),
    )


def _proposal(fields: list[dict[str, object]]) -> AgentProposal:
    return AgentProposal.model_validate(
        {
            "intake_type": "request",
            "fields": fields,
            "missing_required_fields": [],
            "priority": ProposalPriority.NORMAL,
            "contains_injection_or_override_attempt": False,
            "rationale_short": "Typed proposal",
            "tool_calls": [],
            "confidence": 0.9,
        }
    )


def test_validate_profile_field_types_and_select_options(
    typed_profile: TenantConfig,
) -> None:
    proposal = _proposal(
        [
            {"name": "short_text", "value": "Summary"},
            {"name": "long_text", "value": "Details"},
            {"name": "email", "value": "person@example.com"},
            {"name": "phone", "value": "+380501234567"},
            {"name": "address", "value": "1 Main Street"},
            {"name": "date_time", "value": "2026-10-14T10:00:00+00:00"},
            {"name": "number", "value": 12.5},
            {"name": "boolean", "value": True},
            {"name": "single", "value": "one"},
            {"name": "multi", "value": ["one", "two"]},
        ]
    )

    result = validate_proposal_fields(proposal, _runtime(typed_profile))

    assert result.invalid_fields == ()
    assert result.values["email"] == "person@example.com"
    assert result.values["multi"] == ["one", "two"]


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("short_text", 123),
        ("long_text", True),
        ("email", "not-an-email"),
        ("date_time", "2026-10-14"),
        ("number", True),
        ("boolean", "true"),
        ("single", "three"),
        ("multi", ["one", "three"]),
        ("multi", ["one", "one"]),
    ],
)
def test_invalid_profile_values_are_not_present(
    typed_profile: TenantConfig,
    name: str,
    value: object,
) -> None:
    result = validate_proposal_fields(
        _proposal([{"name": name, "value": value}]),
        _runtime(typed_profile),
    )

    assert result.values == {}
    assert result.invalid_fields == (name,)


@pytest.mark.parametrize("name", ["phone", "address"])
def test_blank_profile_values_are_treated_as_missing(
    typed_profile: TenantConfig,
    name: str,
) -> None:
    result = validate_proposal_fields(
        _proposal([{"name": name, "value": ""}]),
        _runtime(typed_profile),
    )

    assert result.values == {}
    assert result.invalid_fields == ()


def test_body_value_wins_over_document_value(
    typed_profile: TenantConfig,
) -> None:
    proposal = _proposal(
        [
            {
                "name": "short_text",
                "value": "body value",
                "source": FieldSource.BODY,
                "source_excerpt": "body value",
            },
            {
                "name": "short_text",
                "value": "document value",
                "source": FieldSource.DOCUMENT,
                "source_excerpt": "document value",
            },
        ]
    )

    result = validate_proposal_fields(
        proposal,
        _runtime(
            typed_profile,
            body="body value",
            document_text="document value",
        ),
    )

    assert result.invalid_fields == ()
    assert result.values == {"short_text": "body value"}


def test_document_provenance_requires_matching_excerpt(
    typed_profile: TenantConfig,
) -> None:
    proposal = _proposal(
        [
            {
                "name": "short_text",
                "value": "document value",
                "source": FieldSource.DOCUMENT,
                "source_excerpt": "not present",
            }
        ]
    )

    result = validate_proposal_fields(
        proposal,
        _runtime(typed_profile, document_text="document value"),
    )

    assert result.values == {}
    assert result.invalid_fields == ("short_text",)
