"""Pydantic contract for version-controlled tenant workflow profiles."""

from enum import Enum
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from app.tenants.identifiers import SafeIdentifier


class StrictModel(BaseModel):
    """Reject unknown keys while preserving YAML enum serialization."""

    model_config = ConfigDict(extra="forbid")


class FieldType(str, Enum):
    SHORT_TEXT = "short_text"
    LONG_TEXT = "long_text"
    EMAIL = "email"
    PHONE = "phone"
    ADDRESS = "address"
    DATE_TIME = "date_time"
    NUMBER = "number"
    BOOLEAN = "boolean"
    SINGLE_SELECT = "single_select"
    MULTI_SELECT = "multi_select"


class RoutingStatus(str, Enum):
    URGENT = "urgent"
    AWAITING_INPUT = "awaiting_input"
    PENDING_APPROVAL = "pending_approval"
    READY = "ready"
    REJECTED = "rejected"


class RoutingDecision(str, Enum):
    ALLOW = "allow"
    NEEDS_APPROVAL = "needs_approval"
    DENY = "deny"


class FieldDefinition(StrictModel):
    type: FieldType
    label: StrictStr = Field(min_length=1)
    options: list[StrictStr] | None = None

    @model_validator(mode="after")
    def validate_options(self) -> Self:
        selectable = self.type in {FieldType.SINGLE_SELECT, FieldType.MULTI_SELECT}
        if selectable and (not self.options or len(set(self.options)) != len(self.options)):
            raise ValueError("select fields require unique non-empty options")
        if not selectable and self.options is not None:
            raise ValueError("options are allowed only for select fields")
        return self


class IntakeTypeConfig(StrictModel):
    name: SafeIdentifier
    description: StrictStr = Field(min_length=1)
    required_fields: list[SafeIdentifier] = Field(default_factory=list)

    @field_validator("required_fields")
    @classmethod
    def validate_unique_required_fields(cls, value: list[StrictStr]) -> list[StrictStr]:
        if len(value) != len(set(value)):
            raise ValueError("required_fields must contain unique names")
        return value


class ActionExecutionMode(str, Enum):
    EXECUTABLE = "executable"
    POLICY_ONLY = "policy_only"


class ActionRule(StrictModel):
    allowed: StrictBool
    requires_approval: StrictBool
    execution: ActionExecutionMode


class DocumentBindingConfig(StrictModel):
    intake_type: SafeIdentifier
    normalizer: SafeIdentifier
    normalizer_version: Annotated[StrictInt, Field(ge=1)]
    default_for_inbound: StrictBool = False


class RoutingConfig(StrictModel):
    outcome_names: list[RoutingStatus]
    always_approval_actions: list[SafeIdentifier]

    @model_validator(mode="after")
    def validate_routing_catalog(self) -> Self:
        expected = set(RoutingStatus)
        if set(self.outcome_names) != expected or len(self.outcome_names) != len(expected):
            raise ValueError(
                "outcome_names must contain each supported routing status exactly once"
            )
        if "send_reply" not in self.always_approval_actions:
            raise ValueError("always_approval_actions must include send_reply")
        if len(self.always_approval_actions) != len(set(self.always_approval_actions)):
            raise ValueError("always_approval_actions must contain unique names")
        return self


class TenantConfig(StrictModel):
    profile_version: Literal[2]
    slug: StrictStr = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
    scenario_key: SafeIdentifier
    display_name: StrictStr = Field(min_length=1)
    intake_types: list[IntakeTypeConfig] = Field(min_length=1)
    fields: dict[SafeIdentifier, FieldDefinition] = Field(min_length=1)
    action_policy: dict[SafeIdentifier, ActionRule] = Field(min_length=1)
    documents: dict[SafeIdentifier, DocumentBindingConfig] = Field(default_factory=dict)
    routing: RoutingConfig

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        names = [intake.name for intake in self.intake_types]
        if len(names) != len(set(names)):
            raise ValueError("intake_types must contain unique names")

        field_names = set(self.fields)
        missing_fields = sorted(
            {
                field
                for intake in self.intake_types
                for field in intake.required_fields
                if field not in field_names
            }
        )
        if missing_fields:
            raise ValueError(
                "required_fields reference unknown fields: " + ", ".join(missing_fields)
            )

        action_names = set(self.action_policy)
        missing_actions = sorted(
            set(self.routing.always_approval_actions) - action_names
        )
        if missing_actions:
            raise ValueError(
                "always_approval_actions reference unknown actions: "
                + ", ".join(missing_actions)
            )

        document_intake_types = {
            document.intake_type for document in self.documents.values()
        }
        missing_document_intakes = sorted(
            document_intake_types - set(names)
        )
        if missing_document_intakes:
            raise ValueError(
                "documents reference unknown intake types: "
                + ", ".join(missing_document_intakes)
            )

        default_documents = sum(
            document.default_for_inbound for document in self.documents.values()
        )
        if default_documents > 1:
            raise ValueError("documents may contain at most one inbound default")
        return self
