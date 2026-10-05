from __future__ import annotations

from pathlib import Path

import yaml


def test_compose_passes_optional_provider_settings_only_to_worker() -> None:
    root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text(encoding="utf-8"))
    worker_environment = compose["services"]["worker"]["environment"]
    api_environment = compose["services"]["api"]["environment"]

    assert worker_environment["LLM_API_KEY"] == "${LLM_API_KEY:-}"
    assert worker_environment["LLM_BASE_URL"] == "${LLM_BASE_URL:-https://api.openai.com/v1}"
    assert worker_environment["OPENAI_MODEL"] == "${OPENAI_MODEL:-gpt-5.4-mini-2026-03-17}"
    assert worker_environment["LLM_REASONING_EFFORT"] == "${LLM_REASONING_EFFORT:-}"
    assert "LLM_API_KEY" not in api_environment
    assert "LLM_BASE_URL" not in api_environment
    assert "OPENAI_MODEL" not in api_environment
    assert "LLM_REASONING_EFFORT" not in api_environment
    assert "WORKER_CONCURRENCY" not in worker_environment
