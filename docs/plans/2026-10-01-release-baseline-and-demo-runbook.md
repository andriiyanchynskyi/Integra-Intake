# Implementation plan: Release database baseline and demo runbook

> **For agents:** Use the executing-plans skill to implement this plan task by task. Steps use checkbox syntax (`- [ ]`) for tracking.

**Purpose:** Replace the pre-release Alembic chain with one final-schema baseline and provide a PowerShell runbook that reproduces each supported live demo profile with either worker launch mode.

**Architecture:** `001_initial_schema` creates the current ORM schema without transitional operations. Docker Compose owns the canonical demo topology, while Alembic remains an explicit one-shot command. The worker alone receives provider settings. Tenant profiles remain checked-in YAML compiled against server-owned action and document registries.

**Key files/stack:** Alembic, PostgreSQL 16, SQLAlchemy 2 async, Docker Compose, PowerShell, Pydantic tenant profiles, pytest.

**TDD constraint:** Write and run tests in the primary agent context. Do not use a subagent for test design, implementation, or revision.

**Design source:** `docs/specs/2026-10-01-release-baseline-and-demo-runbook-design.md`

---

## File map

**Create**

- `alembic/versions/001_initial_schema.py`: sole release revision and final schema owner.

**Delete**

- `alembic/versions/001_bootstrap.py`
- `alembic/versions/002_domain_and_tenancy.py`
- `alembic/versions/003_phase7_worker_runtime.py`
- `alembic/versions/004_phase8_approval_workflow.py`
- `alembic/versions/005_phase8_tenant_expiry_index.py`
- `alembic/versions/006_phase8_approval_status_check.py`
- `alembic/versions/007_phase12_job_trace_id.py`

**Edit**

- `app/db/models.py`: rename the approval payload constraint.
- `scripts/seed_demo.py`: expose one profile-choice constant and use it in argparse.
- `tests/test_migrations.py`: replace development-chain assertions with baseline and final-schema assertions.
- `tests/test_seed.py`: verify YAML files, seed choices, and smoke scenarios use the same slugs.
- `.github/workflows/ci.yml`: exercise upgrade, downgrade, and second upgrade against CI PostgreSQL.
- `tests/test_observability.py`: require the reproducible runbook commands and alternative-profile sections.
- `docs/runbooks/live-provider-demo.md`: write the canonical setup, worker alternatives, profile differences, approval, troubleshooting, and cleanup instructions.
- `README.md`: align quick start and migration policy with the release baseline.
- `app/workers/__main__.py`: remove the stale phase label from the runtime entry-point docstring.
- `app/skills/definitions.py`: remove the stale phase label from the current skill-definition docstring.
- `tests/test_agent_worker.py`: remove the stale phase label from the module docstring.

The three files under `examples/` require validation but no content change unless a failing contract proves a mismatch.

### Task 1: Lock the release-baseline contract in tests

**Files:**

- Edit: `tests/test_migrations.py`
- Test: `tests/test_migrations.py`

- [ ] **Step 1: Replace development-phase constants with final-schema constants**

Use these names at the top of `tests/test_migrations.py`:

```python
FINAL_SCHEMA_TABLES = {
    "tenants",
    "api_keys",
    "customers",
    "intake_cases",
    "case_events",
    "agent_jobs",
    "idempotency_records",
    "approvals",
    "approval_events",
}
PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSIONS_DIRECTORY = PROJECT_ROOT / "alembic" / "versions"
INITIAL_SCHEMA_MIGRATION_PATH = VERSIONS_DIRECTORY / "001_initial_schema.py"
TENANT_SCOPED_TABLES = FINAL_SCHEMA_TABLES - {"tenants"}
TIMESTAMPED_TABLES = FINAL_SCHEMA_TABLES - {"case_events", "approval_events"}
```

Keep `JSONB_COLUMNS` unchanged. Replace every use of `PHASE_EIGHT_TABLES` with
`FINAL_SCHEMA_TABLES`. Remove `PHASE_TWO_TABLES`, `PHASE_SEVEN_TABLES`,
`APPROVAL_WORKFLOW_MIGRATION_PATH`, `APPROVAL_STATUS_MIGRATION_PATH`, and
`TRACE_MIGRATION_PATH`.

