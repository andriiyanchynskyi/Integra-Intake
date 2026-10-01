from fastapi import FastAPI

from app.api.approvals import router as approvals_router
from app.api.body_limit import RequestBodyLimitMiddleware
from app.api.cases import router as cases_router
from app.api.inbound import router as inbound_router
from app.api.intake import router as intake_router
from app.api.jobs import router as jobs_router
from app.core.config import settings
from app.observability import StructlogObserver
from app.observability.logging import configure_json_logging
from app.observability.middleware import ObservabilityMiddleware


configure_json_logging()

app = FastAPI(
    title="IntegraIntake",
    description="Policy-governed intake engine",
    version="0.1.0",
)
app.state.observer = StructlogObserver()
app.add_middleware(RequestBodyLimitMiddleware)
app.add_middleware(ObservabilityMiddleware, observer=app.state.observer)

app.include_router(cases_router, prefix="/v1")
app.include_router(intake_router, prefix="/v1")
app.include_router(inbound_router, prefix="/v1")
app.include_router(approvals_router, prefix="/v1")
app.include_router(jobs_router, prefix="/v1")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "env": settings.app_env}
