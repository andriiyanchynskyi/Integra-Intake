"""Immutable, capability-resolved tenant profile contracts."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
import json
import re
from types import MappingProxyType
from typing import Any

from app.documents.registry import (
    DocumentNormalizerCapability,
    DocumentNormalizerRegistry,
)
from app.tenants.config import (
    ActionExecutionMode,
    FieldType,
    RoutingStatus,
    TenantConfig,
)
from app.tenants.identifiers import SafeIdentifier
from app.tools.registry import (
    ActionCapability,
    ActionRegistry,
    BUILTIN_ACTION_REGISTRY,
)
from app.documents.registry import BUILTIN_DOCUMENT_REGISTRY


_PROFILE_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class CompiledDocumentBinding:
    """One profile-owned document kind resolved to an exact normalizer."""

    document_kind: SafeIdentifier
    target_intake_type: SafeIdentifier
    normalizer_key: SafeIdentifier
    normalizer_version: int
    capability: DocumentNormalizerCapability

    @property
    def intake_type(self) -> SafeIdentifier:
        return self.target_intake_type


@dataclass(frozen=True, slots=True)
class ImmutableFieldDefinition:
    type: FieldType
    label: str
    options: tuple[str, ...] | None


@dataclass(frozen=True, slots=True)
class ImmutableIntakeTypeConfig:
    name: SafeIdentifier
    description: str
    required_fields: tuple[SafeIdentifier, ...]


@dataclass(frozen=True, slots=True)
class ImmutableActionRule:
    allowed: bool
    requires_approval: bool
    execution: ActionExecutionMode


@dataclass(frozen=True, slots=True)
class ImmutableDocumentBindingConfig:
    intake_type: SafeIdentifier
    normalizer: SafeIdentifier
    normalizer_version: int
    default_for_inbound: bool


@dataclass(frozen=True, slots=True)
class ImmutableRoutingConfig:
    outcome_names: tuple[RoutingStatus, ...]
    always_approval_actions: tuple[SafeIdentifier, ...]


def _freeze_snapshot(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_snapshot(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_snapshot(item) for item in value)
    return value


def _thaw_snapshot(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_snapshot(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_snapshot(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class ImmutableTenantConfig:
    """Read-only config view used by compiled runtime boundaries."""

    profile_version: int
    slug: str
    scenario_key: SafeIdentifier
    display_name: str
    intake_types: tuple[ImmutableIntakeTypeConfig, ...]
    fields: Mapping[SafeIdentifier, ImmutableFieldDefinition]
    action_policy: Mapping[SafeIdentifier, ImmutableActionRule]
    documents: Mapping[SafeIdentifier, ImmutableDocumentBindingConfig]
    routing: ImmutableRoutingConfig
    _snapshot: Mapping[str, object]

    @classmethod
    def from_config(cls, config: TenantConfig) -> ImmutableTenantConfig:
        fields = {
            key: ImmutableFieldDefinition(
                type=value.type,
                label=value.label,
                options=tuple(value.options) if value.options is not None else None,
            )
            for key, value in config.fields.items()
        }
        intake_types = tuple(
            ImmutableIntakeTypeConfig(
                name=value.name,
                description=value.description,
                required_fields=tuple(value.required_fields),
            )
            for value in config.intake_types
        )
        action_policy = {
            key: ImmutableActionRule(
                allowed=value.allowed,
                requires_approval=value.requires_approval,
                execution=value.execution,
            )
            for key, value in config.action_policy.items()
        }
        documents = {
            key: ImmutableDocumentBindingConfig(
                intake_type=value.intake_type,
                normalizer=value.normalizer,
                normalizer_version=value.normalizer_version,
                default_for_inbound=value.default_for_inbound,
            )
            for key, value in config.documents.items()
        }
        snapshot = _freeze_snapshot(config.model_dump(mode="json"))
        if not isinstance(snapshot, Mapping):
            raise TypeError("tenant config snapshot must be a mapping")
        return cls(
            profile_version=config.profile_version,
            slug=config.slug,
            scenario_key=config.scenario_key,
            display_name=config.display_name,
            intake_types=intake_types,
            fields=MappingProxyType(fields),
            action_policy=MappingProxyType(action_policy),
            documents=MappingProxyType(documents),
            routing=ImmutableRoutingConfig(
                outcome_names=tuple(config.routing.outcome_names),
                always_approval_actions=tuple(config.routing.always_approval_actions),
            ),
            _snapshot=snapshot,
        )

    def model_dump(self, *, mode: str = "python", **_: Any) -> dict[str, object]:
        """Return a detached JSON-safe snapshot for compatibility callers."""

        del mode
        value = _thaw_snapshot(self._snapshot)
        if not isinstance(value, dict):
            raise TypeError("tenant config snapshot must be a mapping")
        return value

    def model_dump_json(self, **kwargs: Any) -> str:
        return json.dumps(self.model_dump(mode="json"), **kwargs)


@dataclass(frozen=True, slots=True)
class CompiledTenantProfile:
    """Frozen runtime description used by workers and policy boundaries."""

    config: ImmutableTenantConfig
    scenario_key: SafeIdentifier
    profile_fingerprint: str
    declared_actions: frozenset[SafeIdentifier]
    registered_actions: frozenset[SafeIdentifier]
    available_actions: frozenset[SafeIdentifier]
    documents: Mapping[SafeIdentifier, CompiledDocumentBinding]
    default_inbound_document: CompiledDocumentBinding | None


def compile_tenant_profile(
    config: TenantConfig,
    *,
    profile_fingerprint: str,
    action_registry: ActionRegistry = BUILTIN_ACTION_REGISTRY,
    document_registry: DocumentNormalizerRegistry = BUILTIN_DOCUMENT_REGISTRY,
) -> CompiledTenantProfile:
    """Resolve profile declarations against immutable server capabilities."""

    if _PROFILE_FINGERPRINT_PATTERN.fullmatch(profile_fingerprint) is None:
        raise ValueError("profile fingerprint is invalid")

    declared_actions = frozenset(config.action_policy)
    registered_actions = frozenset(
        action
        for action, rule in config.action_policy.items()
        if rule.execution is ActionExecutionMode.EXECUTABLE
        and action in action_registry.keys
    )
    available_actions: set[SafeIdentifier] = set()
    for action_key, rule in config.action_policy.items():
        if rule.execution is ActionExecutionMode.EXECUTABLE:
            capability = action_registry.require(action_key)
            if rule.allowed:
                available_actions.add(capability.key)

    compiled_documents: dict[SafeIdentifier, CompiledDocumentBinding] = {}
    for document_kind, binding in config.documents.items():
        capability = document_registry.require(
            binding.normalizer,
            binding.normalizer_version,
        )
        compiled_documents[document_kind] = CompiledDocumentBinding(
            document_kind=document_kind,
            target_intake_type=binding.intake_type,
            normalizer_key=binding.normalizer,
            normalizer_version=binding.normalizer_version,
            capability=capability,
        )

    documents = MappingProxyType(compiled_documents)
    default_documents = tuple(
        binding
        for document_kind, binding in documents.items()
        if config.documents[document_kind].default_for_inbound
    )
    default_inbound_document = (
        default_documents[0] if default_documents else None
    )

    return CompiledTenantProfile(
        config=ImmutableTenantConfig.from_config(config),
        scenario_key=config.scenario_key,
        profile_fingerprint=profile_fingerprint,
        declared_actions=declared_actions,
        registered_actions=registered_actions,
        available_actions=frozenset(available_actions),
        documents=documents,
        default_inbound_document=default_inbound_document,
    )


__all__ = [
    "CompiledDocumentBinding",
    "CompiledTenantProfile",
    "ImmutableActionRule",
    "ImmutableDocumentBindingConfig",
    "ImmutableFieldDefinition",
    "ImmutableIntakeTypeConfig",
    "ImmutableRoutingConfig",
    "ImmutableTenantConfig",
    "compile_tenant_profile",
]
