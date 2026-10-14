# Tenant Configuration (Phase 3) Implementation Plan

> Execute this plan task by task. Each production behavior starts with a failing test, is implemented minimally, and is verified before moving on.

**Goal:** Add safe, version-controlled YAML tenant profiles with strict Pydantic validation and deterministic routing for the next agent/policy phases.

**Spec:** `docs/specs/2026-09-15-tenant-configuration-design.md`

**Architecture:** `app/tenants/config.py` owns the typed configuration contract, `app/tenants/loader.py` owns safe file parsing and path-aware errors, and `app/tenants/routing.py` owns pure deterministic decisions. YAML files under `examples/` are data only; they cannot execute code or select a tenant at runtime. FastAPI, SQLAlchemy, and the phase-2 case service remain unchanged.

**Tech stack:** Python 3.12, PyYAML safe loader, Pydantic v2, pytest.

## Existing repository facts

- Phase 2 already provides tenant-scoped persistence, API-key authentication, and a `received` case lifecycle.
- `pyproject.toml` has no YAML dependency and discovers packages under `app*`.
- There is no `app/tenants/` package and no `examples/` directory.
- The existing tests use pytest and contain both pure unit tests and optional PostgreSQL integration tests. Phase 3 does not need PostgreSQL.
- `AGENTS.md` is currently a template. `.gitignore` already contains an uncommitted `AGENTS.md` entry; preserve that unrelated user change.

## File map

Create or modify only these files for the phase:

- `pyproject.toml` — add the runtime `PyYAML` dependency.
- `app/tenants/__init__.py` — expose the public configuration and routing interfaces.
- `app/tenants/config.py` — strict Pydantic enums/models and cross-field invariants.
- `app/tenants/loader.py` — `yaml.safe_load`, source-path error wrapping, and model validation.
- `app/tenants/routing.py` — immutable assessment/outcome types and the pure `TenantRouter`.
- `examples/freight-broker.yaml` — primary freight-broker profile.
- `examples/repair-service.yaml` — reusable repair profile.
- `examples/language-school.yaml` — reusable school profile.
- `tests/test_tenant_config.py` — model, loader, example-profile, and routing tests.
- `AGENTS.md` — replace the template with accurate repository instructions; do not mention the project's learning process.

Do not modify phase-2 ORM, API, migration, auth, or database files. Do not add a FastAPI endpoint for configuration in this phase.

## Configuration contract to implement

The three example files must have this shape:

```yaml
slug: freight-broker
display_name: Freight broker
intake_types:
  - name: load_request
    description: A request to quote or arrange a freight load.
    required_fields: [origin, destination, equipment, pickup_window, commodity, contact]
fields:
  origin:
    type: address
    label: Origin
  equipment:
    type: single_select
    label: Equipment
    options: [dry_van, reefer, flatbed]
action_policy:
  create_case:
    allowed: true
    requires_approval: false
  send_reply:
    allowed: true
    requires_approval: true
  close_case:
    allowed: true
    requires_approval: true
routing:
  outcome_names: [urgent, awaiting_input, pending_approval, ready, rejected]
  always_approval_actions: [send_reply, close_case]
```

Implement these exact model rules:

- Every model uses `ConfigDict(extra="forbid")`; primitive strings and booleans use Pydantic `StrictStr`/`StrictBool` so YAML cannot silently coerce numbers or truthy text. Enum fields intentionally accept their serialized YAML strings and convert them to enum members.
- `slug` is lowercase kebab-case; `display_name` is non-empty.
- `FieldType` values are exactly `short_text`, `long_text`, `email`, `phone`, `address`, `date_time`, `number`, `boolean`, `single_select`, and `multi_select`.
- A `FieldDefinition` has `type`, `label`, and optional `options`. Select types require at least one unique option; all other types must omit `options`.
- Each `IntakeTypeConfig.name` is non-empty. Its `required_fields` list contains unique names, and every name must exist in `TenantConfig.fields`.
- `ActionRule` contains `allowed` and `requires_approval` booleans.
- `RoutingConfig.outcome_names` must contain each of the five `RoutingStatus` values exactly once. `always_approval_actions` must be unique and include `send_reply`.
- Every action in `always_approval_actions` must exist in `action_policy`.

