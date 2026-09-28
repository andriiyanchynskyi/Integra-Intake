from __future__ import annotations

from pathlib import Path

import pytest

from app.tenants.loader import load_tenant_config
from evals.freight.loader import load_freight_dataset
from evals.freight.models import FreightEvalCase
from evals.freight.runner import run_freight_eval_case

from .conftest import FreightEvalRecorder


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATASET = load_freight_dataset(
    PROJECT_ROOT / "evals" / "datasets" / "freight.v1.yaml",
    document_manifest_path=PROJECT_ROOT / "evals" / "fixtures" / "docs" / "manifest.yaml",
)
FREIGHT_CONFIG = load_tenant_config(PROJECT_ROOT / "examples" / "freight-broker.yaml")
DOCUMENT_FIXTURES = DATASET.document_fixtures


@pytest.mark.asyncio
@pytest.mark.parametrize("case", DATASET.cases, ids=lambda item: item.id)
async def test_freight_behavior(
    case: FreightEvalCase,
    freight_eval_recorder: FreightEvalRecorder,
) -> None:
    result = await run_freight_eval_case(
        case,
        tenant_config=FREIGHT_CONFIG,
        document_fixtures=DOCUMENT_FIXTURES,
    )
    freight_eval_recorder.record(result)
    assert result.passed, result.safe_failure_message()
