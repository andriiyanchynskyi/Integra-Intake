"""Typed, policy-gated internal tool boundary."""

from app.tools.executor import PolicyGatedToolExecutor
from app.tools.in_memory import InMemoryTenantToolPort
from app.tools.models import (
    CreateCaseArgs,
    CreateReplyDraftArgs,
    CustomerLookupResult,
    CustomerSummary,
    FindCustomerArgs,
    FlagForReviewArgs,
    ApprovalRequested,
    PendingAction,
    ToolDefinition,
    UpdateCaseFieldsArgs,
)
from app.tools.ports import CustomerNotFoundError, TenantToolPort
from app.tools.registry import (
    ActionCapability,
    ActionCapabilityUnavailable,
    ActionRegistry,
    BUILTIN_ACTION_REGISTRY,
)

__all__ = [
    "CreateCaseArgs",
    "CreateReplyDraftArgs",
    "ApprovalRequested",
    "ActionCapability",
    "ActionCapabilityUnavailable",
    "ActionRegistry",
    "BUILTIN_ACTION_REGISTRY",
    "CustomerNotFoundError",
    "CustomerLookupResult",
    "CustomerSummary",
    "FindCustomerArgs",
    "FlagForReviewArgs",
    "PendingAction",
    "InMemoryTenantToolPort",
    "PolicyGatedToolExecutor",
    "TenantToolPort",
    "ToolDefinition",
    "UpdateCaseFieldsArgs",
]
