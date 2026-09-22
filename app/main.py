from fastapi import FastAPI

from app.api.cases import router as cases_router
from app.api.intake import router as intake_router
from app.core.config import settings

app = FastAPI(
    title="IntegraIntake",
    description="Policy-governed intake engine",
    version="0.1.0",
)

app.include_router(cases_router, prefix="/v1")
app.include_router(intake_router, prefix="/v1")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "env": settings.app_env}