- [ ] **Step 2: Add the failing single-baseline source test**

Replace the old migration-owner and trace-backfill source tests with:

```python
def test_release_uses_single_initial_schema_revision() -> None:
    revision_files = sorted(
        path.name
        for path in VERSIONS_DIRECTORY.glob("*.py")
        if path.name != "__init__.py"
    )

    assert revision_files == ["001_initial_schema.py"]
    source = INITIAL_SCHEMA_MIGRATION_PATH.read_text(encoding="utf-8")
    assert 'revision: str = "001_initial_schema"' in source
    assert "down_revision: Union[str, None] = None" in source
    assert "phase" not in source.lower()
    assert source.count('"ck_approvals_status"') == 1
    assert source.count('"ck_approvals_payload_complete"') == 1
    assert "ck_approvals_phase8_payload_complete" not in source
```

Change the ORM metadata assertion to require:

```python
assert any(
    isinstance(constraint, CheckConstraint)
    and constraint.name == "ck_approvals_payload_complete"
    for constraint in approvals.constraints
)
```

Rename the PostgreSQL test to:

```python
async def test_initial_schema_creates_final_tenant_domain_schema(
    postgres_connection: AsyncConnection,
) -> None:
    """The release baseline creates the final schema and tenant boundaries."""
    await query_schema_contract(postgres_connection)
    await assert_tenant_boundaries(postgres_connection)
```

Add both approval checks to `query_schema_contract`:

```python
assert "ck_approvals_payload_complete" in constraint_map
assert "ck_approvals_status" in constraint_map
assert "ck_approvals_phase8_payload_complete" not in constraint_map
```

- [ ] **Step 3: Run the focused tests and confirm the expected failure**

Run:

```powershell
pytest tests/test_migrations.py -q
```

Expected: the non-PostgreSQL baseline tests fail because the old seven revision files still exist and the ORM still declares `ck_approvals_phase8_payload_complete`. PostgreSQL tests may skip when `DATABASE_URL` is absent.

### Task 2: Create the final-schema Alembic baseline

**Files:**

- Create: `alembic/versions/001_initial_schema.py`
- Delete: the seven old files listed in the file map
- Edit: `app/db/models.py`
- Test: `tests/test_migrations.py`

- [ ] **Step 1: Rename the ORM approval constraint**

In `Approval.__table_args__`, change only the name:

```python
CheckConstraint(
    "job_id IS NULL OR (pending_action IS NOT NULL "
    "AND tenant_config_sha256 IS NOT NULL AND expires_at IS NOT NULL)",
    name="ck_approvals_payload_complete",
),
```

- [ ] **Step 2: Create the baseline metadata and helpers**

Start `alembic/versions/001_initial_schema.py` with:

```python
"""Create the IntegraIntake release schema."""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "001_initial_schema"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def uuid_column() -> sa.Column:
    return sa.Column(
        "id",
        postgresql.UUID(as_uuid=True),
        primary_key=True,
        nullable=False,
    )


def timestamps() -> list[sa.Column]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]
```

- [ ] **Step 3: Define the final schema directly**

Implement `upgrade()` in this dependency order:

```text
tenants
api_keys
customers
intake_cases
case_events
agent_jobs
idempotency_records
approvals
approval_events
```

Use this exact final-schema matrix. Every row represents one column declaration;
add the named constraints and indexes shown below.