The public loader contract is:

```python
def load_tenant_config(path: Path) -> TenantConfig:
    """Read UTF-8 YAML with yaml.safe_load and return a validated profile."""

def parse_tenant_config(content: str, *, source: str = "<string>") -> TenantConfig:
    """Validate YAML content while retaining a human-readable source label."""
```

Both functions raise `TenantConfigError(ValueError)` with the source and actionable parser/validation details. They never return a partially validated dictionary.

The pure router contract is:

```python
@dataclass(frozen=True, slots=True)
class RoutingAssessment:
    intake_type: str
    present_fields: frozenset[str]
    contains_safety_or_legal_risk: bool
    requested_action: str

class TenantRouter:
    def __init__(self, config: TenantConfig) -> None: ...
    def route(self, assessment: RoutingAssessment) -> RoutingOutcome: ...
```

`RoutingOutcome` contains `status: RoutingStatus`, `decision: RoutingDecision`, `missing_required_fields: tuple[str, ...]`, and a machine-readable `reason`. The fixed evaluation order and mappings are:

| Condition | Status | Decision | Reason |
|---|---|---|---|
| safety/legal risk | `urgent` | `needs_approval` | `safety_or_legal_risk` |
| unknown intake type | `rejected` | `deny` | `unknown_intake_type` |
| required fields absent | `awaiting_input` | `deny` | `missing_required_fields` |
| unknown action | `rejected` | `deny` | `action_not_configured` |
| action not allowed | `rejected` | `deny` | `action_not_allowed` |
| configured approval action or `send_reply` | `pending_approval` | `needs_approval` | `approval_required` |
| complete allowed action | `ready` | `allow` | `action_allowed` |

Missing field names must be sorted in the outcome. `RoutingAssessment.present_fields` is treated as a set; the router does not parse or mutate message text.

## Tasks

### Task 1: Add the YAML dependency and package skeleton

**Files:** `pyproject.toml`, `app/tenants/__init__.py`, `app/tenants/config.py`, `app/tenants/loader.py`, `app/tenants/routing.py`, `tests/test_tenant_config.py`

1. Add `"PyYAML>=6.0"` to the runtime `dependencies` list in `pyproject.toml`. Keep the existing dependency order and do not add an unneeded YAML framework.
2. Create the package files with imports only where needed. Do not import FastAPI or SQLAlchemy in this package.
3. Add the first failing test to `tests/test_tenant_config.py`:

   ```python
   from app.tenants.config import FieldType, TenantConfig


   def test_tenant_config_accepts_a_minimal_strict_profile() -> None:
       config = TenantConfig.model_validate(
           {
               "slug": "acme",
               "display_name": "Acme",
               "intake_types": [
                   {
                       "name": "request",
                       "description": "A request",
                       "required_fields": ["summary"],
                   }
               ],
               "fields": {
                   "summary": {"type": "short_text", "label": "Summary"},
               },
               "action_policy": {
                   "create_case": {"allowed": True, "requires_approval": False},
                   "send_reply": {"allowed": True, "requires_approval": True},
               },
               "routing": {
                   "outcome_names": [
                       "urgent",
                       "awaiting_input",
                       "pending_approval",
                       "ready",
                       "rejected",
                   ],
                   "always_approval_actions": ["send_reply"],
               },
           }
       )

       assert config.fields["summary"].type is FieldType.SHORT_TEXT
       assert config.intake_types[0].required_fields == ["summary"]
   ```

4. Run the focused test from the repository root:

   ```powershell
   pytest tests/test_tenant_config.py -q
   ```

   Expected result before implementation: collection fails with `ModuleNotFoundError: No module named 'app.tenants'` (or PyYAML is unavailable until the editable install is refreshed).

### Task 2: Implement strict configuration models

**Files:** `app/tenants/config.py`, `tests/test_tenant_config.py`

