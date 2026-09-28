"""Pytest integration for optional sanitized freight reports."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from .freight.models import FreightEvalCaseResult
from .freight.loader import load_freight_dataset
from .freight.reporting import build_freight_eval_report, write_freight_eval_report


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET = load_freight_dataset(
    PROJECT_ROOT / "evals" / "datasets" / "freight.v1.yaml",
    document_manifest_path=PROJECT_ROOT / "evals" / "fixtures" / "docs" / "manifest.yaml",
)


class FreightEvalRecorder:
    """Session-owned collection of safe per-case results."""

    def __init__(self, allowed_case_ids: frozenset[str]) -> None:
        self._results: list[FreightEvalCaseResult] = []
        self._allowed_case_ids = allowed_case_ids

    def record(self, result: FreightEvalCaseResult) -> None:
        if any(item.id == result.id for item in self._results):
            raise ValueError("duplicate freight eval case result")
        self._results.append(result)

    @property
    def results(self) -> tuple[FreightEvalCaseResult, ...]:
        return tuple(self._results)

    @property
    def allowed_case_ids(self) -> frozenset[str]:
        return self._allowed_case_ids


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--freight-eval-report",
        action="store",
        default=None,
        help="Write a sanitized freight eval JSON report to PATH.",
    )


@pytest.fixture(scope="session")
def freight_eval_recorder(request: pytest.FixtureRequest) -> FreightEvalRecorder:
    recorder = FreightEvalRecorder(frozenset(case.id for case in DATASET.cases))
    setattr(request.config, "_freight_eval_recorder", recorder)
    return recorder


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    del exitstatus
    report_path = session.config.getoption("--freight-eval-report")
    recorder = getattr(session.config, "_freight_eval_recorder", None)
    if not report_path or recorder is None or not recorder.results:
        return
    commit_sha = os.environ.get("GITHUB_SHA")
    if commit_sha is not None and not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit_sha):
        commit_sha = None
    report = build_freight_eval_report(
        recorder.results,
        allowed_case_ids=recorder.allowed_case_ids,
        commit_sha=commit_sha,
    )
    write_freight_eval_report(report, Path(report_path))


__all__ = ["FreightEvalRecorder"]
