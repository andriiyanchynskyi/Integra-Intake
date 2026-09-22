"""Trusted, versioned tenant-profile resolution for persisted jobs."""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Mapping

from app.tenants.config import TenantConfig
from app.tenants.loader import TenantConfigError, load_tenant_config


_SAFE_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class TenantProfileUnavailableError(RuntimeError):
    """A trusted tenant profile cannot be loaded or validated."""


@dataclass(frozen=True, slots=True)
class ResolvedTenantProfile:
    config: TenantConfig
    snapshot: dict[str, object]
    sha256: str


def canonical_json_bytes(value: Mapping[str, object]) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


class TenantProfileResolver:
    """Resolve a tenant slug to one checked-in, validated profile file."""

    def __init__(self, directory: Path | str) -> None:
        self._directory = Path(directory)

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
        encoded = canonical_json_bytes(snapshot)
        return ResolvedTenantProfile(
            config=config,
            snapshot=deepcopy(snapshot),
            sha256=hashlib.sha256(encoded).hexdigest(),
        )
