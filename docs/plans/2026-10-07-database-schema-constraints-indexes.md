# Implementation plan: Database schema constraints and indexes

> **For agents:** Use the executing-plans skill to implement this plan task by task. Steps use checkbox syntax (`- [ ]`) for tracking.

**Purpose:** Enforce customer and idempotency identity rules and align PostgreSQL indexes with worker queries.

**Architecture:** Keep the current tables and string columns. Add database constraints for closed values and identity rules, remove indexes duplicated by unique constraints, and tune worker indexes to match their filters and ordering. Apply each definition to SQLAlchemy metadata and the editable baseline migration.

**Key files/stack:** SQLAlchemy 2 declarative models, PostgreSQL indexes and constraints, Alembic baseline DDL, pytest.

**Design:** `docs/specs/2026-10-07-database-schema-constraints-indexes-design.md`

---

## File map

- `app/db/models.py`: ORM constraints and indexes.
- `alembic/versions/001_initial_schema.py`: baseline PostgreSQL DDL.
- `tests/test_migrations.py`: metadata assertions and optional PostgreSQL integrity checks.

### Task 1: Update SQLAlchemy metadata

**Files:**
- Edit: `app/db/models.py`

- [ ] **Step 1: Add tenant and credential checks**

Define these table arguments while preserving `ix_api_keys_tenant_id_id`:

```python
class Tenant(TimestampedModel, Base):
    __tablename__ = "tenants"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'inactive')",
            name="ck_tenants_status",
        ),
    )
```

```python
class ApiKey(TimestampedModel, Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        CheckConstraint(
            "principal_type IN ('service', 'operator')",
            name="ck_api_keys_principal_type",
        ),
        CheckConstraint(
            "capability IN ('none', 'approval_decider')",
            name="ck_api_keys_capability",
        ),
        Index("ix_api_keys_tenant_id_id", "tenant_id", "id"),
    )
```

- [ ] **Step 2: Enforce tenant-scoped customer identity**

Replace `Customer.__table_args__` with:

```python
__table_args__ = (
    UniqueConstraint("tenant_id", "id", name="uq_customers_tenant_id_id"),
    UniqueConstraint(
        "tenant_id", "email", name="uq_customers_tenant_id_email"
    ),
    UniqueConstraint(
        "tenant_id",
        "external_id",
        name="uq_customers_tenant_id_external_id",
    ),
)
```

Do not use `NULLS NOT DISTINCT`; multiple customers may omit either identifier.

- [ ] **Step 3: Constrain intake status**

Keep `uq_intake_cases_tenant_id_id` and the composite customer foreign key. Remove `ix_intake_cases_tenant_id_id` and add:

```python
CheckConstraint(
    "status IN ('received')",
    name="ck_intake_cases_status",
),
```

- [ ] **Step 4: Align agent-job metadata**

Replace `AgentJob.__table_args__` with:

```python
__table_args__ = (
    UniqueConstraint("tenant_id", "id", name="uq_agent_jobs_tenant_id_id"),
    UniqueConstraint("trace_id", name="uq_agent_jobs_trace_id"),
    CheckConstraint(
        "status IN ('queued', 'running', 'awaiting_approval', "
        "'succeeded', 'failed', 'failed_uncertain')",
        name="ck_agent_jobs_status",
    ),
    Index(
        "ix_agent_jobs_status_available_at",
        "status",
        "available_at",
        "id",
    ),
    Index("ix_agent_jobs_running_lease", "status", "lease_expires_at"),
)
```

- [ ] **Step 5: Enforce one idempotency record per tenant job**

Add this beside `uq_idempotency_records_tenant_id_key` and keep `ix_idempotency_records_tenant_id_id`:

```python
UniqueConstraint(
    "tenant_id",
    "job_id",
    name="uq_idempotency_records_tenant_id_job_id",
),
```

- [ ] **Step 6: Replace approval indexes**

Remove `ix_approvals_tenant_id_id`. Replace the expiry index with:

```python
Index(
    "ix_approvals_pending_expires_at",
    "status",
    "expires_at",
    "id",
),
```

Keep both approval unique constraints, both foreign keys, and both check constraints.

- [ ] **Step 7: Inspect the metadata diff**

Run:

```powershell
git diff -- app/db/models.py
```

Expected: only approved constraints and indexes changed. Columns, defaults, nullability, and relationships remain unchanged.

### Task 2: Synchronize the baseline migration

**Files:**
- Edit: `alembic/versions/001_initial_schema.py`

- [ ] **Step 1: Add tenant and credential checks**

Add the matching `sa.CheckConstraint` declarations:

```python
sa.CheckConstraint(
    "status IN ('active', 'inactive')",
    name="ck_tenants_status",
),
sa.CheckConstraint(
    "principal_type IN ('service', 'operator')",
    name="ck_api_keys_principal_type",
),
sa.CheckConstraint(
    "capability IN ('none', 'approval_decider')",
    name="ck_api_keys_capability",
),
```

Place each constraint in its owning `op.create_table` call.

- [ ] **Step 2: Add customer uniqueness**

Add these constraints to `customers` and remove the following `ix_customers_tenant_id_id` call:

```python
sa.UniqueConstraint(
    "tenant_id", "email", name="uq_customers_tenant_id_email"
),
sa.UniqueConstraint(
    "tenant_id",
    "external_id",
    name="uq_customers_tenant_id_external_id",
),
```

