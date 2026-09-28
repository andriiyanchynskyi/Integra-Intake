from __future__ import annotations

from collections import Counter
from copy import deepcopy
import json
import re
from pathlib import Path
import shutil

import pytest
from pydantic import ValidationError
import yaml

from app.agent import AgentProposal
from evals.freight.loader import FreightDatasetError, load_freight_dataset
from evals.freight.models import (
    ClaimLevel,
    EvalCaseKind,
    FreightEvalCase,
    FreightEvalCaseResult,
    FreightEvalDataset,
    FreightEvalExpected,
    FreightEvalObservation,
)
from evals.freight.reporting import (
    build_freight_eval_report,
    render_github_summary,
    write_freight_eval_report,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
README_PATH = PROJECT_ROOT / "README.md"
DATASET_PATH = PROJECT_ROOT / "evals" / "datasets" / "freight.v1.yaml"
DOCUMENT_FIXTURES = PROJECT_ROOT / "evals" / "fixtures" / "docs"
MANIFEST_PATH = DOCUMENT_FIXTURES / "manifest.yaml"
WORKFLOW_PATH = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
CASE_ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

CORE_COUNTS = {
    "body_only": 16,
    "document": 12,
    "signed_webhook": 6,
    "multi_request": 4,
    "tenant_idempotency": 2,
}
ADVERSARIAL_COUNTS = {
    "body_override": 4,
    "document_override": 3,
    "webhook_override": 3,
}


def _proposal_payload(*, tool_name: str = "create_case") -> dict[str, object]:
    return {
        "intake_type": "load_request",
        "fields": [
            {
                "name": "origin",
                "value": "Chicago",
                "source_excerpt": "Origin: Chicago",
            }
        ],
        "missing_required_fields": [],
        "priority": "normal",
        "contains_injection_or_override_attempt": False,
        "rationale_short": "Synthetic typed proposal.",
        "tool_calls": [{"name": tool_name, "arguments": []}],
        "confidence": 0.95,
    }


def _case_payload(
    *,
    case_id: str = "smoke-case",
    kind: str = "agent",
    source: dict[str, object] | None = None,
    scripted_proposals: list[dict[str, object]] | None = None,
    expected: dict[str, object] | None = None,
    metadata: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": case_id,
        "category": "body_only",
        "language": "en",
        "kind": kind,
        "source": source
        or {
            "kind": "body",
            "channel": "email",
            "subject": "Synthetic load request",
            "body": "Origin: Chicago; destination: Detroit",
        },
        "scripted_proposals": scripted_proposals
        if scripted_proposals is not None
        else [_proposal_payload()],
        "expected": expected
        or {
            "intake_type": "load_request",
            "missing_required_fields": [],
            "policy_decision": "allow",
            "routing_status": "ready",
            "routing_reason": "action_allowed",
            "tool_name": "create_case",
            "approval_required": False,
            "case_created": True,
        },
        "metadata": metadata
        or {
            "adversarial": False,
            "claim_level": "downstream_from_typed_proposal",
        },
    }


def _minimal_dataset_payload() -> dict[str, object]:
    return {"version": 1, "cases": [_case_payload()]}


def _load_repository_dataset():
    loaded = load_freight_dataset(
        DATASET_PATH,
        document_manifest_path=MANIFEST_PATH,
    )
    return loaded.dataset


def _write_dataset_variant(
    tmp_path: Path,
    payload: dict[str, object],
    *,
    filename: str = "freight-variant.yaml",
) -> Path:
    path = tmp_path / filename
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path


def _copy_document_fixture_tree(tmp_path: Path) -> tuple[Path, Path]:
    fixture_root = tmp_path / "docs"
    shutil.copytree(DOCUMENT_FIXTURES, fixture_root)
    return fixture_root / "manifest.yaml", fixture_root


def test_repository_dataset_has_strict_version_and_exact_counts() -> None:
    dataset = _load_repository_dataset()

    assert isinstance(dataset, FreightEvalDataset)
    assert dataset.version == 1
    assert len(dataset.cases) == 50
    assert sum(not case.metadata.adversarial for case in dataset.cases) == 40
    assert sum(case.metadata.adversarial for case in dataset.cases) == 10

    category_counts = Counter(
        ("adversarial" if case.metadata.adversarial else "core", case.category)
        for case in dataset.cases
    )
    assert {
        category: count
        for (kind, category), count in category_counts.items()
        if kind == "core"
    } == CORE_COUNTS
    assert {
        category: count
        for (kind, category), count in category_counts.items()
        if kind == "adversarial"
    } == ADVERSARIAL_COUNTS


def test_repository_case_ids_are_unique_lowercase_kebab_case() -> None:
    dataset = _load_repository_dataset()
    ids = [case.id for case in dataset.cases]

    assert len(ids) == len(set(ids))
    assert all(CASE_ID_PATTERN.fullmatch(case_id) for case_id in ids)


def test_webhook_and_document_error_contracts_are_explicit() -> None:
    dataset = _load_repository_dataset()

    webhook_cases = [
        case for case in dataset.cases if case.source.kind.value == "inbound_webhook"
    ]
    assert webhook_cases
    assert all(case.expected.signature_verified is True for case in webhook_cases)

    malformed_cases = [
        case
        for case in dataset.cases
        if case.source.kind.value == "document"
        and getattr(case.source, "fixture_id", None) == "malformed-pdf"
    ]
    assert malformed_cases
    assert all(
        case.expected.document_extraction_error == "pdf_malformed"
        for case in malformed_cases
    )


@pytest.mark.parametrize(
    ("section", "extra_key"),
    [
        ("root", "unexpected_root"),
        ("case", "unexpected_case"),
        ("source", "unexpected_source"),
        ("proposal", "unexpected_proposal"),
        ("expected", "unexpected_expected"),
        ("metadata", "unexpected_metadata"),
    ],
)
def test_dataset_models_reject_unknown_keys(section: str, extra_key: str) -> None:
    payload = _minimal_dataset_payload()
    if section == "root":
        payload[extra_key] = "must be rejected"
    elif section == "case":
        payload["cases"][0][extra_key] = "must be rejected"  # type: ignore[index]
    elif section == "source":
        payload["cases"][0]["source"][extra_key] = "must be rejected"  # type: ignore[index]
    elif section == "proposal":
        payload["cases"][0]["scripted_proposals"][0][extra_key] = (  # type: ignore[index]
            "must be rejected"
        )
    elif section == "expected":
        payload["cases"][0]["expected"][extra_key] = "must be rejected"  # type: ignore[index]
    else:
        payload["cases"][0]["metadata"][extra_key] = "must be rejected"  # type: ignore[index]

    with pytest.raises(ValidationError):
        FreightEvalDataset.model_validate(payload)


def test_dataset_version_is_exactly_one() -> None:
    payload = _minimal_dataset_payload()
    payload["version"] = 2

    with pytest.raises(ValidationError):
        FreightEvalDataset.model_validate(payload)


def test_scripted_proposals_are_closed_agent_proposals() -> None:
    invalid_proposal = _proposal_payload()
    invalid_proposal["confidence"] = "0.95"

    with pytest.raises(ValidationError):
        AgentProposal.model_validate(invalid_proposal)

    case_payload = _minimal_dataset_payload()["cases"][0]  # type: ignore[index]
    case_payload["scripted_proposals"] = [invalid_proposal]
    with pytest.raises(ValidationError):
        FreightEvalCase.model_validate(case_payload)


def test_expected_missing_fields_are_sorted_and_immutable() -> None:
    payload = _case_payload()
    payload["expected"]["missing_required_fields"] = [  # type: ignore[index]
        "valid_until",
        "equipment",
    ]

    case = FreightEvalCase.model_validate(payload)
    assert case.expected.missing_required_fields == ("equipment", "valid_until")
    with pytest.raises((TypeError, ValidationError)):
        case.expected.missing_required_fields += ("commodity",)  # type: ignore[misc]


def test_expected_missing_fields_reject_duplicates() -> None:
    payload = _case_payload()
    payload["expected"]["missing_required_fields"] = [  # type: ignore[index]
        "equipment",
        "equipment",
    ]

    with pytest.raises(ValidationError):
        FreightEvalCase.model_validate(payload)


def test_claim_level_matches_case_kind() -> None:
    dataset = _load_repository_dataset()

    for case in dataset.cases:
        if case.kind is EvalCaseKind.AGENT:
            assert (
                case.metadata.claim_level
                is ClaimLevel.DOWNSTREAM_FROM_TYPED_PROPOSAL
            )
        else:
            assert (
                case.metadata.claim_level
                is ClaimLevel.DETERMINISTIC_SERVER_INVARIANT
            )


@pytest.mark.parametrize("bad_fixture_id", ["does-not-exist", "../complete-en", "/tmp/complete-en"])
def test_loader_rejects_undeclared_or_unsafe_document_fixture_ids(
    tmp_path: Path,
    bad_fixture_id: str,
) -> None:
    payload = yaml.safe_load(DATASET_PATH.read_text(encoding="utf-8"))
    document_case = next(
        case
        for case in payload["cases"]
        if case["source"]["kind"] == "document"
    )
    document_case["source"]["fixture_id"] = bad_fixture_id
    dataset_path = _write_dataset_variant(tmp_path, payload)

    with pytest.raises(FreightDatasetError) as error:
        load_freight_dataset(dataset_path, document_manifest_path=MANIFEST_PATH)

    message = str(error.value)
    assert dataset_path.name in message
    assert "invalid_document_reference" in message
    assert bad_fixture_id not in message


@pytest.mark.parametrize("unsafe_filename", ["../outside.txt", "C:\\outside.txt"])
def test_loader_rejects_manifest_paths_outside_fixture_directory(
    tmp_path: Path,
    unsafe_filename: str,
) -> None:
    manifest_path, fixture_root = _copy_document_fixture_tree(tmp_path)
    manifest_payload = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    manifest_payload["documents"][0]["filename"] = unsafe_filename
    manifest_path.write_text(
        yaml.safe_dump(manifest_payload, sort_keys=False),
        encoding="utf-8",
    )

    with pytest.raises(FreightDatasetError) as error:
        load_freight_dataset(DATASET_PATH, document_manifest_path=manifest_path)

    message = str(error.value)
    assert DATASET_PATH.name in message
    assert "invalid_document_reference" in message
    assert unsafe_filename not in message
    assert fixture_root.name not in message


def test_loader_error_is_safe_and_identifies_dataset_and_case(tmp_path: Path) -> None:
    payload = yaml.safe_load(DATASET_PATH.read_text(encoding="utf-8"))
    duplicate_id = payload["cases"][0]["id"]
    payload["cases"][1]["id"] = duplicate_id
    payload["cases"][1]["source"]["body"] = "SECRET_SOURCE_BODY"  # type: ignore[index]
    dataset_path = _write_dataset_variant(tmp_path, payload)

    with pytest.raises(FreightDatasetError) as error:
        load_freight_dataset(dataset_path, document_manifest_path=MANIFEST_PATH)

    message = str(error.value)
    assert dataset_path.name in message
    assert "duplicate_case_id" in message
    assert duplicate_id in message
    assert "SECRET_SOURCE_BODY" not in message


def test_minimal_valid_yaml_loads_into_frozen_models() -> None:
    payload = yaml.safe_load(
        yaml.safe_dump(_minimal_dataset_payload(), sort_keys=False)
    )
    dataset = FreightEvalDataset.model_validate(payload)

    assert dataset.version == 1
    assert isinstance(dataset.cases, tuple)
    with pytest.raises(ValidationError):
        dataset.version = 2  # type: ignore[misc]
    with pytest.raises((TypeError, ValidationError)):
        dataset.cases += (dataset.cases[0],)  # type: ignore[misc]


def test_case_model_rejects_wrong_claim_level_for_server_invariant() -> None:
    payload = _case_payload(
        kind="webhook_idempotency",
        scripted_proposals=[],
        expected={"duplicate_reused": True, "conflict_raised": False},
        metadata={
            "adversarial": False,
            "claim_level": "downstream_from_typed_proposal",
        },
    )

    with pytest.raises(ValidationError):
        FreightEvalCase.model_validate(payload)


def test_case_model_rejects_missing_scripted_proposals_for_agent() -> None:
    payload = _case_payload(scripted_proposals=[])

    with pytest.raises(ValidationError):
        FreightEvalCase.model_validate(payload)


def test_case_payload_helper_is_not_mutated_by_validation() -> None:
    payload = _minimal_dataset_payload()
    original = deepcopy(payload)

    FreightEvalDataset.model_validate(payload)

    assert payload == original


def _report_case(
    *,
    case_id: str,
    category: str,
    passed: bool,
    mismatches: tuple[str, ...] = (),
) -> FreightEvalCaseResult:
    return FreightEvalCaseResult(
        id=case_id,
        category=category,
        passed=passed,
        observation=FreightEvalObservation(
            intake_type="SOURCE_BODY_SHOULD_NOT_BE_SERIALIZED",
            policy_decision="SOURCE_POLICY_SHOULD_NOT_BE_SERIALIZED",
            tool_name="SOURCE_TOOL_SHOULD_NOT_BE_SERIALIZED",
            provider_calls=0,
            llm_calls=1,
        ),
        expected=FreightEvalExpected(
            intake_type="EXPECTED_RATIONALE_SHOULD_NOT_BE_SERIALIZED",
            policy_decision="EXPECTED_RATIONALE_SHOULD_NOT_BE_SERIALIZED",
        ),
        mismatches=mismatches,
    )


def test_freight_report_has_exact_safe_top_level_and_case_shape(
    tmp_path: Path,
) -> None:
    results = (
        _report_case(
            case_id="z-case",
            category="body_only",
            passed=True,
        ),
        _report_case(
            case_id="a-case",
            category="document",
            passed=False,
            mismatches=("tool_name_mismatch", "policy_decision_mismatch"),
        ),
    )

    report = build_freight_eval_report(
        results,
        allowed_case_ids={"a-case", "z-case"},
        commit_sha="abc1234",
    )
    report_path = tmp_path / "freight-report.json"
    write_freight_eval_report(report, report_path)
    payload = json.loads(report_path.read_text(encoding="utf-8"))

    assert set(payload) == {
        "dataset_version",
        "runner_version",
        "commit_sha",
        "total",
        "passed",
        "failed",
        "pass_rate",
        "categories",
        "cases",
    }
    assert payload["total"] == 2
    assert payload["passed"] == 1
    assert payload["failed"] == 1
    assert payload["pass_rate"] == 0.5
    assert all(
        set(case) == {"id", "category", "passed", "mismatches"}
        for case in payload["cases"]
    )
    assert payload["cases"] == [
        {
            "category": "document",
            "id": "a-case",
            "mismatches": ["policy_decision_mismatch", "tool_name_mismatch"],
            "passed": False,
        },
        {
            "category": "body_only",
            "id": "z-case",
            "mismatches": [],
            "passed": True,
        },
    ]


def test_freight_report_is_deterministically_sorted() -> None:
    results = (
        _report_case(
            case_id="z-case",
            category="body_only",
            passed=False,
            mismatches=("tool_name_mismatch", "policy_decision_mismatch"),
        ),
        _report_case(
            case_id="a-case",
            category="document",
            passed=True,
        ),
    )

    report = build_freight_eval_report(
        results,
        allowed_case_ids={"a-case", "z-case"},
    )

    assert tuple(category.value for category in report.categories) == (
        "body_only",
        "document",
    )
    assert tuple(case.id for case in report.cases) == ("a-case", "z-case")
    assert report.cases[-1].mismatches == (
        "policy_decision_mismatch",
        "tool_name_mismatch",
    )


def test_freight_report_redacts_source_proposal_and_secret_sentinels() -> None:
    result = _report_case(
        case_id="secret-case",
        category="body_only",
        passed=False,
        mismatches=("policy_decision_mismatch",),
    )
    report = build_freight_eval_report(
        (result,),
        allowed_case_ids={"secret-case"},
    )
    report_json = json.dumps(report.model_dump(mode="json"), ensure_ascii=False)
    summary = render_github_summary(report)
    failure = result.safe_failure_message()

    for text in (report_json, summary, repr(report), failure):
        assert "SOURCE_BODY_SHOULD_NOT_BE_SERIALIZED" not in text
        assert "SOURCE_POLICY_SHOULD_NOT_BE_SERIALIZED" not in text
        assert "SOURCE_TOOL_SHOULD_NOT_BE_SERIALIZED" not in text
        assert "EXPECTED_RATIONALE_SHOULD_NOT_BE_SERIALIZED" not in text
    assert "secret-case" in summary
    assert "policy_decision_mismatch" in summary


def test_freight_report_requires_at_least_one_result() -> None:
    with pytest.raises(ValueError):
        build_freight_eval_report((), allowed_case_ids=set())


def test_freight_report_rejects_values_outside_safe_allowlists() -> None:
    result = _report_case(
        case_id="secret-case",
        category="body_only",
        passed=False,
        mismatches=("secret-source-text",),
    )

    with pytest.raises(ValueError, match="mismatch code"):
        build_freight_eval_report(
            (result,),
            allowed_case_ids={"secret-case"},
        )

    with pytest.raises(ValueError, match="case ID"):
        build_freight_eval_report(
            (result,),
            allowed_case_ids={"different-case"},
        )


def test_readme_documents_the_deterministic_freight_eval_contract() -> None:
    readme = README_PATH.read_text(encoding="utf-8")
    lowered = readme.lower()
    normalized = " ".join(lowered.split())

    assert "pytest evals/test_freight.py -q" in normalized
    assert "40 core and 10 adversarial cases" in normalized
    assert "k=1" in normalized
    assert "100% pass rate" in normalized
    assert "scripted typed `agentproposal` values" in normalized
    assert "does not measure live-model extraction, classification, or injection-detection quality" in normalized

    forbidden_context = (
        r"\bphase(?:s)?\b",
        r"\broadmap\b",
        r"\binterview\b",
        r"\bvacancy\b",
        r"\blearning\b",
        r"\btuturial\b",
        r"\btutorial\b",
        r"\btraining(?:[-\s]context)?\b",
    )
    for pattern in forbidden_context:
        assert re.search(pattern, lowered) is None, pattern


def _load_ci_workflow() -> tuple[dict[str, object], str]:
    assert WORKFLOW_PATH.is_file(), f"missing CI workflow: {WORKFLOW_PATH}"
    source = WORKFLOW_PATH.read_text(encoding="utf-8")
    # BaseLoader keeps the GitHub Actions `on` key as a string.  The default
    # YAML 1.1 resolver would otherwise coerce it to the boolean True.
    workflow = yaml.load(source, Loader=yaml.BaseLoader)
    assert isinstance(workflow, dict)
    return workflow, source


def _ci_steps(workflow: dict[str, object]) -> list[dict[str, object]]:
    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    test_job = jobs["test"]
    assert isinstance(test_job, dict)
    steps = test_job["steps"]
    assert isinstance(steps, list)
    assert all(isinstance(step, dict) for step in steps)
    return steps


def _ci_step(workflow: dict[str, object], name: str) -> dict[str, object]:
    for step in _ci_steps(workflow):
        if step.get("name") == name:
            return step
    raise AssertionError(f"CI step not found: {name}")


def test_ci_workflow_triggers_and_python_version_are_explicit() -> None:
    workflow, _ = _load_ci_workflow()

    triggers = workflow["on"]
    assert isinstance(triggers, dict)
    assert set(triggers) == {"pull_request", "push", "workflow_dispatch"}
    assert triggers["push"] == {"branches": ["main"]}
    assert workflow["permissions"] == {"contents": "read"}

    jobs = workflow["jobs"]
    assert isinstance(jobs, dict)
    assert set(jobs) == {"test"}
    test_job = jobs["test"]
    assert isinstance(test_job, dict)
    assert test_job["runs-on"] == "ubuntu-latest"

    setup = _ci_step(workflow, "Set up Python")
    assert setup["uses"] == "actions/setup-python@v7"
    setup_with = setup["with"]
    assert isinstance(setup_with, dict)
    assert setup_with["python-version"] == "3.12"

    checkout = _ci_step(workflow, "Check out repository")
    checkout_with = checkout["with"]
    assert isinstance(checkout_with, dict)
    assert checkout_with["fetch-depth"] == "0"


def test_ci_workflow_runs_install_whitespace_unit_and_eval_checks() -> None:
    workflow, _ = _load_ci_workflow()

    install = _ci_step(workflow, "Install package")
    assert install["run"] == 'python -m pip install -e ".[dev]"'

    whitespace = _ci_step(workflow, "Check whitespace")
    assert "git diff --check" in str(whitespace["run"])
    assert "github.event.pull_request.base.sha" in str(whitespace["run"])

    unit = _ci_step(workflow, "Run unit and API tests")
    assert unit["run"] == "pytest -q"

    evals = _ci_step(workflow, "Run deterministic freight evals")
    eval_command = str(evals["run"])
    assert "pytest evals/test_freight.py -q" in eval_command
    assert "--freight-eval-report" in eval_command
    assert "${{ runner.temp }}/freight-evals.json" in eval_command


def test_ci_workflow_always_writes_and_uploads_only_the_eval_report() -> None:
    workflow, _ = _load_ci_workflow()

    summary = _ci_step(workflow, "Write freight eval summary")
    assert summary["if"] == "always()"
    assert "python -m evals.freight.reporting" in str(summary["run"])
    assert '"${{ runner.temp }}/freight-evals.json"' in str(summary["run"])
    assert '"$GITHUB_STEP_SUMMARY"' in str(summary["run"])

    artifact = _ci_step(workflow, "Upload freight eval report")
    assert artifact["if"] == "always()"
    assert artifact["uses"] == "actions/upload-artifact@v7"
    artifact_with = artifact["with"]
    assert isinstance(artifact_with, dict)
    assert artifact_with["path"] == "${{ runner.temp }}/freight-evals.json"
    assert artifact_with["if-no-files-found"] == "ignore"
    assert "*" not in str(artifact_with["path"])


def test_ci_workflow_has_no_secrets_provider_network_database_or_schedule() -> None:
    _, source = _load_ci_workflow()
    lowered = source.lower()

    assert not re.search(r"\$\{\{\s*secrets\.", source, flags=re.IGNORECASE)
    forbidden_fragments = (
        "llm_api_key",
        "database_url",
        "api_key",
        "services:",
        "schedule:",
        "workflow_run:",
        "curl ",
        "httpx",
        "openai",
        "docker-compose",
        "deploy",
    )
    for fragment in forbidden_fragments:
        assert fragment not in lowered, fragment