| Table | Columns beyond `id` | Constraints and indexes |
| --- | --- | --- |
| `tenants` | `slug String(255) NOT NULL UNIQUE`; `name String(255) NOT NULL`; `status String(50) DEFAULT 'active' NOT NULL`; timestamps | unique slug |
| `api_keys` | `tenant_id UUID FK tenants NOT NULL`; `prefix String(11) NOT NULL`; `key_hash String(255) NOT NULL UNIQUE`; `is_active Boolean DEFAULT true NOT NULL`; `principal_type String(30) DEFAULT 'service' NOT NULL`; `capability String(50) DEFAULT 'none' NOT NULL`; `actor_ref String(255) NULL`; timestamps | `ix_api_keys_tenant_id_id(tenant_id,id)` |
| `customers` | `tenant_id UUID FK tenants NOT NULL`; `external_id`, `name`, `email` as nullable `String(255)`; `phone String(50) NULL`; `attributes JSONB DEFAULT '{}' NOT NULL`; timestamps | `uq_customers_tenant_id_id`; `ix_customers_tenant_id_id` |
| `intake_cases` | `tenant_id UUID FK tenants NOT NULL`; nullable `customer_id UUID`; `status String(50) DEFAULT 'received' NOT NULL`; `channel String(100)`, `subject String(500)`, `body Text` all NOT NULL; nullable `source String(100)`; `raw_payload` and `extracted_fields` JSONB DEFAULT `{}` NOT NULL; timestamps | `uq_intake_cases_tenant_id_id`; `fk_intake_cases_tenant_customer`; `ix_intake_cases_tenant_id_id` |
| `case_events` | `tenant_id UUID FK tenants NOT NULL`; `case_id UUID NOT NULL`; `event_type String(100) NOT NULL`; nullable `actor String(255)`; `payload JSONB DEFAULT '{}' NOT NULL`; `created_at TIMESTAMPTZ DEFAULT now() NOT NULL` | `fk_case_events_tenant_case`; `ix_case_events_tenant_id_id` |
| `agent_jobs` | `tenant_id UUID FK tenants NOT NULL`; `trace_id UUID NOT NULL`; `status String(50) DEFAULT 'queued' NOT NULL`; `source_snapshot JSONB NOT NULL`; `tenant_config_snapshot JSONB NOT NULL`; `tenant_config_sha256 String(64) NOT NULL`; `risk_signals JSONB DEFAULT '{}' NOT NULL`; `attempt_count Integer DEFAULT 0 NOT NULL`; `available_at TIMESTAMPTZ DEFAULT now() NOT NULL`; nullable `lease_expires_at`, `started_at`, `finished_at`, `side_effect_committed_at` TIMESTAMPTZ; nullable `result JSONB`; nullable `error_code String(100)`; timestamps | `uq_agent_jobs_tenant_id_id`; `uq_agent_jobs_trace_id`; indexes `ix_agent_jobs_tenant_id_id`, `ix_agent_jobs_status_available_at`, `ix_agent_jobs_running_lease` |
| `idempotency_records` | `tenant_id UUID FK tenants NOT NULL`; `key String(255) NOT NULL`; `request_hash String(64) NOT NULL`; nullable `job_id UUID`; nullable `case_id UUID`; `response JSONB DEFAULT '{}' NOT NULL`; timestamps | `uq_idempotency_records_tenant_id_key`; `fk_idempotency_records_tenant_case`; `fk_idempotency_records_tenant_job`; `ix_idempotency_records_tenant_id_id` |
| `approvals` | `tenant_id UUID FK tenants NOT NULL`; nullable `case_id UUID`; nullable `job_id UUID`; `action String(100) NOT NULL`; `status String(50) DEFAULT 'pending' NOT NULL`; `decision JSONB DEFAULT '{}' NOT NULL`; nullable `policy_reason String(100)`, `pending_action JSONB`, `tenant_config_sha256 String(64)`, `expires_at`, `decided_at`, `executed_at` TIMESTAMPTZ, `decided_by_actor_ref String(255)`, `decision_reason Text`, `execution_result JSONB`; timestamps | `uq_approvals_tenant_id_id`; `uq_approvals_job_id`; `fk_approvals_tenant_case`; `fk_approvals_tenant_job`; `ck_approvals_payload_complete`; `ck_approvals_status`; indexes `ix_approvals_tenant_id_id`, `ix_approvals_pending_expires_at(tenant_id,status,expires_at)` |
| `approval_events` | `tenant_id UUID FK tenants NOT NULL`; `approval_id UUID NOT NULL`; `event_type String(100) NOT NULL`; nullable `actor_ref String(255)`; `payload JSONB DEFAULT '{}' NOT NULL`; `created_at TIMESTAMPTZ DEFAULT now() NOT NULL` | `fk_approval_events_tenant_approval`; `ix_approval_events_tenant_id_id` |

