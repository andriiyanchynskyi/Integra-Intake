"""Sanitized local and GitHub summaries for deterministic freight evals."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Collection, Sequence
from pathlib import Path
import re

from .models import (
    DATASET_VERSION,
    REPORT_CATEGORY_VALUES,
    REPORT_MISMATCH_VALUES,
    RUNNER_VERSION,
    FreightEvalCaseResult,
    FreightEvalReport,
    FreightEvalReportCase,
    ReportCategory,
    ReportMismatchCode,
)


_CASE_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_COMMIT_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")


def build_freight_eval_report(
    results: Sequence[FreightEvalCaseResult],
    *,
    allowed_case_ids: Collection[str],
    dataset_version: int = DATASET_VERSION,
    commit_sha: str | None = None,
) -> FreightEvalReport:
    """Project detailed results into a source-free closed report.

    The caller must provide the case IDs from the loaded dataset. This keeps
    the report boundary fail-closed even when handed a handcrafted result.
    """

    if not results:
        raise ValueError("at least one freight eval result is required")
    allowed_ids = frozenset(allowed_case_ids)
    if not allowed_ids or any(
        not isinstance(case_id, str) or not _CASE_ID_PATTERN.fullmatch(case_id)
        for case_id in allowed_ids
    ):
        raise ValueError("report case ID allowlist is invalid")
    if any(item.id not in allowed_ids for item in results):
        raise ValueError("report contains a case ID outside the dataset allowlist")
    if len({item.id for item in results}) != len(results):
        raise ValueError("report contains duplicate case IDs")
    if any(item.category not in REPORT_CATEGORY_VALUES for item in results):
        raise ValueError("report contains a category outside the allowlist")
    if any(
        mismatch not in REPORT_MISMATCH_VALUES
        for item in results
        for mismatch in item.mismatches
    ):
        raise ValueError("report contains a mismatch code outside the allowlist")
    if commit_sha is not None and not _COMMIT_SHA_PATTERN.fullmatch(commit_sha):
        raise ValueError("report commit SHA is invalid")
    ordered = tuple(sorted(results, key=lambda item: item.id))
    cases = tuple(
        FreightEvalReportCase(
            id=item.id,
            category=ReportCategory(item.category),
            passed=item.passed,
            mismatches=tuple(ReportMismatchCode(code) for code in item.mismatches),
        )
        for item in ordered
    )
    passed = sum(item.passed for item in ordered)
    total = len(ordered)
    category_counts = Counter(ReportCategory(item.category) for item in ordered)
    categories = dict(
        sorted(category_counts.items(), key=lambda item: item[0].value)
    )
    return FreightEvalReport(
        dataset_version=dataset_version,
        runner_version=RUNNER_VERSION,
        commit_sha=commit_sha,
        total=total,
        passed=passed,
        failed=total - passed,
        pass_rate=float(passed / total),
        categories=categories,
        cases=cases,
    )


def write_freight_eval_report(report: FreightEvalReport, path: Path | str) -> None:
    """Write only the closed report projection as deterministic UTF-8 JSON."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    )
    target.write_text(payload + "\n", encoding="utf-8")


def render_github_summary(report: FreightEvalReport) -> str:
    """Render counts and safe case IDs without source or proposal content."""

    lines = [
        "## Deterministic freight evals",
        "",
        f"- Cases: {report.total}",
        f"- Passed: {report.passed}",
        f"- Failed: {report.failed}",
        f"- Pass rate: {report.pass_rate:.1%}",
        "",
        "### Category counts",
        "",
    ]
    for category, count in sorted(
        report.categories.items(), key=lambda item: item[0].value
    ):
        lines.append(f"- `{category.value}`: {count}")
    failed = [case for case in report.cases if not case.passed]
    if failed:
        lines.extend(["", "### Failed cases", ""])
        for case in failed:
            mismatch_text = ", ".join(case.mismatches) or "unspecified_mismatch"
            lines.append(f"- `{case.id}`: {mismatch_text}")
    return "\n".join(lines) + "\n"


def _read_report(path: Path) -> FreightEvalReport:
    try:
        return FreightEvalReport.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as error:
        raise ValueError("freight eval report is invalid") from error


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--github-summary", type=Path)
    args = parser.parse_args(argv)
    if not args.report.exists():
        summary = "Freight eval report was not produced.\n"
    else:
        try:
            summary = render_github_summary(_read_report(args.report))
        except ValueError:
            summary = "Freight eval report was not produced.\n"
    if args.github_summary is not None:
        args.github_summary.parent.mkdir(parents=True, exist_ok=True)
        with args.github_summary.open("a", encoding="utf-8") as output:
            output.write(summary)
    else:
        print(summary, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "build_freight_eval_report",
    "main",
    "render_github_summary",
    "write_freight_eval_report",
]
