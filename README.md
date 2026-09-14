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

### 6. Seed the local demo tenant

```bash
python scripts/seed_demo.py
```

The command creates the `demo` tenant when it does not exist and intentionally
rotates its API key on every run. It prints the new raw key to standard output
once. Copy it into a shell variable or password manager immediately; do not
write it to a file, commit it, or expect it to be shown again. The database
stores its SHA-256 digest and display-safe prefix.

For the request below, set the key only in your current shell session:

```bash
export INTEGRA_DEMO_API_KEY='<printed-key>'
# PowerShell: $env:INTEGRA_DEMO_API_KEY = '<printed-key>'
```

Use the printed value as the `X-API-Key` header when creating a case:

```bash
curl -X POST http://localhost:8000/v1/cases \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $INTEGRA_DEMO_API_KEY" \
  -d '{"channel":"email","subject":"Password reset","body":"Please reset my account password."}'
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
| `POST /v1/cases` | Create a case for the tenant authenticated by `X-API-Key`. |
| `GET /v1/cases/{case_id}` | Read a case visible to the tenant authenticated by `X-API-Key`. |

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://integra:integra@localhost:5432/integra` | Postgres connection |
| `LLM_API_KEY` | — | LLM provider API key |
| `LLM_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible API base |
| `APP_ENV` | `development` | Runtime environment label |