Use these exact composite constraint declarations:

```python
sa.UniqueConstraint("tenant_id", "id", name="uq_agent_jobs_tenant_id_id")
sa.ForeignKeyConstraint(
    ["tenant_id", "job_id"],
    ["agent_jobs.tenant_id", "agent_jobs.id"],
    name="fk_idempotency_records_tenant_job",
)
sa.CheckConstraint(
    "job_id IS NULL OR (pending_action IS NOT NULL "
    "AND tenant_config_sha256 IS NOT NULL AND expires_at IS NOT NULL)",
    name="ck_approvals_payload_complete",
)
sa.CheckConstraint(
    "status IN ('pending', 'approved', 'rejected', 'expired')",
    name="ck_approvals_status",
)
```

Use `sa.text("'{}'::jsonb")` for JSONB defaults, `sa.text("true")` for
`is_active`, and `sa.text("now()")` for timestamp defaults. Do not add a server
default for UUIDs or `trace_id`; application code owns UUID generation.

- [ ] **Step 4: Implement the downgrade in reverse dependency order**

Use:

```python
def downgrade() -> None:
    for table_name in (
        "approval_events",
        "approvals",
        "idempotency_records",
        "agent_jobs",
        "case_events",
        "intake_cases",
        "customers",
        "api_keys",
        "tenants",
    ):
        op.drop_table(table_name)
```

- [ ] **Step 5: Remove the old revision files**

Delete only the seven tracked Python files listed in the file map. Do not remove
the `alembic/versions` directory or unrelated user files. Ignore local
`__pycache__`; it is not part of Alembic discovery or the release artifact.

- [ ] **Step 6: Run the focused non-database contract**

Run:

```powershell
pytest tests/test_migrations.py -q
python -m alembic heads
```

Expected: non-database assertions pass, PostgreSQL checks either pass against an
explicit reachable `DATABASE_URL` or report their existing skip, and Alembic
prints one head: `001_initial_schema (head)`.

### Task 3: Add the migration round trip to CI

**Files:**

- Edit: `.github/workflows/ci.yml`

- [ ] **Step 1: Add an explicit migration step after package installation**

Insert this step before unit/API tests:

```yaml
      - name: Verify release migration round trip
        run: |
          python -m alembic upgrade head
          python -m alembic current
          python -m alembic downgrade base
          python -m alembic upgrade head
```

The first upgrade proves a clean database reaches head. The downgrade checks
reverse dependency order. The second upgrade leaves PostgreSQL ready for the
test fixtures.

- [ ] **Step 2: Parse the workflow and inspect the command order**

Run:

```powershell
python -c "from pathlib import Path; import yaml; data=yaml.safe_load(Path('.github/workflows/ci.yml').read_text(encoding='utf-8')); steps=data['jobs']['test']['steps']; step=next(item for item in steps if item.get('name') == 'Verify release migration round trip'); print(step['run'])"
```

Expected output lists `upgrade head`, `current`, `downgrade base`, and the
second `upgrade head` in that order.

### Task 4: Lock profile YAML and CLI identity together

**Files:**

- Edit: `scripts/seed_demo.py`
- Edit: `tests/test_seed.py`
- Test: `tests/test_seed.py`
- Test: `tests/test_tenant_config.py`
- Test: `tests/test_scenario_profiles.py`
- Test: `tests/test_live_provider_smoke.py`

- [ ] **Step 1: Add the failing cross-file catalog test**

Import `PROFILE_SCENARIOS` and the new seed constant in `tests/test_seed.py`:

```python
from pathlib import Path

from scripts.live_provider_smoke import PROFILE_SCENARIOS
from scripts.seed_demo import (
    DEMO_PROFILE_SLUGS,
    DEMO_TENANT_SLUG,
    resolve_profile_slug,
    seed_demo_tenant,
    seed_operator_key,
)
```

Add:

