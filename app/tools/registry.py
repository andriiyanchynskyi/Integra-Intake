"""Immutable, server-owned metadata for executable actions."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
import re
from types import MappingProxyType

from pydantic import BaseModel

from app.tenants.identifiers import SAFE_IDENTIFIER_PATTERN
from app.tools.models import (
    CreateCaseArgs,
    CreateReplyDraftArgs,
    FindCustomerArgs,
    FlagForReviewArgs,
    UpdateCaseFieldsArgs,
)


class ActionCapabilityUnavailable(ValueError):
    """The requested action is not available in this deployment."""


@dataclass(frozen=True, slots=True)
class ActionCapability:
    key: str
    arguments_model: type[BaseModel]
    requires_complete_fields: bool
    commits_side_effect: bool
    pending_action_version: int = 1

    def __post_init__(self) -> None:
        if re.fullmatch(SAFE_IDENTIFIER_PATTERN, self.key) is None:
            raise ValueError("action capability key must be a safe identifier")
        if not isinstance(self.arguments_model, type) or not issubclass(
            self.arguments_model, BaseModel
        ):
            raise TypeError("action capability arguments_model must be a Pydantic model")
        if type(self.requires_complete_fields) is not bool:
            raise TypeError("requires_complete_fields must be a boolean")
        if type(self.commits_side_effect) is not bool:
            raise TypeError("commits_side_effect must be a boolean")
        if type(self.pending_action_version) is not int or self.pending_action_version < 1:
            raise ValueError("pending_action_version must be a positive integer")


class ActionRegistry:
    """Immutable lookup table for known executable action descriptors."""

    def __init__(self, capabilities: Iterable[ActionCapability]) -> None:
        values: dict[str, ActionCapability] = {}
        for capability in capabilities:
            if capability.key in values:
                raise ValueError("action capability keys must be unique")
            values[capability.key] = capability
        self._capabilities = MappingProxyType(values)

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(self._capabilities)

    def require(self, key: str) -> ActionCapability:
        capability = self._capabilities.get(key)
        if capability is None:
            raise ActionCapabilityUnavailable("action capability unavailable")
        return capability


BUILTIN_ACTION_REGISTRY = ActionRegistry(
    (
        ActionCapability(
            key="find_customer",
            arguments_model=FindCustomerArgs,
            requires_complete_fields=False,
            commits_side_effect=False,
        ),
        ActionCapability(
            key="create_case",
            arguments_model=CreateCaseArgs,
            requires_complete_fields=True,
            commits_side_effect=True,
        ),
        ActionCapability(
            key="update_case_fields",
            arguments_model=UpdateCaseFieldsArgs,
            requires_complete_fields=True,
            commits_side_effect=True,
        ),
        ActionCapability(
            key="create_reply_draft",
            arguments_model=CreateReplyDraftArgs,
            requires_complete_fields=False,
            commits_side_effect=False,
        ),
        ActionCapability(
            key="flag_for_review",
            arguments_model=FlagForReviewArgs,
            requires_complete_fields=False,
            commits_side_effect=False,
        ),
    )
)


__all__ = [
    "ActionCapability",
    "ActionCapabilityUnavailable",
    "ActionRegistry",
    "BUILTIN_ACTION_REGISTRY",
]
