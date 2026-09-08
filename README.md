# IntegraIntake

Policy-governed AI intake engine.

Turns unstructured inbound messages and documents into validated, auditable cases — with retries, idempotency, human approval, evals, and production traces. Tenant-specific workflows are defined via YAML configuration (`examples/`).

## Stack

Python 3.12+, FastAPI, PostgreSQL, SQLAlchemy 2 (async), Alembic, Pydantic v2, Docker Compose, pytest, structlog.

## Quick start

### 1. Environment

```bash
cp .env.example .env
```

### 2. Database

```bash
docker compose up -d db
```

### 3. Local API

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate

pip install -e ".[dev]"
uvicorn app.main:app --reload
```

### 4. Health check

```bash
curl http://localhost:8000/health
# {"status":"ok","env":"development"}
```

### 5. Migrations

```bash
alembic upgrade head
```

### Full stack (API + DB in Docker)

```bash
docker compose up --build
```

## API

| Endpoint | Description |
|---|---|
| `GET /health` | Liveness probe |
| `GET /docs` | OpenAPI (Swagger UI) |

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://integra:integra@localhost:5432/integra` | Postgres connection |
| `LLM_API_KEY` | — | LLM provider API key |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible API base |
| `APP_ENV` | `development` | Runtime environment label |
