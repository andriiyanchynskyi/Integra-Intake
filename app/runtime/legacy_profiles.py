"""Hash-preserving compatibility upgrade for persisted profile snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
import re

from app.tools.registry import BUILTIN_ACTION_REGISTRY


_SAFE_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def upgrade_legacy_profile_snapshot(
    snapshot: Mapping[str, object],
) -> dict[str, object]:
    """Return a v2 mapping derived from a validated v1 profile snapshot."""

    upgraded = deepcopy(dict(snapshot))
    raw_slug = upgraded.get("slug")
    if not isinstance(raw_slug, str) or _SAFE_SLUG.fullmatch(raw_slug) is None:
        raise ValueError("legacy tenant profile is invalid")

    action_policy = upgraded.get("action_policy")
    if not isinstance(action_policy, Mapping):
        raise ValueError("legacy tenant profile is invalid")
    upgraded["action_policy"] = {
        str(action_key): {
            **dict(action_rule),
            "execution": (
                "executable"
                if action_key in BUILTIN_ACTION_REGISTRY.keys
                else "policy_only"
            ),
        }
        for action_key, action_rule in action_policy.items()
        if isinstance(action_key, str) and isinstance(action_rule, Mapping)
    }
    if len(upgraded["action_policy"]) != len(action_policy):
        raise ValueError("legacy tenant profile is invalid")

    upgraded["profile_version"] = 2
    upgraded["scenario_key"] = raw_slug.replace("-", "_")

    documents = upgraded.get("documents")
    if documents is None:
        documents = {}
    if not isinstance(documents, Mapping):
        raise ValueError("legacy tenant profile is invalid")
    upgraded["documents"] = deepcopy(dict(documents))
    intake_types = upgraded.get("intake_types")
    if not isinstance(intake_types, list):
        raise ValueError("legacy tenant profile is invalid")
    intake_names = {
        item.get("name")
        for item in intake_types
        if isinstance(item, Mapping)
    }
    if "rate_confirmation" in intake_names:
        upgraded["documents"].setdefault(
            "rate_confirmation",
            {
                "intake_type": "rate_confirmation",
                "normalizer": "bounded_text_pdf",
                "normalizer_version": 1,
                "default_for_inbound": True,
            },
        )
    return upgraded


__all__ = ["upgrade_legacy_profile_snapshot"]