1. Implement the enums and models below. Use Pydantic v2 `@field_validator`/`@model_validator`; do not put validation in the YAML examples or in the router.

   ```python
   from enum import Enum
   from typing import Self

   from pydantic import (
       BaseModel,
       ConfigDict,
       Field,
       StrictBool,
       StrictStr,
       field_validator,
       model_validator,
   )


   class StrictModel(BaseModel):
       model_config = ConfigDict(extra="forbid")


   class FieldType(str, Enum):
       SHORT_TEXT = "short_text"
       LONG_TEXT = "long_text"
       EMAIL = "email"
       PHONE = "phone"
       ADDRESS = "address"
       DATE_TIME = "date_time"
       NUMBER = "number"
       BOOLEAN = "boolean"
       SINGLE_SELECT = "single_select"
       MULTI_SELECT = "multi_select"


   class RoutingStatus(str, Enum):
       URGENT = "urgent"
       AWAITING_INPUT = "awaiting_input"
       PENDING_APPROVAL = "pending_approval"
       READY = "ready"
       REJECTED = "rejected"


   class RoutingDecision(str, Enum):
       ALLOW = "allow"
       NEEDS_APPROVAL = "needs_approval"
       DENY = "deny"


   class FieldDefinition(StrictModel):
       type: FieldType
       label: StrictStr = Field(min_length=1)
       options: list[StrictStr] | None = None

       @model_validator(mode="after")
       def validate_options(self) -> Self:
           selectable = self.type in {FieldType.SINGLE_SELECT, FieldType.MULTI_SELECT}
           if selectable and (not self.options or len(set(self.options)) != len(self.options)):
               raise ValueError("select fields require unique non-empty options")
           if not selectable and self.options is not None:
               raise ValueError("options are allowed only for select fields")
           return self


   class IntakeTypeConfig(StrictModel):
       name: StrictStr = Field(min_length=1)
       description: StrictStr = Field(min_length=1)
       required_fields: list[StrictStr] = Field(default_factory=list)

       @field_validator("required_fields")
       @classmethod
       def validate_unique_required_fields(cls, value: list[str]) -> list[str]:
           if len(value) != len(set(value)):
               raise ValueError("required_fields must contain unique names")
           return value


   class ActionRule(StrictModel):
       allowed: StrictBool
       requires_approval: StrictBool


   class RoutingConfig(StrictModel):
       outcome_names: list[RoutingStatus]
       always_approval_actions: list[StrictStr]

       @model_validator(mode="after")
       def validate_routing_catalog(self) -> Self:
           expected = set(RoutingStatus)
           if set(self.outcome_names) != expected or len(self.outcome_names) != len(expected):
               raise ValueError("outcome_names must contain each supported routing status exactly once")
           if "send_reply" not in self.always_approval_actions:
               raise ValueError("always_approval_actions must include send_reply")
           if len(self.always_approval_actions) != len(set(self.always_approval_actions)):
               raise ValueError("always_approval_actions must contain unique names")
           return self


   class TenantConfig(StrictModel):
       slug: StrictStr = Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
       display_name: StrictStr = Field(min_length=1)
       intake_types: list[IntakeTypeConfig] = Field(min_length=1)
       fields: dict[str, FieldDefinition] = Field(min_length=1)
       action_policy: dict[StrictStr, ActionRule] = Field(min_length=1)
       routing: RoutingConfig

       @model_validator(mode="after")
       def validate_references(self) -> Self:
           names = [intake.name for intake in self.intake_types]
           if len(names) != len(set(names)):
               raise ValueError("intake_types must contain unique names")
           field_names = set(self.fields)
           missing = sorted(
               {
                   field
                   for intake in self.intake_types
                   for field in intake.required_fields
                   if field not in field_names
               }
           )
           if missing:
               raise ValueError(f"required_fields reference unknown fields: {', '.join(missing)}")
           action_names = set(self.action_policy)
           missing_actions = sorted(
               set(self.routing.always_approval_actions) - action_names
           )
           if missing_actions:
               raise ValueError(
                   "always_approval_actions reference unknown actions: "
                   + ", ".join(missing_actions)
               )
           return self
   ```

2. Add validation tests for every cross-field contract:

   - `pytest.raises(ValidationError)` for `type: made_up`.
   - `pytest.raises(ValidationError)` for duplicate `intake_types` names.
   - `pytest.raises(ValidationError)` for an undeclared required field.
   - `pytest.raises(ValidationError)` for select fields with no options and non-select fields with options.
   - `pytest.raises(ValidationError)` for a duplicate option and a missing `send_reply` approval action.
   - `pytest.raises(ValidationError)` for an extra root key such as `tenant_id`.