```python
def test_demo_profile_choices_match_checked_in_yaml_and_smoke_scenarios() -> None:
    root = Path(__file__).resolve().parents[1]
    yaml_slugs = {
        path.stem
        for path in (root / "examples").glob("*.yaml")
    }

    assert set(DEMO_PROFILE_SLUGS) == yaml_slugs == set(PROFILE_SCENARIOS)
    for slug in DEMO_PROFILE_SLUGS:
        assert resolve_profile_slug(slug) == slug
```

Reuse the module-level `resolve_profile_slug` import instead of importing it
inside the older test.

- [ ] **Step 2: Run the new test and confirm the missing constant**

Run:

```powershell
pytest tests/test_seed.py::test_demo_profile_choices_match_checked_in_yaml_and_smoke_scenarios -q
```

Expected: collection fails because `DEMO_PROFILE_SLUGS` does not exist.

- [ ] **Step 3: Define one seed CLI catalog**

In `scripts/seed_demo.py`, define:

```python
DEMO_PROFILE_SLUGS = (
    "freight-broker",
    "repair-service",
    "language-school",
)
DEMO_TENANT_SLUG = DEMO_PROFILE_SLUGS[0]
```

Use it in argparse:

```python
parser.add_argument(
    "--profile",
    default=DEMO_TENANT_SLUG,
    choices=DEMO_PROFILE_SLUGS,
    help="trusted tenant profile slug",
)
```

- [ ] **Step 4: Run all YAML/profile contracts**

Run:

```powershell
pytest tests/test_seed.py tests/test_tenant_config.py tests/test_scenario_profiles.py tests/test_live_provider_smoke.py tests/test_skills.py tests/test_freight_eval_models.py tests/test_freight_eval_runner.py evals/test_freight.py -q
```

Expected: all tests pass without network or provider credentials. The three
tenant profiles compile against the action/document registries, and the freight
dataset, manifest, and skill fixtures retain their deterministic contracts.

### Task 5: Make the demo documentation executable as written

**Files:**

- Edit: `tests/test_observability.py`
- Edit: `docs/runbooks/live-provider-demo.md`
- Edit: `README.md`
- Edit: `app/workers/__main__.py`
- Edit: `app/skills/definitions.py`
- Edit: `tests/test_agent_worker.py`

- [ ] **Step 1: Strengthen the failing documentation contract**

Extend `test_product_docs_describe_safe_live_demo_entrypoints` with:

```python
assert runbook.is_file()
text = runbook.read_text(encoding="utf-8")
for required in (
    "docker compose down -v",
    "docker compose up -d db",
    "docker compose run --rm api alembic upgrade head",
    "docker compose run --rm api alembic current",
    "docker compose up -d worker",
    "docker compose stop worker",
    "python -m app.workers",
    "--profile repair-service",
    "--profile language-school",
    "repair_service",
    "language_school",
    "documents: {}",
):
    assert required in text, required

assert text.index("docker compose run --rm api alembic upgrade head") < text.index(
    "docker compose up -d worker"
)
```

Keep the existing secret-text and `WORKER_CONCURRENCY` checks. Remove the
conditional `if runbook.exists()` because the tracked runbook is required.

- [ ] **Step 2: Run the documentation test and confirm it fails**

Run:

```powershell
pytest tests/test_observability.py::test_product_docs_describe_safe_live_demo_entrypoints -q
```

Expected: failure on the missing destructive reset, migration-container
sequence, host-worker alternative, or alternative-profile details.

- [ ] **Step 3: Rewrite the runbook in execution order**

Use these headings and commands in `docs/runbooks/live-provider-demo.md`:

```markdown
# Live provider demo runbook

## 1. Prerequisites
## 2. Configure the provider
## 3. Recreate the local database
## 4. Build images and apply the release schema
## 5. Start and verify the API
## 6. Provision the freight demo tenant
## 7. Start one worker
### Option A: Compose worker
### Option B: Worker from PowerShell
## 8. Run the freight body smoke
## 9. Run the repair-service scenario
## 10. Run the language-school scenario
## 11. Optional freight webhook
## 12. Optional approval decision
## 13. Troubleshooting
## 14. Stop the demo and clear secrets
```

The canonical command sequence must be:

