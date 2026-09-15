"""Safe loading and source-aware errors for tenant YAML profiles."""

from pathlib import Path

import yaml
from pydantic import ValidationError
from yaml import YAMLError

from app.tenants.config import TenantConfig


class TenantConfigError(ValueError):
    """A source file cannot become a valid tenant configuration."""


def parse_tenant_config(content: str, *, source: str = "<string>") -> TenantConfig:
    """Parse and validate YAML content without constructing arbitrary objects."""
    try:
        raw = yaml.safe_load(content)
    except YAMLError as error:
        raise TenantConfigError(f"{source}: invalid YAML: {error}") from error

    if not isinstance(raw, dict):
        raise TenantConfigError(f"{source}: root must be a mapping")

    try:
        return TenantConfig.model_validate(raw)
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
            for item in error.errors()
        )
        raise TenantConfigError(f"{source}: invalid tenant config: {details}") from error


def load_tenant_config(path: Path) -> TenantConfig:
    """Load a UTF-8 YAML profile from ``path`` and validate its complete contract."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as error:
        raise TenantConfigError(f"{path}: cannot read configuration: {error}") from error
    return parse_tenant_config(content, source=str(path))
