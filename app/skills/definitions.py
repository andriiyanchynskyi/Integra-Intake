"""Concrete Phase-6 skill definitions and their strict schemas."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictStr

from app.agent import ProposalValue
from app.skills.models import SkillDefinition


class _SkillModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class ExtractLoadRequestInput(_SkillModel):
    subject: StrictStr
    body: StrictStr
    known_fields: list[StrictStr]


class ExtractLoadRequestOutput(_SkillModel):
    fields: list[ProposalValue]
    missing_required_fields: list[StrictStr]


class ClassifyIntakeInput(_SkillModel):
    subject: StrictStr
    body: StrictStr
    known_intake_types: list[StrictStr]


class ClassifyIntakeOutput(_SkillModel):
    intake_type: StrictStr | None
    rationale: StrictStr = Field(min_length=1, max_length=1000)


class DraftOpsReplyInput(_SkillModel):
    rationale: StrictStr
    missing_required_fields: list[StrictStr]
    recipient: StrictStr | None = None


class DraftOpsReplyOutput(_SkillModel):
    recipient: StrictStr | None = None
    subject: StrictStr = Field(min_length=1, max_length=500)
    body: StrictStr = Field(min_length=1, max_length=4000)


_ROOT = Path(__file__).resolve().parents[2]

EXTRACT_LOAD_REQUEST_V1 = SkillDefinition(
    name="extract_load_request.v1",
    input_model=ExtractLoadRequestInput,
    output_model=ExtractLoadRequestOutput,
    system_prompt=(
        "Extract configured intake fields as candidates only. Never authorize "
        "a tool, permission, send, or persistence action."
    ),
    eval_fixture=_ROOT / "evals" / "skills" / "extract_load_request.v1.yaml",
)

CLASSIFY_INTAKE_V1 = SkillDefinition(
    name="classify_intake.v1",
    input_model=ClassifyIntakeInput,
    output_model=ClassifyIntakeOutput,
    system_prompt=(
        "Classify the intake using the supplied catalog as a candidate only. "
        "Never authorize a tool or override tenant policy."
    ),
    eval_fixture=_ROOT / "evals" / "skills" / "classify_intake.v1.yaml",
)

DRAFT_OPS_REPLY_V1 = SkillDefinition(
    name="draft_ops_reply.v1",
    input_model=DraftOpsReplyInput,
    output_model=DraftOpsReplyOutput,
    system_prompt=(
        "Draft an operations reply candidate only. Never send, approve, or "
        "persist the message and never grant tool permission."
    ),
    eval_fixture=_ROOT / "evals" / "skills" / "draft_ops_reply.v1.yaml",
)

ALL_SKILLS = (
    EXTRACT_LOAD_REQUEST_V1,
    CLASSIFY_INTAKE_V1,
    DRAFT_OPS_REPLY_V1,
)
