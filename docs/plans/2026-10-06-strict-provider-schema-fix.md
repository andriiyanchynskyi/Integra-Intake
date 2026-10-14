# Implementation plan: Strict provider schema fix

> **For agents:** Use the executing-plans skill to implement this plan task by task. Steps use checkbox syntax (`- [ ]`) for tracking.

**Purpose:** Make the `AgentProposal` JSON Schema valid for OpenAI strict structured outputs so the live provider accepts the request.

**Architecture:** Keep the Pydantic model as the source for runtime validation and base JSON Schema generation. Normalize the fresh schema at the provider boundary so every object property appears in `required`, while internal scripted payloads may continue omitting fields with defaults.

**Key files/stack:** Python 3.12, Pydantic v2, httpx, pytest, Docker Compose.

## File map

- `app/providers/openai_compatible.py`: normalize the generated schema for OpenAI strict mode.
- `tests/test_openai_compatible_provider.py`: enforce the strict nested-schema requirement.
- `tests/test_skills.py`: retain the existing internal compatibility contract; no edit expected.
- `docs/specs/2026-10-06-strict-provider-schema-fix-design.md`: approved design; no implementation edits expected.

---

### Task 1: Add the strict-schema regression test

**Files:**
- Test: `tests/test_openai_compatible_provider.py`

- [ ] **Step 1: Change the existing schema assertion**

Replace:

```python
assert set(fields_schema["required"]) == {"name", "value"}
```

with:

```python
assert set(fields_schema["required"]) == {
    "name",
    "value",
    "source_excerpt",
}
```

Remove the obsolete assertion:

```python
assert "source_excerpt" not in fields_schema["required"]
```

- [ ] **Step 2: Run the focused test and confirm the regression is exposed**

Run:

```powershell
pytest tests/test_openai_compatible_provider.py::test_valid_structured_proposal_uses_openai_compatible_contract -q
```

Expected: FAIL because the generated `ProposalValue` schema requires only `name` and `value`.

### Task 2: Normalize the provider schema

**Files:**
- Edit: `app/providers/openai_compatible.py`
- Test: `tests/test_openai_compatible_provider.py`

- [ ] **Step 1: Add a recursive strict-schema normalizer**

Add this helper after `_canonical_json`:

```python
def _strict_json_schema(schema: dict[str, object]) -> dict[str, object]:
    for value in schema.values():
        if isinstance(value, dict):
            _strict_json_schema(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    _strict_json_schema(item)
    properties = schema.get("properties")
    if schema.get("type") == "object" and isinstance(properties, dict):
        schema["required"] = list(properties)
    return schema
```

- [ ] **Step 2: Apply the normalizer to the response-format schema**

Replace:

```python
"schema": AgentProposal.model_json_schema(),
```

with:

```python
"schema": _strict_json_schema(AgentProposal.model_json_schema()),
```

The helper mutates only the new dictionary returned for the current request. It does not change Pydantic validation.

- [ ] **Step 3: Run the focused provider test**

Run:

```powershell
pytest tests/test_openai_compatible_provider.py::test_valid_structured_proposal_uses_openai_compatible_contract -q
```

Expected: `1 passed`.

- [ ] **Step 4: Verify internal payload compatibility**

Run:

```powershell
pytest tests/test_skills.py::test_proposal_value_source_excerpt_is_optional_for_existing_payloads -q
```

Expected: `1 passed`; a payload without `source_excerpt` still validates to `None`.

- [ ] **Step 5: Run all provider adapter tests**

Run:

```powershell
pytest tests/test_openai_compatible_provider.py -q
```

Expected: all tests pass with no network access.

### Task 3: Verify the live provider boundary

**Files:**
- No source changes.

- [ ] **Step 1: Rebuild and recreate the worker**

Run:

```powershell
docker compose build worker
docker compose up -d --force-recreate worker
docker compose ps worker
```

Expected: `worker` reports `Up`. The recreated container receives the existing provider settings.

- [ ] **Step 2: Provision a fresh freight service key in the execution shell**

Run:

```powershell
$env:INTEGRA_DEMO_API_KEY = (python scripts/seed_demo.py --profile freight-broker).Trim()
```

Expected: the variable contains a non-empty key. Do not print or persist it.

- [ ] **Step 3: Run the freight smoke**

Run:

```powershell
python scripts/live_provider_smoke.py --profile freight-broker --base-url http://localhost:8000
```

Expected: exit code `0` with terminal `status` equal to `succeeded` or `awaiting_approval`. If it fails, inspect `docker compose logs --tail 120 worker` and use the source-free event for the returned `trace_id`; do not expose response bodies or credentials.

### Task 4: Run repository quality checks

**Files:**
- No source changes.

- [ ] **Step 1: Run the full test suite**

Run:

```powershell
pytest -q
```

Expected: all configured unit, API, regression, and reachable integration tests pass. PostgreSQL tests may skip only when their documented database prerequisite is unavailable; a skip is not integration evidence.

- [ ] **Step 2: Check the diff for whitespace errors**

Run:

```powershell
git diff --check
```

Expected: exit code `0` with no output.

### Task 5: Review the completed change

**Files:**
- Review the final diff only.

- [ ] **Step 1: Call the `code-review` skill**

Call the `code-review` skill on the changes made by this plan. Provide the final diff and the context from `docs/specs/2026-10-06-strict-provider-schema-fix-design.md`. Review security, provider-boundary correctness, schema recursion, test coverage, and repository style. Do not make corrections; discuss any findings with the user first.
