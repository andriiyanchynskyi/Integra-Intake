"""Trusted, versioned tenant-profile resolution for current and persisted jobs."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import re

from app.documents.registry import (
    BUILTIN_DOCUMENT_REGISTRY,
    DocumentNormalizerRegistry,
)
from app.runtime.legacy_profiles import upgrade_legacy_profile_snapshot
from app.tenants.compiled import CompiledTenantProfile, compile_tenant_profile
from app.tenants.loader import TenantConfigError, load_tenant_config
from app.tenants.config import TenantConfig
from app.tools.registry import ActionRegistry, BUILTIN_ACTION_REGISTRY


_SAFE_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class TenantProfileUnavailableError(RuntimeError):
    """A trusted tenant profile cannot be loaded or validated."""


@dataclass(frozen=True, slots=True)
class ResolvedTenantProfile:
    config: TenantConfig
    snapshot: dict[str, object]
    sha256: str
    compiled: CompiledTenantProfile | None = None


def canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _profile_fingerprint(snapshot: Mapping[str, object]) -> str:
    return hashlib.sha256(canonical_json_bytes(snapshot)).hexdigest()


def _resolved_profile(
    *,
    config: TenantConfig,
    snapshot: Mapping[str, object],
    sha256: str,
    action_registry: ActionRegistry,
    document_registry: DocumentNormalizerRegistry,
) -> ResolvedTenantProfile:
    compiled = compile_tenant_profile(
        config,
        profile_fingerprint=sha256,
        action_registry=action_registry,
        document_registry=document_registry,
    )
    return ResolvedTenantProfile(
        config=config,
        snapshot=deepcopy(dict(snapshot)),
        sha256=sha256,
        compiled=compiled,
    )


def resolve_persisted_profile(
    snapshot: Mapping[str, object],
    expected_sha256: str,
    *,
    tenant_slug: str | None = None,
    action_registry: ActionRegistry = BUILTIN_ACTION_REGISTRY,
    document_registry: DocumentNormalizerRegistry = BUILTIN_DOCUMENT_REGISTRY,
) -> ResolvedTenantProfile:
    """Verify a stored mapping before applying any compatibility upgrade."""

    if not isinstance(snapshot, Mapping) or not isinstance(expected_sha256, str):
        raise TenantProfileUnavailableError("tenant profile snapshot is unavailable")
    if _SHA256.fullmatch(expected_sha256) is None:
        raise TenantProfileUnavailableError("tenant profile snapshot is unavailable")
    try:
        actual_sha256 = _profile_fingerprint(snapshot)
    except (TypeError, ValueError):
        raise TenantProfileUnavailableError(
            "tenant profile snapshot is unavailable"
        ) from None
    if actual_sha256 != expected_sha256:
        raise TenantProfileUnavailableError("tenant profile snapshot hash mismatch")

    try:
        if snapshot.get("profile_version") == 2:
            upgraded = deepcopy(dict(snapshot))
        else:
            upgraded = upgrade_legacy_profile_snapshot(snapshot)
        config = TenantConfig.model_validate(upgraded)
    except Exception as error:
        raise TenantProfileUnavailableError(
            "tenant profile snapshot is unavailable"
        ) from error
    if tenant_slug is not None and config.slug != tenant_slug:
        raise TenantProfileUnavailableError("tenant profile snapshot is unavailable")
    return _resolved_profile(
        config=config,
        snapshot=snapshot,
        sha256=expected_sha256,
        action_registry=action_registry,
        document_registry=document_registry,
    )


class TenantProfileResolver:
    """Resolve tenant profiles and compile their immutable capabilities."""

    def __init__(
        self,
        directory: Path | str,
        *,
        action_registry: ActionRegistry = BUILTIN_ACTION_REGISTRY,
        document_registry: DocumentNormalizerRegistry = BUILTIN_DOCUMENT_REGISTRY,
    ) -> None:
        self._directory = Path(directory)
        self._action_registry = action_registry
        self._document_registry = document_registry

    def resolve(self, tenant_slug: str) -> ResolvedTenantProfile:
        if not _SAFE_SLUG.fullmatch(tenant_slug):
            raise TenantProfileUnavailableError("tenant profile is unavailable")
        path = self._directory / f"{tenant_slug}.yaml"
        try:
            config = load_tenant_config(path)
        except (TenantConfigError, OSError) as error:
            raise TenantProfileUnavailableError(
                "tenant profile is unavailable"
            ) from error
        if config.slug != tenant_slug:
            raise TenantProfileUnavailableError("tenant profile is unavailable")
        snapshot = config.model_dump(mode="json")
        return _resolved_profile(
            config=config,
            snapshot=snapshot,
            sha256=_profile_fingerprint(snapshot),
            action_registry=self._action_registry,
            document_registry=self._document_registry,
        )

    def resolve_snapshot(
        self,
        snapshot: Mapping[str, object],
        expected_sha256: str,
        *,
        tenant_slug: str | None = None,
    ) -> ResolvedTenantProfile:
        return resolve_persisted_profile(
            snapshot,
            expected_sha256,
            tenant_slug=tenant_slug,
            action_registry=self._action_registry,
            document_registry=self._document_registry,
        )

    def resolve_persisted(
        self,
        snapshot: Mapping[str, object],
        expected_sha256: str,
        *,
        tenant_slug: str | None = None,
    ) -> ResolvedTenantProfile:
        """Alias used by worker/runtime callers for persisted snapshots."""

        return self.resolve_snapshot(
            snapshot,
            expected_sha256,
            tenant_slug=tenant_slug,
        )


__all__ = [
    "ResolvedTenantProfile",
    "TenantProfileResolver",
    "TenantProfileUnavailableError",
    "canonical_json_bytes",
    "resolve_persisted_profile",
]