3. Run the focused tests. Expected result after implementation: all model tests pass; no database or network service is required.

   ```powershell
   pip install -e ".[dev]"
   pytest tests/test_tenant_config.py -q
   ```

### Task 3: Implement safe YAML loading and errors

**Files:** `app/tenants/loader.py`, `app/tenants/__init__.py`, `tests/test_tenant_config.py`

1. Add loader tests before implementing the loader:

   ```python
   from pathlib import Path

   import pytest

   from app.tenants.loader import TenantConfigError, load_tenant_config, parse_tenant_config


   def test_parse_tenant_config_wraps_yaml_syntax_errors() -> None:
       with pytest.raises(TenantConfigError, match=r"broken.yaml: invalid YAML"):
           parse_tenant_config("slug: [", source="broken.yaml")


   def test_load_tenant_config_wraps_validation_errors_with_path(tmp_path: Path) -> None:
       path = tmp_path / "invalid.yaml"
       path.write_text("slug: bad profile\n", encoding="utf-8")

       with pytest.raises(TenantConfigError, match=r"invalid\.yaml: invalid tenant config"):
           load_tenant_config(path)


   def test_parse_tenant_config_rejects_a_non_mapping_root() -> None:
       with pytest.raises(TenantConfigError, match="root must be a mapping"):
           parse_tenant_config("- item\n", source="list.yaml")
   ```

2. Implement `app/tenants/loader.py` with these semantics:

   ```python
   from pathlib import Path

   import yaml
   from pydantic import ValidationError
   from yaml import YAMLError

   from app.tenants.config import TenantConfig


   class TenantConfigError(ValueError):
       """A source file cannot become a valid tenant configuration."""


   def parse_tenant_config(content: str, *, source: str = "<string>") -> TenantConfig:
       try:
           raw = yaml.safe_load(content)
       except YAMLError as error:
           raise TenantConfigError(f"{source}: invalid YAML: {error}") from error
       if not isinstance(raw, dict):
           raise TenantConfigError(f"{source}: root must be a mapping")
       try:
           return TenantConfig.model_validate(raw)
       except ValidationError as error:
           details = "; ".join(
               f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}"
               for item in error.errors()
           )
           raise TenantConfigError(f"{source}: invalid tenant config: {details}") from error


   def load_tenant_config(path: Path) -> TenantConfig:
       try:
           content = path.read_text(encoding="utf-8")
       except OSError as error:
           raise TenantConfigError(f"{path}: cannot read configuration: {error}") from error
       return parse_tenant_config(content, source=str(path))
   ```

3. Export `TenantConfigError`, `load_tenant_config`, `parse_tenant_config`, the config models, and the router types from `app/tenants/__init__.py` without importing application modules.
4. Run loader and model tests:

   ```powershell
   pytest tests/test_tenant_config.py -q
   ```

   Expected result: all parsing and validation tests pass, with no unsafe YAML constructors used.

### Task 4: Add the three version-controlled tenant profiles

**Files:** `examples/freight-broker.yaml`, `examples/repair-service.yaml`, `examples/language-school.yaml`, `tests/test_tenant_config.py`

1. Create the `examples/` directory and add complete profiles. Each file must contain all five routing statuses and `send_reply` in `always_approval_actions`.
2. `freight-broker.yaml` must include these intake types and fields:

   - Intake types: `load_request`, `rate_confirmation`, `invoice_query`, `other`.
   - Required `load_request` fields: `origin`, `destination`, `equipment`, `pickup_window`, `commodity`, `contact`.
   - Optional `rate_hint` field.
   - Rate-confirmation fields `quoted_rate` and `valid_until` so phase 9 can extend the profile without renaming the catalog.
   - `equipment` as `single_select` with `dry_van`, `reefer`, and `flatbed` options.

