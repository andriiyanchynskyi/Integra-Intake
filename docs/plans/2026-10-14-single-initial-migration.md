# Implementation plan: single initial migration baseline

## Scope

The approved design resets the development migration baseline. The complete
schema, including `job_artifacts`, will be created by
`001_initial_schema`; the standalone `002_job_artifacts` revision will be
removed. Existing databases may be recreated, so no compatibility migration
for an `alembic_version` value of `002_job_artifacts` is required.

The existing uncommitted correction in
`tests/test_postgres_approvals.py` (`summary` to `commodity`) is unrelated and
must remain intact.

## Files and responsibilities

- `alembic/versions/001_initial_schema.py`: single complete upgrade and
  downgrade schema definition.
- `alembic/versions/002_job_artifacts.py`: obsolete second revision to remove.
- `tests/test_migrations.py`: migration graph and schema-contract assertions.
- `README.md`: local release-migration instructions.
- `.github/workflows/ci.yml`: clean-database migration verification and
  explicit single-head assertion.

## Tasks

### 1. Extend the initial migration

Edit `alembic/versions/001_initial_schema.py`.

In `upgrade()`, after creating `approval_events`, add the complete
`job_artifacts` table definition from the current `002_job_artifacts.py`:

```python
    op.create_table(
        "job_artifacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id"),
            nullable=False,
        ),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.String(50), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
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
        sa.UniqueConstraint(
            "tenant_id", "job_id", "kind", name="uq_job_artifacts_tenant_job_kind"
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "job_id"],
            ["agent_jobs.tenant_id", "agent_jobs.id"],
            name="fk_job_artifacts_tenant_job",
        ),
        sa.CheckConstraint(
            "kind IN ('reply_draft', 'review_flag')",
            name="ck_job_artifacts_kind",
        ),
    )
    op.create_index(
        "ix_job_artifacts_tenant_id_id", "job_artifacts", ["tenant_id", "id"]
    )
    op.create_index(
        "ix_job_artifacts_tenant_id_job_id",
        "job_artifacts",
        ["tenant_id", "job_id"],
    )
```

Make `downgrade()` drop `job_artifacts` indexes and table before dropping
`agent_jobs` and the other referenced tables:

```python
    op.drop_index("ix_job_artifacts_tenant_id_job_id", table_name="job_artifacts")
    op.drop_index("ix_job_artifacts_tenant_id_id", table_name="job_artifacts")
    op.drop_table("job_artifacts")
```

### 2. Remove the obsolete revision

Delete `alembic/versions/002_job_artifacts.py`. Confirm that
`python -m alembic heads` reports only `001_initial_schema` and that the
initial migration still declares `down_revision = None`.

### 3. Update migration contract tests

Edit `tests/test_migrations.py`:

- Change the expected revision file list to
  `['001_initial_schema.py']`.
- Remove reads and assertions for `002_job_artifacts.py`.
- Rename `test_release_uses_append_only_schema_revisions` to describe the
  single initial baseline.
- Keep the `job_artifacts` ORM metadata, column, constraint, index, and
  composite-FK assertions unchanged.

Run the focused static migration tests before database tests:

```powershell
python -m pytest tests/test_migrations.py -q
```

Expected result: all migration tests pass without looking for revision 002.

### 4. Update local documentation

In `README.md`, replace the statement that release startup applies
`002_job_artifacts` after `001_initial_schema` with a statement that the
complete release schema is applied by `001_initial_schema`. Keep the existing
rule that API and worker startup do not run migrations.

### 5. Reset the ephemeral CI schema and assert the baseline

In `.github/workflows/ci.yml`, after package installation and before the first
Alembic command, add a step that drops and recreates the `public` schema using
the workflow's `DATABASE_URL`:

```yaml
- name: Reset ephemeral PostgreSQL schema
  run: |
    python - <<'PY'
    import asyncio
    import os

    from sqlalchemy.ext.asyncio import create_async_engine

    async def reset_schema() -> None:
        engine = create_async_engine(os.environ["DATABASE_URL"])
        async with engine.begin() as connection:
            await connection.exec_driver_sql("DROP SCHEMA IF EXISTS public CASCADE")
            await connection.exec_driver_sql("CREATE SCHEMA public")
        await engine.dispose()

    asyncio.run(reset_schema())
    PY
```

This reset is limited to the disposable PostgreSQL service and removes any
stale `alembic_version` row before the new baseline is applied. Do not add
compatibility code to application migrations.

Keep the migration round-trip and add `python -m alembic heads` after
`current` so the job verifies the single head while exercising:

```yaml
python -m alembic upgrade head
python -m alembic current
python -m alembic heads
python -m alembic downgrade base
python -m alembic upgrade head
```

### 6. Verify the complete change

With PostgreSQL reachable, run:

```powershell
$env:DATABASE_URL='postgresql+asyncpg://integra:integra@localhost:5432/integra'
python -m alembic upgrade head
python -m alembic current
python -m alembic heads
python -m alembic downgrade base
python -m alembic upgrade head
python -m pytest -q
python -m pytest evals/test_freight.py -q --freight-eval-report "$env:TEMP\freight-evals-ci-final.json"
python -m evals.freight.reporting --report "$env:TEMP\freight-evals-ci-final.json"
git diff --check
```

Expected results: `current` and `heads` identify only `001_initial_schema`,
the full test suite and freight evals pass, and `git diff --check` emits no
errors.

### 7. Run final quality checks

Run the repository's configured checks after all implementation edits:

```powershell
python -m pytest -q
python -m pytest evals/test_freight.py -q
git diff --check
```

The project has no separately configured formatter, linter, or type checker.
If any command fails, stop and fix the reported issue before review.

### 8. Review the implementation diff

Call the `code-review` skill on the changes made by this plan. Give it the
full diff and the approved design context. Do not make corrections during the
review; discuss any findings with the user first.

## Review checklist

- The final migration graph has one revision file and one head.
- `job_artifacts` is present after upgrade and absent after downgrade.
- ORM and SQL schema contracts still agree, including tenant-scoped FKs.
- No code path references `002_job_artifacts`.
- The prior approval-fixture correction remains in the working tree.
- CI verifies the same round-trip as local PostgreSQL.
