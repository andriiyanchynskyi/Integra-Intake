"""Compatibility exports for the reusable scenario contracts."""

from .contracts import (
    SAFE_MISMATCH_CODES,
    SAFE_RESULT_KEYS,
    ScenarioCase,
    ScenarioCaseKind,
    ScenarioExpectation,
    ScenarioObservation,
    ScenarioResult,
    compare_scenario_observation,
    safe_result_from_observation,
)

__all__ = [
    "SAFE_MISMATCH_CODES",
    "SAFE_RESULT_KEYS",
    "ScenarioCase",
    "ScenarioCaseKind",
    "ScenarioExpectation",
    "ScenarioObservation",
    "ScenarioResult",
    "compare_scenario_observation",
    "safe_result_from_observation",
]