3. `repair-service.yaml` must include at least `service_request` and `warranty_claim`; `language-school.yaml` must include at least `course_inquiry` and `placement_request`. Use the same field-type catalog and action policy shape; keep their fields domain-specific and non-identical to freight.
4. Write the complete profile contents as follows (the repair and school field names are intentionally different from freight):

   ```yaml
   # examples/freight-broker.yaml
   slug: freight-broker
   display_name: Freight broker
   intake_types:
     - name: load_request
       description: A request to quote or arrange a freight load.
       required_fields: [origin, destination, equipment, pickup_window, commodity, contact]
     - name: rate_confirmation
       description: A carrier rate confirmation document or message.
       required_fields: [origin, destination, quoted_rate, valid_until]
     - name: invoice_query
       description: A question about a freight invoice.
       required_fields: [invoice_number, contact]
     - name: other
       description: An inbound message that does not match a known freight intake type.
       required_fields: []
   fields:
     origin: {type: address, label: Origin}
     destination: {type: address, label: Destination}
     equipment:
       type: single_select
       label: Equipment
       options: [dry_van, reefer, flatbed]
     pickup_window: {type: date_time, label: Pickup window}
     commodity: {type: short_text, label: Commodity}
     rate_hint: {type: number, label: Rate hint}
     contact: {type: email, label: Contact email}
     quoted_rate: {type: number, label: Quoted rate}
     valid_until: {type: date_time, label: Quote valid until}
     invoice_number: {type: short_text, label: Invoice number}
   action_policy:
     create_case: {allowed: true, requires_approval: false}
     send_reply: {allowed: true, requires_approval: true}
     close_case: {allowed: true, requires_approval: true}
     flag_for_review: {allowed: true, requires_approval: false}
   routing:
     outcome_names: [urgent, awaiting_input, pending_approval, ready, rejected]
     always_approval_actions: [send_reply, close_case]
   ```

   ```yaml
   # examples/repair-service.yaml
   slug: repair-service
   display_name: Repair service
   intake_types:
     - name: service_request
       description: A request to diagnose or repair an asset.
       required_fields: [customer_name, contact, issue, asset_type]
     - name: warranty_claim
       description: A request to assess a warranty claim.
       required_fields: [customer_name, contact, issue, serial_number]
   fields:
     customer_name: {type: short_text, label: Customer name}
     contact: {type: email, label: Customer email}
     issue: {type: long_text, label: Issue description}
     asset_type:
       type: single_select
       label: Asset type
       options: [appliance, vehicle, equipment]
     preferred_time: {type: date_time, label: Preferred appointment}
     serial_number: {type: short_text, label: Serial number}
   action_policy:
     create_case: {allowed: true, requires_approval: false}
     send_reply: {allowed: true, requires_approval: true}
     close_case: {allowed: true, requires_approval: true}
     flag_for_review: {allowed: true, requires_approval: false}
   routing:
     outcome_names: [urgent, awaiting_input, pending_approval, ready, rejected]
     always_approval_actions: [send_reply, close_case]
   ```

   ```yaml
   # examples/language-school.yaml
   slug: language-school
   display_name: Language school
   intake_types:
     - name: course_inquiry
       description: A question about available language courses.
       required_fields: [student_name, contact, language]
     - name: placement_request
       description: A request to arrange a language placement assessment.
       required_fields: [student_name, contact, language, level]
   fields:
     student_name: {type: short_text, label: Student name}
     contact: {type: email, label: Student email}
     language:
       type: single_select
       label: Target language
       options: [english, ukrainian, german, polish]
     level:
       type: single_select
       label: Current level
       options: [beginner, intermediate, advanced]
     preferred_schedule: {type: short_text, label: Preferred schedule}
     goals: {type: long_text, label: Learning goals}
   action_policy:
     create_case: {allowed: true, requires_approval: false}
     send_reply: {allowed: true, requires_approval: true}
     close_case: {allowed: true, requires_approval: true}
     flag_for_review: {allowed: true, requires_approval: false}
   routing:
     outcome_names: [urgent, awaiting_input, pending_approval, ready, rejected]
     always_approval_actions: [send_reply, close_case]
   ```

5. Add a parameterized test that loads all three files with `load_tenant_config`, asserts their slugs, and verifies that `freight-broker` contains the four required intake names and all six required `load_request` fields.
6. Add a test that writes a temporary YAML document containing `type: made_up`, calls `load_tenant_config` on that path, and confirms it raises `TenantConfigError` rather than silently accepting it. This protects the documented failure mode without adding a second loader.
7. Run the example tests:

   ```powershell
   pytest tests/test_tenant_config.py -q
   ```

   Expected result: all three profiles load successfully and the malformed fixture fails with an actionable error.

