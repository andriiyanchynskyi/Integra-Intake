"""Unit contracts for compiled scenario profiles and legacy snapshots."""

from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path

import pytest

from app.documents.registry import DocumentCapabilityUnavailable
from app.runtime.legacy_profiles import upgrade_legacy_profile_snapshot
from app.runtime.profiles import (
    TenantProfileResolver,
    TenantProfileUnavailableError,
    canonical_json_bytes,
)
from app.tenants.compiled import compile_tenant_profile
from app.tenants.loader import load_tenant_config
from app.tools.registry import (
    ActionCapabilityUnavailable,
    BUILTIN_ACTION_REGISTRY,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = PROJECT_ROOT / "examples"


@pytest.fixture
def freight_profile():
    return load_tenant_config(EXAMPLES / "freight-broker.yaml")


def _legacy_profile_snapshot(profile) -> dict[str, object]:
    snapshot = deepcopy(profile.model_dump(mode="json"))
    snapshot.pop("profile_version")
    snapshot.pop("scenario_key")
    snapshot.pop("documents")

    action_policy = snapshot["action_policy"]
    assert isinstance(action_policy, dict)
    for action_rule in action_policy.values():
        assert isinstance(action_rule, dict)
        action_rule.pop("execution")
    return snapshot


def test_compiled_profile_separates_declared_registered_and_available_actions(
    freight_profile,
) -> None:
    compiled = compile_tenant_profile(
        freight_profile,
        profile_fingerprint="a" * 64,
    )

    assert compiled.declared_actions == frozenset(freight_profile.action_policy)
    assert compiled.registered_actions == BUILTIN_ACTION_REGISTRY.keys
    assert "send_reply" in compiled.declared_actions
    assert "send_reply" not in compiled.registered_actions
    assert "send_reply" not in compiled.available_actions
    assert "create_case" in compiled.registered_actions
    assert "create_case" in compiled.available_actions


def test_compiled_profile_resolves_document_binding_and_default(freight_profile) -> None:
    compiled = compile_tenant_profile(
        freight_profile,
        profile_fingerprint="b" * 64,
    )

    binding = compiled.documents["rate_confirmation"]

    assert binding.document_kind == "rate_confirmation"
    assert binding.target_intake_type == "rate_confirmation"
    assert binding.normalizer_key == "bounded_text_pdf"
    assert binding.normalizer_version == 1
    assert binding.capability.version == 1
    assert compiled.default_inbound_document is binding


def test_profile_compilation_rejects_missing_executable_adapter(freight_profile) -> None:
    payload = freight_profile.model_dump(mode="json")
    action_policy = payload["action_policy"]
    assert isinstance(action_policy, dict)
    action_policy["future_action"] = {
        "allowed": True,
        "requires_approval": False,
        "execution": "executable",
    }
    profile = type(freight_profile).model_validate(payload)

    with pytest.raises(ActionCapabilityUnavailable, match="action capability unavailable"):
        compile_tenant_profile(profile, profile_fingerprint="c" * 64)


def test_profile_compilation_rejects_missing_exact_normalizer_version(
    freight_profile,
) -> None:
    payload = freight_profile.model_dump(mode="json")
    documents = payload["documents"]
    assert isinstance(documents, dict)
    binding = documents["rate_confirmation"]
    assert isinstance(binding, dict)
    binding["normalizer_version"] = 2
    profile = type(freight_profile).model_validate(payload)

    with pytest.raises(
        DocumentCapabilityUnavailable,
        match="document capability unavailable",
    ):
        compile_tenant_profile(profile, profile_fingerprint="d" * 64)


def test_legacy_profile_upgrade_derives_scenario_and_preserves_original_hash(
    freight_profile,
) -> None:
    legacy = _legacy_profile_snapshot(freight_profile)
    original = deepcopy(legacy)
    expected_hash = hashlib.sha256(canonical_json_bytes(legacy)).hexdigest()

    upgraded = upgrade_legacy_profile_snapshot(legacy)

    assert legacy == original
    assert upgraded["profile_version"] == 2
    assert upgraded["scenario_key"] == "freight_broker"
    assert upgraded["documents"] == {
        "rate_confirmation": {
            "intake_type": "rate_confirmation",
            "normalizer": "bounded_text_pdf",
            "normalizer_version": 1,
            "default_for_inbound": True,
        }
    }
    upgraded_actions = upgraded["action_policy"]
    assert isinstance(upgraded_actions, dict)
    for action_key in BUILTIN_ACTION_REGISTRY.keys:
        assert upgraded_actions[action_key]["execution"] == "executable"
    assert upgraded_actions["send_reply"]["execution"] == "policy_only"
    assert upgraded_actions["close_case"]["execution"] == "policy_only"

    resolved = TenantProfileResolver(EXAMPLES).resolve_snapshot(
        legacy,
        expected_hash,
        tenant_slug="freight-broker",
    )

    assert resolved.config.scenario_key == "freight_broker"
    assert resolved.snapshot == legacy
    assert resolved.sha256 == expected_hash
    assert resolved.compiled is not None
    assert resolved.compiled.profile_fingerprint == expected_hash
    assert hashlib.sha256(canonical_json_bytes(resolved.snapshot)).hexdigest() == (
        expected_hash
    )
    assert hashlib.sha256(canonical_json_bytes(upgraded)).hexdigest() != expected_hash


def test_persisted_profile_verifies_hash_before_legacy_upgrade(
    freight_profile,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    legacy = _legacy_profile_snapshot(freight_profile)
    expected_hash = hashlib.sha256(canonical_json_bytes(legacy)).hexdigest()
    tampered = deepcopy(legacy)
    tampered["display_name"] = "Tampered profile"
    upgrade_calls: list[object] = []

    def fail_if_called(snapshot: object) -> dict[str, object]:
        upgrade_calls.append(snapshot)
        raise AssertionError("legacy upgrade must not run for a tampered snapshot")

    monkeypatch.setattr(
        "app.runtime.profiles.upgrade_legacy_profile_snapshot",
        fail_if_called,
    )

    with pytest.raises(
        TenantProfileUnavailableError,
        match="tenant profile snapshot hash mismatch",
    ):
        TenantProfileResolver(EXAMPLES).resolve_snapshot(tampered, expected_hash)

    assert upgrade_calls == []
