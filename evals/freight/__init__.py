"""Deterministic freight evaluation contracts and execution helpers."""

from .models import (
    ADVERSARIAL_COUNTS,
    CORE_COUNTS,
    DATASET_VERSION,
    RUNNER_VERSION,
    ClaimLevel,
    EvalCaseKind,
    EvalSourceKind,
    FreightBodySource,
    FreightDocumentSource,
    FreightEvalCase,
    FreightEvalCaseResult,
    FreightEvalDataset,
    FreightEvalExpected,
    FreightEvalMetadata,
    FreightEvalObservation,
    FreightEvalReport,
    FreightEvalReportCase,
    FreightEvalSource,
    FreightWebhookSource,
)


def __getattr__(name: str):
    if name in {
        "FreightEvalExecutionError",
        "RecordingPolicy",
        "ScriptedLLM",
        "run_freight_eval_case",
    }:
        from . import runner

        return getattr(runner, name)
    if name in {
        "build_freight_eval_report",
        "render_github_summary",
        "write_freight_eval_report",
    }:
        from . import reporting

        return getattr(reporting, name)
    raise AttributeError(name)

__all__ = [
    "ADVERSARIAL_COUNTS",
    "CORE_COUNTS",
    "DATASET_VERSION",
    "RUNNER_VERSION",
    "ClaimLevel",
    "EvalCaseKind",
    "EvalSourceKind",
    "FreightBodySource",
    "FreightDocumentSource",
    "FreightEvalCase",
    "FreightEvalCaseResult",
    "FreightEvalDataset",
    "FreightEvalExpected",
    "FreightEvalMetadata",
    "FreightEvalObservation",
    "FreightEvalReport",
    "FreightEvalReportCase",
    "FreightEvalSource",
    "FreightWebhookSource",
    "FreightEvalExecutionError",
    "RecordingPolicy",
    "ScriptedLLM",
    "run_freight_eval_case",
    "build_freight_eval_report",
    "render_github_summary",
    "write_freight_eval_report",
]
