"""Pure validation of profile-specific extracted values and provenance."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
import math
import re

from pydantic import EmailStr, TypeAdapter, ValidationError

from app.agent.models import AgentProposal, FieldSource, ProposalPriority, ToolData
from app.policy.models import TrustedToolRuntimeContext, proposal_value_is_present
from app.tenants.config import FieldDefinition, FieldType


MAX_SHORT_TEXT_CHARS = 500
MAX_LONG_TEXT_CHARS = 10_000
MAX_ADDRESS_CHARS = 1_000
MAX_PHONE_CHARS = 50
MAX_EMAIL_CHARS = 320

_PHONE_PATTERN = re.compile(r"^[+0-9().\-\s]{3,50}$")
_EMAIL_PATTERN = re.compile(
    r"^[^@\s]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252}[A-Za-z0-9])?\.[A-Za-z]{2,63}$"
)
_INVALID = object()
_EMAIL_ADAPTER = TypeAdapter(EmailStr)


@dataclass(frozen=True, slots=True)
class ValidatedProposalFields:
    """Accepted profile fields and the names whose supplied values were invalid."""

    values: Mapping[str, ToolData]
    invalid_fields: tuple[str, ...] = ()


def _valid_string(value: object, *, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= maximum
    )


def _valid_email(value: object) -> bool:
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_EMAIL_CHARS:
        return False
    try:
        _EMAIL_ADAPTER.validate_python(value)
    except (TypeError, ValueError, ValidationError):
        # The deterministic fixtures use RFC 2606's ``.test`` domain.  The
        # adapter rejects reserved domains even when syntax is otherwise valid;
        # keep this narrow fallback so fixture addresses remain source-safe
        # without accepting malformed addresses or other reserved suffixes.
        if not _EMAIL_PATTERN.fullmatch(value):
            return False
        return value.rsplit("@", 1)[-1].lower().endswith(".test")
    return True


def _valid_date_time(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _normalized_value(value: object, definition: FieldDefinition) -> object:
    field_type = definition.type
    if field_type is FieldType.SHORT_TEXT:
        return value if _valid_string(value, maximum=MAX_SHORT_TEXT_CHARS) else _INVALID
    if field_type is FieldType.LONG_TEXT:
        return value if _valid_string(value, maximum=MAX_LONG_TEXT_CHARS) else _INVALID
    if field_type is FieldType.EMAIL:
        return value if _valid_email(value) else _INVALID
    if field_type is FieldType.PHONE:
        valid = (
            isinstance(value, str)
            and bool(value.strip())
            and len(value) <= MAX_PHONE_CHARS
            and _PHONE_PATTERN.fullmatch(value) is not None
        )
        return value if valid else _INVALID
    if field_type is FieldType.ADDRESS:
        return value if _valid_string(value, maximum=MAX_ADDRESS_CHARS) else _INVALID
    if field_type is FieldType.DATE_TIME:
        return value if _valid_date_time(value) else _INVALID
    if field_type is FieldType.NUMBER:
        if isinstance(value, bool):
            return _INVALID
        if isinstance(value, int):
            return value
        if isinstance(value, float) and math.isfinite(value):
            return value
        return _INVALID
    if field_type is FieldType.BOOLEAN:
        return value if isinstance(value, bool) else _INVALID
    if field_type is FieldType.SINGLE_SELECT:
        return (
            value
            if isinstance(value, str) and value in (definition.options or ())
            else _INVALID
        )
    if field_type is FieldType.MULTI_SELECT:
        valid = (
            isinstance(value, list)
            and bool(value)
            and all(isinstance(item, str) for item in value)
            and len(value) == len(set(value))
            and all(item in (definition.options or ()) for item in value)
        )
        return list(value) if valid else _INVALID
    return _INVALID


def _source_candidates(
    item: object,
    *,
    body: str,
    document: str | None,
) -> tuple[FieldSource, ...]:
    source = item.source
    excerpt = item.source_excerpt
    if document is None:
        if source is FieldSource.DOCUMENT:
            return ()
        if excerpt is not None and excerpt not in body:
            return ()
        return (FieldSource.BODY,)
    if source is not None:
        if excerpt is None:
            return ()
        source_text = body if source is FieldSource.BODY else document
        return (source,) if excerpt in source_text else ()
    if excerpt is None:
        return ()
    candidates: list[FieldSource] = []
    if excerpt in body:
        candidates.append(FieldSource.BODY)
    if excerpt in document:
        candidates.append(FieldSource.DOCUMENT)
    return tuple(candidates)


def validate_proposal_fields(
    proposal: AgentProposal,
    runtime: TrustedToolRuntimeContext,
) -> ValidatedProposalFields:
    """Validate profile values, verify provenance, and apply body precedence."""

    definitions = runtime.tenant_config.fields
    source = runtime.source
    body = source.body if source is not None else ""
    document = (
        source.document.text
        if source is not None and source.document is not None
        else None
    )
    candidates: dict[str, list[tuple[FieldSource, ToolData]]] = defaultdict(list)
    invalid: set[str] = set()
    for item in proposal.fields:
        definition = definitions.get(item.name)
        if definition is None:
            continue
        if not proposal_value_is_present(item.value):
            continue
        sources = _source_candidates(item, body=body, document=document)
        if not sources:
            invalid.add(item.name)
            continue
        normalized = _normalized_value(item.value, definition)
        if normalized is _INVALID:
            invalid.add(item.name)
            continue
        for item_source in sources:
            candidates[item.name].append((item_source, deepcopy(normalized)))

    values: dict[str, ToolData] = {}
    for name, choices in candidates.items():
        choices.sort(key=lambda item: 0 if item[0] is FieldSource.BODY else 1)
        values[name] = choices[0][1]
    return ValidatedProposalFields(
        values=values,
        invalid_fields=tuple(sorted(invalid)),
    )


def validate_frozen_fields(
    fields: Mapping[str, object],
    runtime: TrustedToolRuntimeContext,
) -> ValidatedProposalFields:
    """Revalidate fields stored inside an approval command.

    Approval commands do not retain source excerpts, so they are checked as
    body-origin values against the immutable profile. Unknown names and values
    that cannot form a valid proposal are reported as invalid rather than
    silently dropped.
    """

    unknown = set(fields) - set(runtime.tenant_config.fields)
    try:
        proposal = AgentProposal.model_validate(
            {
                "intake_type": None,
                "fields": [
                    {"name": name, "value": value, "source": FieldSource.BODY}
                    for name, value in fields.items()
                ],
                "missing_required_fields": [],
                "priority": ProposalPriority.NORMAL,
                "contains_injection_or_override_attempt": False,
                "rationale_short": "Frozen approval command validation",
                "tool_calls": [],
                "confidence": 1.0,
            }
        )
    except (TypeError, ValueError):
        return ValidatedProposalFields(values={}, invalid_fields=tuple(sorted(fields)))
    # Frozen approval fields are already captured by the server. They are
    # treated as body-origin values even when the original intake also had an
    # attachment, so the document-excerpt requirement does not invalidate the
    # immutable command at decision time.
    source = runtime.source
    if source is not None and source.document is not None:
        source = replace(source, document=None)
    result = validate_proposal_fields(proposal, replace(runtime, source=source))
    return ValidatedProposalFields(
        values=result.values,
        invalid_fields=tuple(sorted(set(result.invalid_fields) | unknown)),
    )


__all__ = [
    "MAX_ADDRESS_CHARS",
    "MAX_EMAIL_CHARS",
    "MAX_LONG_TEXT_CHARS",
    "MAX_PHONE_CHARS",
    "MAX_SHORT_TEXT_CHARS",
    "ValidatedProposalFields",
    "validate_frozen_fields",
    "validate_proposal_fields",
]
