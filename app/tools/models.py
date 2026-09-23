"""Closed argument/result models for the Phase-6 internal tools."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictStr,
    model_validator,
)

from app.agent import AgentProposal, ToolData
from app.policy import TrustedToolRuntimeContext


class StrictToolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _parse_uuid(value: object) -> UUID:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        return UUID(value)
    raise TypeError("value must be a UUID")


ToolUUID = Annotated[UUID, BeforeValidator(_parse_uuid)]


class FindCustomerArgs(StrictToolModel):
    email: StrictStr | None = None
    external_id: StrictStr | None = None

    @model_validator(mode="after")
    def require_lookup_key(self) -> Self:
        if not self.email and not self.external_id:
            raise ValueError("email or external_id is required")
        return self


class CreateCaseArgs(StrictToolModel):
    customer_id: ToolUUID | None = None


class UpdateCaseFieldsArgs(StrictToolModel):
    case_id: ToolUUID


class CreateReplyDraftArgs(StrictToolModel):
    case_id: ToolUUID | None = None


class FlagForReviewArgs(StrictToolModel):
    note: StrictStr | None = Field(default=None, max_length=500)


class CustomerSummary(StrictToolModel):
    id: str
    external_id: str | None = None
    name: str | None = None
    email: str | None = None
    phone: str | None = None


class CustomerLookupResult(StrictToolModel):
    found: bool
    customer: CustomerSummary | None = None

    @model_validator(mode="after")
    def validate_found_consistency(self) -> Self:
        if self.found != (self.customer is not None):
            raise ValueError("found must match customer presence")
        return self


class CreatedCase(StrictToolModel):
    id: str
    status: str
    accepted_fields: list[str]


class UpdatedCase(StrictToolModel):
    id: str
    updated_fields: list[str]


class ReplyDraft(StrictToolModel):
    recipient: str | None = None
    subject: str
    body: str
    persisted: bool = False


class ReviewFlag(StrictToolModel):
    reason: StrictStr = Field(min_length=1, max_length=1000)
    persisted: bool = False


class PendingAction(StrictToolModel):
    """Closed, validated command frozen for a human approval decision."""

    version: Literal[1] = 1
    name: Literal[
        "find_customer",
        "create_case",
        "update_case_fields",
        "create_reply_draft",
        "flag_for_review",
    ]
    # Values are JSON-compatible dumps of already validated tool/proposal data.
    arguments: dict[str, object]
    known_fields: dict[str, object]


class ApprovalRequested(StrictToolModel):
    id: UUID
    expires_at: datetime


ToolHandler = Callable[
    [BaseModel, AgentProposal, TrustedToolRuntimeContext], ToolData
]


class ToolDefinition:
    """Runtime metadata for one typed tool implementation."""

    def __init__(
        self,
        *,
        name: str,
        requires_complete_fields: bool,
        arguments_model: type[BaseModel],
        handler: ToolHandler,
    ) -> None:
        self.name = name
        self.requires_complete_fields = requires_complete_fields
        self.arguments_model = arguments_model
        self.handler = handler
