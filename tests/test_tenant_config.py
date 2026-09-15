from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.tenants.config import FieldType, RoutingDecision, RoutingStatus, TenantConfig
from app.tenants.loader import TenantConfigError, load_tenant_config, parse_tenant_config
from app.tenants.routing import RoutingAssessment, TenantRouter


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def valid_profile() -> dict[str, object]:
    return {
        "slug": "acme",
        "display_name": "Acme",
        "intake_types": [
            {
                "name": "request",
                "description": "A request",
                "required_fields": ["summary"],
            }
        ],
        "fields": {
            "summary": {"type": "short_text", "label": "Summary"},
        },
        "action_policy": {
            "create_case": {"allowed": True, "requires_approval": False},
            "send_reply": {"allowed": True, "requires_approval": True},
        },
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


def test_tenant_config_accepts_a_minimal_strict_profile(
    valid_profile: dict[str, object],
) -> None:
    config = TenantConfig.model_validate(valid_profile)

    assert config.fields["summary"].type is FieldType.SHORT_TEXT
    assert config.intake_types[0].required_fields == ["summary"]


def test_tenant_config_rejects_an_unknown_field_type(
    valid_profile: dict[str, object],
) -> None:
    valid_profile["fields"]["summary"]["type"] = "made_up"  # type: ignore[index]

    with pytest.raises(ValidationError):
        TenantConfig.model_validate(valid_profile)


@pytest.mark.parametrize(
    ("key", "value"),
    [("slug", "Bad Slug"), ("display_name", "")],
)
def test_tenant_config_rejects_invalid_identity_values(
    valid_profile: dict[str, object], key: str, value: str
) -> None:
    valid_profile[key] = value

    with pytest.raises(ValidationError):
        TenantConfig.model_validate(valid_profile)


def test_tenant_config_rejects_duplicate_intake_type_names(
    valid_profile: dict[str, object],
) -> None:
    duplicate = deepcopy(valid_profile["intake_types"][0])  # type: ignore[index]
    valid_profile["intake_types"].append(duplicate)  # type: ignore[union-attr]

    with pytest.raises(ValidationError, match="unique names"):
        TenantConfig.model_validate(valid_profile)


def test_tenant_config_rejects_an_empty_intake_type_name(
    valid_profile: dict[str, object],
) -> None:
    valid_profile["intake_types"][0]["name"] = ""  # type: ignore[index]

    with pytest.raises(ValidationError):
        TenantConfig.model_validate(valid_profile)


def test_tenant_config_rejects_an_undeclared_required_field(
    valid_profile: dict[str, object],
) -> None:
    valid_profile["intake_types"][0]["required_fields"] = ["missing"]  # type: ignore[index]

    with pytest.raises(ValidationError, match="unknown fields: missing"):
        TenantConfig.model_validate(valid_profile)


@pytest.mark.parametrize(
    "field_definition",
    [
        {"type": "single_select", "label": "Choice"},
        {"type": "multi_select", "label": "Choices", "options": []},
        {"type": "short_text", "label": "Summary", "options": ["brief"]},
        {"type": "single_select", "label": "Choice", "options": ["a", "a"]},
    ],
)
def test_tenant_config_rejects_invalid_field_options(
    valid_profile: dict[str, object], field_definition: dict[str, object]
) -> None:
    valid_profile["fields"]["summary"] = field_definition  # type: ignore[index]

    with pytest.raises(ValidationError):
        TenantConfig.model_validate(valid_profile)


def test_tenant_config_rejects_duplicate_required_fields(
    valid_profile: dict[str, object],
) -> None:
    valid_profile["intake_types"][0]["required_fields"] = ["summary", "summary"]  # type: ignore[index]

    with pytest.raises(ValidationError, match="unique names"):
        TenantConfig.model_validate(valid_profile)


def test_tenant_config_rejects_an_incomplete_routing_catalog(
    valid_profile: dict[str, object],
) -> None:
    valid_profile["routing"]["outcome_names"] = ["urgent", "rejected"]  # type: ignore[index]

    with pytest.raises(ValidationError, match="each supported routing status exactly once"):
        TenantConfig.model_validate(valid_profile)


def test_tenant_config_rejects_missing_send_reply_approval_action(
    valid_profile: dict[str, object],
) -> None:
    valid_profile["routing"]["always_approval_actions"] = ["create_case"]  # type: ignore[index]

    with pytest.raises(ValidationError, match="include send_reply"):
        TenantConfig.model_validate(valid_profile)


def test_tenant_config_rejects_duplicate_approval_actions(
    valid_profile: dict[str, object],
) -> None:
    valid_profile["routing"]["always_approval_actions"] = ["send_reply", "send_reply"]  # type: ignore[index]

    with pytest.raises(ValidationError, match="unique names"):
        TenantConfig.model_validate(valid_profile)


def test_tenant_config_rejects_an_unknown_approval_action(
    valid_profile: dict[str, object],
) -> None:
    valid_profile["routing"]["always_approval_actions"] = ["send_reply", "archive"]  # type: ignore[index]

    with pytest.raises(ValidationError, match="unknown actions: archive"):
        TenantConfig.model_validate(valid_profile)


def test_tenant_config_rejects_extra_root_keys(valid_profile: dict[str, object]) -> None:
    valid_profile["tenant_id"] = "not-part-of-the-profile"

    with pytest.raises(ValidationError):
        TenantConfig.model_validate(valid_profile)


def test_tenant_config_rejects_non_strict_primitive_values(
    valid_profile: dict[str, object],
) -> None:
    valid_profile["display_name"] = 123
    valid_profile["action_policy"]["create_case"]["allowed"] = "yes"  # type: ignore[index]

    with pytest.raises(ValidationError):
        TenantConfig.model_validate(valid_profile)


def test_parse_tenant_config_wraps_yaml_syntax_errors() -> None:
    with pytest.raises(TenantConfigError, match=r"broken\.yaml: invalid YAML"):
        parse_tenant_config("slug: [", source="broken.yaml")


def test_load_tenant_config_wraps_validation_errors_with_path(tmp_path: Path) -> None:
    path = tmp_path / "invalid.yaml"
    path.write_text("slug: bad profile\n", encoding="utf-8")

    with pytest.raises(TenantConfigError, match=r"invalid\.yaml: invalid tenant config"):
        load_tenant_config(path)


def test_load_tenant_config_wraps_file_read_errors_with_path(tmp_path: Path) -> None:
    path = tmp_path / "missing.yaml"

    with pytest.raises(TenantConfigError, match=r"missing\.yaml: cannot read configuration"):
        load_tenant_config(path)


def test_parse_tenant_config_rejects_a_non_mapping_root() -> None:
    with pytest.raises(TenantConfigError, match="root must be a mapping"):
        parse_tenant_config("- item\n", source="list.yaml")


def test_load_tenant_config_rejects_an_unknown_field_type(tmp_path: Path) -> None:
    path = tmp_path / "unknown-field-type.yaml"
    path.write_text(
        """\
slug: acme
display_name: Acme
intake_types:
  - name: request
    description: A request
    required_fields: [summary]
fields:
  summary: {type: made_up, label: Summary}
action_policy:
  send_reply: {allowed: true, requires_approval: true}
routing:
  outcome_names: [urgent, awaiting_input, pending_approval, ready, rejected]
  always_approval_actions: [send_reply]
""",
        encoding="utf-8",
    )

    with pytest.raises(TenantConfigError, match="invalid tenant config"):
        load_tenant_config(path)


@pytest.mark.parametrize(
    ("filename", "expected_slug"),
    [
        ("freight-broker.yaml", "freight-broker"),
        ("repair-service.yaml", "repair-service"),
        ("language-school.yaml", "language-school"),
    ],
)
def test_example_tenant_profiles_load(filename: str, expected_slug: str) -> None:
    config = load_tenant_config(PROJECT_ROOT / "examples" / filename)

    assert config.slug == expected_slug


def test_freight_profile_preserves_the_required_intake_catalog() -> None:
    config = load_tenant_config(PROJECT_ROOT / "examples" / "freight-broker.yaml")
    intake_types = {intake.name: intake for intake in config.intake_types}

    assert set(intake_types) == {
        "load_request",
        "rate_confirmation",
        "invoice_query",
        "other",
    }
    assert set(intake_types["load_request"].required_fields) == {
        "origin",
        "destination",
        "equipment",
        "pickup_window",
        "commodity",
        "contact",
    }


@pytest.fixture
def freight_router() -> TenantRouter:
    config = load_tenant_config(PROJECT_ROOT / "examples" / "freight-broker.yaml")
    return TenantRouter(config)


def make_load_assessment(
    *,
    requested_action: str = "create_case",
    present_fields: frozenset[str] | None = None,
    contains_risk: bool = False,
) -> RoutingAssessment:
    return RoutingAssessment(
        intake_type="load_request",
        present_fields=(
            present_fields
            if present_fields is not None
            else frozenset(
                {
                    "origin",
                    "destination",
                    "equipment",
                    "pickup_window",
                    "commodity",
                    "contact",
                }
            )
        ),
        contains_safety_or_legal_risk=contains_risk,
        requested_action=requested_action,
    )


def test_router_prioritizes_safety_risk_over_an_unknown_action(
    freight_router: TenantRouter,
) -> None:
    outcome = freight_router.route(
        make_load_assessment(requested_action="unknown", contains_risk=True)
    )

    assert outcome.status is RoutingStatus.URGENT
    assert outcome.decision is RoutingDecision.NEEDS_APPROVAL
    assert outcome.missing_required_fields == ()
    assert outcome.reason == "safety_or_legal_risk"


def test_router_rejects_an_unknown_intake_type(freight_router: TenantRouter) -> None:
    outcome = freight_router.route(
        RoutingAssessment("unknown", frozenset(), False, "create_case")
    )

    assert outcome.status is RoutingStatus.REJECTED
    assert outcome.decision is RoutingDecision.DENY
    assert outcome.reason == "unknown_intake_type"


def test_router_returns_sorted_missing_required_fields(freight_router: TenantRouter) -> None:
    outcome = freight_router.route(
        make_load_assessment(
            present_fields=frozenset({"origin", "pickup_window", "commodity", "contact"})
        )
    )

    assert outcome.status is RoutingStatus.AWAITING_INPUT
    assert outcome.decision is RoutingDecision.DENY
    assert outcome.missing_required_fields == ("destination", "equipment")
    assert outcome.reason == "missing_required_fields"


def test_router_checks_missing_fields_before_an_unknown_action(
    freight_router: TenantRouter,
) -> None:
    outcome = freight_router.route(
        make_load_assessment(requested_action="unknown", present_fields=frozenset())
    )

    assert outcome.status is RoutingStatus.AWAITING_INPUT
    assert outcome.decision is RoutingDecision.DENY
    assert outcome.reason == "missing_required_fields"


def test_router_allows_a_complete_configured_action(freight_router: TenantRouter) -> None:
    outcome = freight_router.route(make_load_assessment())

    assert outcome.status is RoutingStatus.READY
    assert outcome.decision is RoutingDecision.ALLOW
    assert outcome.missing_required_fields == ()
    assert outcome.reason == "action_allowed"


def test_router_requires_approval_for_send_reply(freight_router: TenantRouter) -> None:
    outcome = freight_router.route(make_load_assessment(requested_action="send_reply"))

    assert outcome.status is RoutingStatus.PENDING_APPROVAL
    assert outcome.decision is RoutingDecision.NEEDS_APPROVAL
    assert outcome.reason == "approval_required"


@pytest.mark.parametrize("approval_source", ["rule", "routing_catalog"])
def test_router_honors_each_approval_mechanism(
    valid_profile: dict[str, object], approval_source: str
) -> None:
    if approval_source == "rule":
        valid_profile["action_policy"]["create_case"]["requires_approval"] = True  # type: ignore[index]
        action = "create_case"
    else:
        valid_profile["action_policy"]["archive"] = {  # type: ignore[index]
            "allowed": True,
            "requires_approval": False,
        }
        valid_profile["routing"]["always_approval_actions"].append("archive")  # type: ignore[union-attr]
        action = "archive"
    router = TenantRouter(TenantConfig.model_validate(valid_profile))

    outcome = router.route(
        RoutingAssessment("request", frozenset({"summary"}), False, action)
    )

    assert outcome.status is RoutingStatus.PENDING_APPROVAL
    assert outcome.decision is RoutingDecision.NEEDS_APPROVAL
    assert outcome.reason == "approval_required"


def test_router_rejects_an_unknown_action(freight_router: TenantRouter) -> None:
    outcome = freight_router.route(make_load_assessment(requested_action="unknown"))

    assert outcome.status is RoutingStatus.REJECTED
    assert outcome.decision is RoutingDecision.DENY
    assert outcome.reason == "action_not_configured"


def test_router_rejects_an_explicitly_disallowed_action(
    valid_profile: dict[str, object],
) -> None:
    valid_profile["action_policy"]["create_case"]["allowed"] = False  # type: ignore[index]
    router = TenantRouter(TenantConfig.model_validate(valid_profile))

    outcome = router.route(
        RoutingAssessment("request", frozenset({"summary"}), False, "create_case")
    )

    assert outcome.status is RoutingStatus.REJECTED
    assert outcome.decision is RoutingDecision.DENY
    assert outcome.reason == "action_not_allowed"