### Task 5: Implement deterministic routing

**Files:** `app/tenants/routing.py`, `app/tenants/__init__.py`, `tests/test_tenant_config.py`

1. Add failing routing tests using a small in-test `TenantConfig` fixture or the freight profile:

   - A risk flag returns `RoutingStatus.URGENT`, `RoutingDecision.NEEDS_APPROVAL`, no missing fields, and reason `safety_or_legal_risk`, even if the action is unknown.
   - Unknown intake type returns `REJECTED`/`DENY` with `unknown_intake_type`.
   - A `load_request` without `destination` and `equipment` returns `AWAITING_INPUT`/`DENY` and `("destination", "equipment")` in sorted order.
   - A complete `load_request` with allowed `create_case` returns `READY`/`ALLOW`.
   - `send_reply` returns `PENDING_APPROVAL`/`NEEDS_APPROVAL` even when its rule is allowed.
   - An unknown action and an explicitly disallowed action return `REJECTED`/`DENY` with their distinct reasons.

2. Implement `TenantRouter.route` using the exact precedence from the contract. Use a dictionary lookup for intake and action rules; do not add recursive logic, callbacks, `eval`, LLM calls, or database access.

   ```python
   from dataclasses import dataclass

   from app.tenants.config import (
       RoutingDecision,
       RoutingStatus,
       TenantConfig,
   )


   @dataclass(frozen=True, slots=True)
   class RoutingAssessment:
       intake_type: str
       present_fields: frozenset[str]
       contains_safety_or_legal_risk: bool
       requested_action: str


   @dataclass(frozen=True, slots=True)
   class RoutingOutcome:
       status: RoutingStatus
       decision: RoutingDecision
       missing_required_fields: tuple[str, ...]
       reason: str


   class TenantRouter:
       def __init__(self, config: TenantConfig) -> None:
           self.config = config
           self._intake_types = {item.name: item for item in config.intake_types}

       def route(self, assessment: RoutingAssessment) -> RoutingOutcome:
           if assessment.contains_safety_or_legal_risk:
               return RoutingOutcome(
                   RoutingStatus.URGENT,
                   RoutingDecision.NEEDS_APPROVAL,
                   (),
                   "safety_or_legal_risk",
               )

           intake = self._intake_types.get(assessment.intake_type)
           if intake is None:
               return RoutingOutcome(
                   RoutingStatus.REJECTED,
                   RoutingDecision.DENY,
                   (),
                   "unknown_intake_type",
               )

           missing = tuple(sorted(set(intake.required_fields) - assessment.present_fields))
           if missing:
               return RoutingOutcome(
                   RoutingStatus.AWAITING_INPUT,
                   RoutingDecision.DENY,
                   missing,
                   "missing_required_fields",
               )

           rule = self.config.action_policy.get(assessment.requested_action)
           if rule is None:
               return RoutingOutcome(
                   RoutingStatus.REJECTED,
                   RoutingDecision.DENY,
                   (),
                   "action_not_configured",
               )
           if not rule.allowed:
               return RoutingOutcome(
                   RoutingStatus.REJECTED,
                   RoutingDecision.DENY,
                   (),
                   "action_not_allowed",
               )
           if (
               assessment.requested_action in self.config.routing.always_approval_actions
               or rule.requires_approval
           ):
               return RoutingOutcome(
                   RoutingStatus.PENDING_APPROVAL,
                   RoutingDecision.NEEDS_APPROVAL,
                   (),
                   "approval_required",
               )
           return RoutingOutcome(
               RoutingStatus.READY,
               RoutingDecision.ALLOW,
               (),
               "action_allowed",
           )
   ```

3. Run the routing tests and then the complete unit suite:

   ```powershell
   pytest tests/test_tenant_config.py tests/test_case_service.py tests/test_auth.py tests/test_health.py -q
   ```

   Expected result: routing is covered without PostgreSQL, and existing phase-2 tests remain green.

### Task 6: Populate `AGENTS.md` with the current project contract

**File:** `AGENTS.md`

