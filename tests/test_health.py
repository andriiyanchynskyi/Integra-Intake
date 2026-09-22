import inspect

from fastapi.testclient import TestClient

import app.main as main_module
from app.main import app

client = TestClient(app)


def test_health_returns_ok() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "env" in body


def test_app_assembly_keeps_api_routes_separate_from_worker_and_provider_startup() -> None:
    """Importing FastAPI must not claim jobs or construct an LLM runtime."""
    route_paths = {route.path for route in app.routes}
    assert "/v1/cases" in route_paths
    assert "/v1/cases/{case_id}" in route_paths
    assert "/v1/intake" in route_paths

    source = inspect.getsource(main_module)
    assert "app.workers" not in source
    assert "AgentRuntimeFactory" not in source
    assert "OpenAICompatibleLLMClient" not in source
