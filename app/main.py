from fastapi import FastAPI

from app.core.config import settings

app = FastAPI(
    title="IntegraIntake",
    description="Policy-governed intake engine",
    version="0.1.0",
)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "env": settings.app_env}