1. Replace the template placeholders with current facts from the repository:

   - Project overview: IntegraIntake is a policy-governed, multi-tenant intake engine for turning inbound content into auditable cases.
   - Stack: Python 3.12+, FastAPI, PostgreSQL, SQLAlchemy 2 async, Alembic, Pydantic v2, PyYAML, pytest, Docker Compose.
   - Architecture: API/auth/domain persistence today; tenant configuration boundary under `app/tenants/`; future agent/worker layers are not yet implemented.
   - Setup commands from `README.md`: virtual environment, `pip install -e ".[dev]"`, `docker compose up -d db`, `alembic upgrade head`, `uvicorn app.main:app --reload`, and `pytest -q`.
   - Current endpoints: `GET /health`, `POST /v1/cases`, and `GET /v1/cases/{case_id}` with `X-API-Key` tenant authentication.
   - Current test split: pure unit tests under `tests/`, optional PostgreSQL integration tests gated by `DATABASE_URL`.
   - Configuration variables from `app/core/config.py`: `DATABASE_URL`, `LLM_API_KEY`, `LLM_BASE_URL`, and `APP_ENV`; YAML profiles are under `examples/` and loaded with `load_tenant_config`.
   - Pitfalls: never trust body `tenant_id`; always use tenant-scoped repository predicates; never persist raw API keys; do not use unsafe YAML loading; create new Alembic revisions instead of rewriting applied migrations.
   - Typical tasks: add a case endpoint, add a migration, add a tenant profile, or change routing with unit tests.

2. Remove all placeholder angle-bracket text and all references to a learning/tutorial process. Keep the document focused on operating and extending the product.
3. Do not alter the existing `.gitignore` entry for `AGENTS.md`; the requested file must exist in the workspace even if the current repository policy ignores it.

### Task 7: Final verification

**Files:** none (read-only verification)

1. Run the phase-specific suite:

   ```powershell
   pytest tests/test_tenant_config.py -q
   ```

   Expected result: all phase-3 tests pass.

2. Run the complete suite:

   ```powershell
   pytest -q
   ```

   Expected result: unit tests pass; PostgreSQL tests may be skipped when `DATABASE_URL` is unset, matching the existing fixture contract.

3. Check formatting and accidental whitespace:

   ```powershell
   git diff --check
   ```

   Expected result: no output and exit code 0.

4. Inspect the final changed-file list and confirm no secrets, generated files, phase-2 database changes, unsafe YAML loader calls, or product-facing learning references were introduced:

   ```powershell
   git status --short
   git diff -- pyproject.toml app examples tests AGENTS.md
   ```

### Task 8: Run project quality checks

**Files:** none (read-only verification)

1. Install the declared development dependencies and run the complete configured test command:

   ```powershell
   pip install -e ".[dev]"
   pytest -q
   ```

   Expected result: all runnable tests pass; PostgreSQL integration tests may be skipped when `DATABASE_URL` is not configured.

2. Run the repository whitespace check:

   ```powershell
   git diff --check
   ```

   Expected result: exit code 0 and no output. There is no separate linter, formatter, or type-check command configured in `pyproject.toml`.

### Task 9: Review the phase-3 diff

**Files:** none (review only)

1. Call the `code-review` skill on the changes made by this plan. Give it the complete phase-3 diff and the phase-3 specification/plan context.
2. The review must check safe YAML loading, strict boundary validation, routing precedence, test coverage, accidental tenant/security regressions, and adherence to the repository style.
3. Do not make corrections during the review. Discuss findings with the user first, then create a follow-up fix task only if the user approves it.

## Plan self-review

- Spec goal and scope map to Tasks 1–4 and exclude changes in Task 6/7.
- Strict model rules map to Task 2, including unknown field types, duplicate intake names, select options, required-field references, and extra-key rejection.
- Safe loading and path-aware errors map to Task 3.
- All three profiles and freight minimum fields map to Task 4.
- Routing precedence and every outcome/decision pair map to Task 5.
- Repository documentation and the no-learning-reference constraint map to Task 6.
- Existing regression and diff verification map to Task 7.
- Quality commands and the absence of additional configured tooling map to Task 8.
- The requested review gate maps to Task 9 and is explicitly read-only.
- There are no placeholder steps, unbounded refactors, runtime LLM calls, database writes, or unspecified external dependencies.