```powershell
Copy-Item .env.example .env
docker compose down -v
docker compose build api worker
docker compose up -d db
docker compose ps db
docker compose run --rm api alembic upgrade head
docker compose run --rm api alembic current
docker compose up -d api
Invoke-RestMethod http://localhost:8000/health
$env:INTEGRA_DEMO_API_KEY = (python scripts/seed_demo.py --profile freight-broker).Trim()
```

Show the two worker alternatives exactly:

```powershell
# Option A
docker compose up -d worker
docker compose logs --tail 100 worker

# Option B
docker compose stop worker
$env:DATABASE_URL = "postgresql+asyncpg://integra:integra@localhost:5432/integra"
python -m app.workers
```

Explain that Option B occupies its terminal. State that `.env` changes require
`docker compose up -d --force-recreate worker`, not an image rebuild.

For the alternative profiles, show only the differing commands:

```powershell
$env:INTEGRA_DEMO_API_KEY = (python scripts/seed_demo.py --profile repair-service).Trim()
python scripts/live_provider_smoke.py --profile repair-service --base-url http://localhost:8000

$env:INTEGRA_DEMO_API_KEY = (python scripts/seed_demo.py --profile language-school).Trim()
python scripts/live_provider_smoke.py --profile language-school --base-url http://localhost:8000
```

Document the expected safe identities `repair_service` and `language_school`.
State that both profiles declare `documents: {}` and therefore do not support
the freight attachment/webhook demonstration.

Use this approval sequence:

```powershell
$env:INTEGRA_DEMO_OPERATOR_KEY = (
  python scripts/seed_demo.py --profile freight-broker --operator-ref demo-operator
).Trim()
$headers = @{ "X-API-Key" = $env:INTEGRA_DEMO_OPERATOR_KEY }
$decision = @{ decision = "approve"; reason = "Local demo approval" } | ConvertTo-Json
Invoke-RestMethod `
  -Method Post `
  -Uri "http://localhost:8000/v1/approvals/$approvalId/decide" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $decision
```

Explain that `$approvalId` comes from the safe smoke result only when status is
`awaiting_approval`. Keep the existing source-free output rules, webhook
command, stable error explanations, size limits, lease fencing, and
`tool_execution_failed` guidance.

Use non-destructive shutdown by default:

```powershell
docker compose down
Remove-Item Env:LLM_API_KEY -ErrorAction SilentlyContinue
Remove-Item Env:INTEGRA_DEMO_API_KEY -ErrorAction SilentlyContinue
Remove-Item Env:INTEGRA_DEMO_OPERATOR_KEY -ErrorAction SilentlyContinue
Remove-Item Env:INBOUND_WEBHOOK_API_KEY -ErrorAction SilentlyContinue
Remove-Item Env:DATABASE_URL -ErrorAction SilentlyContinue
```

Describe `.env` removal as an explicit optional step because deleting it loses
the operator's local provider configuration.

- [ ] **Step 4: Align README with the release workflow**

Replace the quick-start migration and full-stack examples with the explicit
order:

```powershell
docker compose up -d db
docker compose run --rm api alembic upgrade head
docker compose up -d api worker
```

State that the first release uses `001_initial_schema`, all databases must start
from scratch, and future schema changes add append-only revisions. Remove the
old text about an approval-status migration owner and the historical chain.

- [ ] **Step 5: Remove stale phase labels from current entry-point docstrings**

Use:

```python
# app/workers/__main__.py
"""Process entry point for the asynchronous agent worker."""

# app/skills/definitions.py
"""Versioned skill definitions and their strict schemas."""

# tests/test_agent_worker.py
"""Offline orchestration tests for the isolated agent worker."""
```

Do not rewrite archived specifications. Do not rename unrelated historical test
files as part of this release task.

- [ ] **Step 6: Run the documentation contract**

Run:

```powershell
pytest tests/test_observability.py::test_product_docs_describe_safe_live_demo_entrypoints -q
```

Expected: PASS.

### Task 6: Verify a clean local database and the complete offline gate

**Files:**

- Verify: `alembic/versions/001_initial_schema.py`
- Verify: `app/db/models.py`
- Verify: `examples/*.yaml`
- Verify: `docs/runbooks/live-provider-demo.md`

- [ ] **Step 1: Confirm the destructive target before resetting Docker data**