- [ ] **Step 3: Constrain intake status**

Add the matching check to `intake_cases` and remove the `ix_intake_cases_tenant_id_id` call:

```python
sa.CheckConstraint(
    "status IN ('received')",
    name="ck_intake_cases_status",
),
```

- [ ] **Step 4: Align agent-job DDL**

Add the matching status check, remove `ix_agent_jobs_tenant_id_id`, and change the queued index:

```python
sa.CheckConstraint(
    "status IN ('queued', 'running', 'awaiting_approval', "
    "'succeeded', 'failed', 'failed_uncertain')",
    name="ck_agent_jobs_status",
),
```

```python
op.create_index(
    "ix_agent_jobs_status_available_at",
    "agent_jobs",
    ["status", "available_at", "id"],
)
```

Keep `ix_agent_jobs_running_lease` unchanged.

- [ ] **Step 5: Add idempotency uniqueness**

Add this constraint to `idempotency_records`:

```python
sa.UniqueConstraint(
    "tenant_id",
    "job_id",
    name="uq_idempotency_records_tenant_id_job_id",
),
```

- [ ] **Step 6: Replace approval indexes**

Remove `ix_approvals_tenant_id_id` and define:

```python
op.create_index(
    "ix_approvals_pending_expires_at",
    "approvals",
    ["status", "expires_at", "id"],
)
```

- [ ] **Step 7: Confirm downgrade behavior**

Read `downgrade()` and confirm it drops tables in dependency order. Do not add individual index or constraint drops because each `op.drop_table` removes table-owned objects.

- [ ] **Step 8: Compare ORM and migration names**

Run:

```powershell
rg -n "ck_tenants_status|ck_api_keys_principal_type|ck_api_keys_capability|uq_customers_tenant_id_email|uq_customers_tenant_id_external_id|ck_intake_cases_status|ck_agent_jobs_status|uq_idempotency_records_tenant_id_job_id|ix_approvals_pending_expires_at|ix_agent_jobs_status_available_at" app/db/models.py alembic/versions/001_initial_schema.py
```

Expected: every listed name appears once in each file.

### Task 3: Add schema tests through `unit-test-writer`

**Files:**
- Edit through the skill: `tests/test_migrations.py`

- [ ] **Step 1: Invoke the `unit-test-writer` skill**

Give the skill both schema diffs and require these assertions:

```text
ORM metadata:
- customer unique constraints cover (tenant_id, email) and (tenant_id, external_id)
- idempotency uniqueness covers (tenant_id, job_id)
- queued-job index is (status, available_at, id)
- pending-approval index is (status, expires_at, id)
- customers, intake_cases, agent_jobs, and approvals omit ix_<table>_tenant_id_id
- every new check constraint exists under the name specified in this plan

Live PostgreSQL:
- pg_indexes reports the queued and approval index column order
- pg_constraint reports every new unique and check constraint
- duplicate non-null customer email or external_id in one tenant fails
- the same customer identifier in different tenants succeeds
- multiple NULL customer identifiers succeed
- duplicate (tenant_id, job_id) idempotency rows fail
- invalid tenant, API-key, intake-case, and agent-job scalar values fail
```

Tell the skill to preserve tenant-boundary assertions and replace the loop that assumes every tenant-scoped table has `ix_<table>_tenant_id_id`.

- [ ] **Step 2: Run focused tests**

Run:

```powershell
pytest tests/test_migrations.py -q
```

Expected without `DATABASE_URL`: metadata tests pass and the PostgreSQL test skips. Expected with PostgreSQL: all migration tests pass.

- [ ] **Step 3: Resolve contract mismatches**

When a test reports a constraint name, column order, or predicate mismatch, update both schema files to the approved contract. Ask `unit-test-writer` to revise a test only when it contradicts this plan.

- [ ] **Step 4: Rerun focused tests**

Run `pytest tests/test_migrations.py -q` again. Expected: all reachable tests pass.

### Task 4: Run project verification

**Files:**
- Verify: repository-wide behavior

- [ ] **Step 1: Run the full suite**

Run `pytest -q`.

Expected: all unit and deterministic eval tests pass. PostgreSQL tests may skip only when `DATABASE_URL` is unavailable.

- [ ] **Step 2: Check whitespace**

Run `git diff --check`.

Expected: no output and exit code `0`.

- [ ] **Step 3: Review the diff scope**

Run:

```powershell
git status --short
git diff -- app/db/models.py alembic/versions/001_initial_schema.py tests/test_migrations.py docs/specs/2026-10-07-database-schema-constraints-indexes-design.md docs/plans/2026-10-07-database-schema-constraints-indexes.md
```

Expected: only the approved schema, tests, specification, and plan changed.

### Task 5: Review the changes

**Files:**
- Review: final repository diff

- [ ] **Step 1: Call the `code-review` skill**

Call the `code-review` skill on the changes made by this plan. Give it the final diff and `docs/specs/2026-10-07-database-schema-constraints-indexes-design.md`. Ask it to check PostgreSQL semantics, ORM/migration parity, tenant isolation, index usefulness, constraint compatibility, and test coverage. Do not make corrections; discuss findings with the user first.

- [ ] **Step 2: Report evidence**

Report the focused test result, full-suite result, PostgreSQL test status, `git diff --check`, and review findings. Do not claim PostgreSQL execution evidence when integration tests skipped.
