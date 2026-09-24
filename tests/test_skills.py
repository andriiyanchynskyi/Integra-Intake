import pytest
import yaml
from pydantic import ValidationError

from app.agent import MessageRole, ProposalValue
from app.skills import (
    ALL_SKILLS,
    CLASSIFY_INTAKE_V1,
    DRAFT_OPS_REPLY_V1,
    EXTRACT_RATE_CONFIRMATION_V1,
    EXTRACT_LOAD_REQUEST_V1,
)


_EXPECTED_SKILL_NAMES = (
    "extract_load_request.v1",
    "classify_intake.v1",
    "draft_ops_reply.v1",
    "extract_rate_confirmation.v1",
)

_INPUT_SAMPLES: dict[str, dict[str, object]] = {
    "extract_load_request.v1": {
        "subject": "Need a dry van",
        "body": "Chicago to Detroit",
        "known_fields": ["origin", "destination", "equipment"],
    },
    "classify_intake.v1": {
        "subject": "Quote request",
        "body": "Please quote this load.",
        "known_intake_types": ["load_request", "other"],
    },
    "draft_ops_reply.v1": {
        "rationale": "The pickup window is missing.",
        "missing_required_fields": ["pickup_window"],
        "recipient": "ops@example.test",
    },
    "extract_rate_confirmation.v1": {
        "document_text": "Origin: Chicago\nDestination: Detroit",
        "known_fields": ["origin", "destination", "quoted_rate", "valid_until"],
    },
}

_OUTPUT_SAMPLES: dict[str, dict[str, object]] = {
    "extract_load_request.v1": {
        "fields": [{"name": "origin", "value": "Chicago"}],
        "missing_required_fields": ["destination"],
    },
    "classify_intake.v1": {
        "intake_type": "load_request",
        "rationale": "The message asks for a freight quote.",
    },
    "draft_ops_reply.v1": {
        "recipient": "ops@example.test",
        "subject": "More information needed",
        "body": "Please provide the pickup window.",
    },
    "extract_rate_confirmation.v1": {
        "fields": [
            {
                "name": "origin",
                "value": "Chicago",
                "source_excerpt": "Origin: Chicago",
            }
        ],
        "missing_required_fields": ["destination", "quoted_rate", "valid_until"],
    },
}


@pytest.mark.parametrize(
    "skill",
    ALL_SKILLS,
    ids=lambda skill: skill.name,
)
def test_skill_definitions_have_exact_versioned_names_and_prompts(skill) -> None:
    assert tuple(item.name for item in ALL_SKILLS) == _EXPECTED_SKILL_NAMES
    assert skill.system_prompt.strip()
    assert skill.eval_fixture.is_file()


@pytest.mark.parametrize(
    "skill",
    ALL_SKILLS,
    ids=lambda skill: skill.name,
)
def test_skill_builds_a_system_message(skill) -> None:
    message = skill.build_system_message()

    assert message.role is MessageRole.SYSTEM
    assert message.content == skill.system_prompt
    assert message.tool_call is None
    assert message.proposal is None


@pytest.mark.parametrize(
    "skill",
    ALL_SKILLS,
    ids=lambda skill: skill.name,
)
def test_skill_input_schema_rejects_unknown_keys(skill) -> None:
    payload = dict(_INPUT_SAMPLES[skill.name])
    skill.input_model.model_validate(payload)

    with pytest.raises(ValidationError):
        skill.input_model.model_validate({**payload, "unexpected": "value"})


@pytest.mark.parametrize(
    "skill",
    ALL_SKILLS,
    ids=lambda skill: skill.name,
)
def test_skill_output_schema_rejects_unknown_keys(skill) -> None:
    payload = dict(_OUTPUT_SAMPLES[skill.name])
    skill.output_model.model_validate(payload)

    with pytest.raises(ValidationError):
        skill.output_model.model_validate({**payload, "unexpected": "value"})


def test_skill_outputs_are_candidates_not_tools_or_policy_decisions() -> None:
    forbidden_fields = {
        "allow",
        "allowed",
        "authorize",
        "may_authorize",
        "permission",
        "policy",
        "tool_call",
        "tool_calls",
    }

    for skill in ALL_SKILLS:
        assert not forbidden_fields.intersection(skill.output_model.model_fields)
        assert "ToolCall" not in repr(skill.output_model.model_fields)


def test_rate_confirmation_skill_is_exported_in_the_declared_order() -> None:
    assert EXTRACT_RATE_CONFIRMATION_V1 is ALL_SKILLS[-1]
    assert EXTRACT_RATE_CONFIRMATION_V1.name == "extract_rate_confirmation.v1"
    assert EXTRACT_RATE_CONFIRMATION_V1.input_model.model_fields.keys() == {
        "document_text",
        "known_fields",
    }
    assert EXTRACT_RATE_CONFIRMATION_V1.output_model.model_fields.keys() == {
        "fields",
        "missing_required_fields",
    }


def test_rate_confirmation_skill_is_candidate_only_and_treats_source_as_data() -> None:
    prompt = EXTRACT_RATE_CONFIRMATION_V1.system_prompt.lower()

    assert "candidate" in prompt
    assert "source" in prompt or "document" in prompt
    assert "authorize" in prompt or "permission" in prompt
    assert not {
        "allow",
        "allowed",
        "authorize",
        "permission",
        "policy",
        "tool_call",
        "tool_calls",
    }.intersection(EXTRACT_RATE_CONFIRMATION_V1.output_model.model_fields)


def test_proposal_value_source_excerpt_is_optional_for_existing_payloads() -> None:
    value = ProposalValue.model_validate({"name": "origin", "value": "Chicago"})
    with_excerpt = ProposalValue.model_validate(
        {
            "name": "origin",
            "value": "Chicago",
            "source_excerpt": "Origin: Chicago",
        }
    )

    assert value.source_excerpt is None
    assert with_excerpt.source_excerpt == "Origin: Chicago"


@pytest.mark.parametrize("source_excerpt", ["", "   ", "x" * 501])
def test_proposal_value_rejects_blank_or_oversized_source_excerpt(
    source_excerpt: str,
) -> None:
    with pytest.raises(ValidationError):
        ProposalValue.model_validate(
            {
                "name": "origin",
                "value": "Chicago",
                "source_excerpt": source_excerpt,
            }
        )


@pytest.mark.parametrize(
    "skill",
    ALL_SKILLS,
    ids=lambda skill: skill.name,
)
def test_each_skill_fixture_has_normal_and_adversarial_non_authorizing_cases(skill) -> None:
    fixture = yaml.safe_load(skill.eval_fixture.read_text(encoding="utf-8"))

    assert fixture["name"] == skill.name
    cases = fixture["cases"]
    assert len(cases) == 2

    names = [case["name"].lower() for case in cases]
    assert any("injection" in name or "adversarial" in name for name in names)
    assert any("injection" not in name and "adversarial" not in name for name in names)

    for case in cases:
        assert case["input"]
        assert case["expected_output"]
        assert case["metadata"]["may_authorize"] is False
        skill.input_model.model_validate(case["input"])
        skill.output_model.model_validate(case["expected_output"])