Run:

```powershell
docker compose config --services
docker volume ls --filter label=com.docker.compose.project=integra_intake
```

Expected: services are `db`, `api`, and `worker`; any listed volumes belong to
this Compose project. Stop if the project name or targets differ.

- [ ] **Step 2: Recreate the disposable database and build current images**

Run:

```powershell
docker compose down -v
docker compose build api worker
docker compose up -d db
docker compose ps db
```

Expected: only this project's containers and PostgreSQL volume are removed;
the recreated `db` service reaches `healthy`.

- [ ] **Step 3: Prove the containerized upgrade and revision identity**

Run:

```powershell
docker compose run --rm api alembic upgrade head
docker compose run --rm api alembic current
docker compose run --rm api alembic heads
```

Expected: `current` and `heads` report `001_initial_schema`; upgrade exits zero.

- [ ] **Step 4: Prove downgrade and second clean upgrade**

Run:

```powershell
docker compose run --rm api alembic downgrade base
docker compose run --rm api alembic upgrade head
docker compose run --rm api alembic current
```

Expected: downgrade and upgrade exit zero; current returns
`001_initial_schema (head)`.

- [ ] **Step 5: Run PostgreSQL-backed migration and runtime tests**

Run in the activated host virtual environment:

```powershell
$env:DATABASE_URL = "postgresql+asyncpg://integra:integra@localhost:5432/integra"
pytest tests/test_migrations.py tests/test_postgres_integration.py tests/test_postgres_approvals.py -q
```

Expected: no skips and no failures. A PostgreSQL skip means integration evidence
is missing and must not be reported as success.

- [ ] **Step 6: Run the complete provider-free suite and freight gate**

Run:

```powershell
pytest -q
pytest evals/test_freight.py -q
python -m alembic heads
git diff --check
```

Expected: all tests pass; the freight gate reports 50 passed; Alembic reports
one head; `git diff --check` prints no errors. Do not supply `LLM_API_KEY` to
pytest or CI.

- [ ] **Step 7: Inspect the final diff for scope and privacy regressions**

Run:

```powershell
git status --short
git diff -- alembic app/db/models.py scripts/seed_demo.py tests/test_migrations.py tests/test_seed.py tests/test_observability.py .github/workflows/ci.yml README.md docs/runbooks/live-provider-demo.md
rg -n "LLM_API_KEY=.*[^=]$|Bearer |source_text|transcript" docs/runbooks/live-provider-demo.md README.md .github/workflows/ci.yml
```

Expected: the diff contains only the planned baseline, profile-catalog,
documentation, CI, and stale-docstring changes. No real credential, source
payload, transcript, or provider secret appears in tracked artifacts.

### Task 7: Review the completed changes

**Files:**

- Review: every file changed while executing this plan
- Reference: `docs/specs/2026-10-01-release-baseline-and-demo-runbook-design.md`
- Reference: `docs/plans/2026-10-01-release-baseline-and-demo-runbook.md`

- [ ] **Step 1: Capture the review scope**

Run:

```powershell
git status --short
git diff --stat
git diff -- alembic app/db/models.py scripts/seed_demo.py tests/test_migrations.py tests/test_seed.py tests/test_observability.py .github/workflows/ci.yml README.md docs/runbooks/live-provider-demo.md app/workers/__main__.py app/skills/definitions.py tests/test_agent_worker.py
```

Expected: the review input contains the release baseline, profile-catalog
contract, runbook, README, CI migration round trip, tests, and docstring cleanup.

- [ ] **Step 2: Call the `code-review` skill without a subagent**

Call the `code-review` skill on the changes made by this plan. Give it the full
diff, the approved specification, this implementation plan, and the final test
results. Review only the plan's changes for:

- migration correctness and downgrade dependency order;
- agreement between Alembic and ORM metadata;
- tenant isolation and approval constraints;
- provider-key and source-data privacy;
- YAML/profile/CLI identity consistency;
- runbook command correctness;
- test strength and CI behavior.

Do not use a review subagent. Do not make corrections after the review. Present
all findings to the user first, ordered by severity, with file and line
references and a concrete suggested fix.
