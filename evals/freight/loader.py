"""Safe loading of the versioned freight corpus and document manifest."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Mapping

import yaml
from pydantic import ValidationError

from app.documents import DocumentMediaType

from .models import (
    ADVERSARIAL_COUNTS,
    CORE_COUNTS,
    FreightEvalCase,
    FreightEvalDataset,
)


_SAFE_FIXTURE_ID = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class FreightDatasetError(ValueError):
    """A stable, source-safe dataset or manifest loading failure."""


@dataclass(frozen=True, slots=True)
class ResolvedDocumentFixture:
    id: str
    path: Path
    media_type: DocumentMediaType


@dataclass(frozen=True, slots=True)
class LoadedFreightDataset:
    dataset: FreightEvalDataset
    document_fixtures: Mapping[str, ResolvedDocumentFixture]

    @property
    def cases(self) -> tuple[FreightEvalCase, ...]:
        return self.dataset.cases


def _error(
    dataset_path: Path,
    reason: str,
    *,
    case_id: str | None = None,
) -> FreightDatasetError:
    suffix = f" case={case_id}" if case_id else ""
    return FreightDatasetError(
        f"freight dataset {dataset_path.name}: {reason}{suffix}"
    )


def _read_yaml(path: Path, *, dataset_path: Path, reason: str) -> object:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise _error(dataset_path, reason) from exc


def _manifest_fixtures(
    manifest_path: Path,
    *,
    dataset_path: Path,
) -> dict[str, ResolvedDocumentFixture]:
    raw = _read_yaml(
        manifest_path,
        dataset_path=dataset_path,
        reason="invalid_document_reference",
    )
    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise _error(dataset_path, "invalid_document_reference")
    entries = raw.get("documents")
    if not isinstance(entries, list):
        raise _error(dataset_path, "invalid_document_reference")

    root = manifest_path.parent.resolve()
    fixtures: dict[str, ResolvedDocumentFixture] = {}
    resolved_files: set[Path] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise _error(dataset_path, "invalid_document_reference")
        identifier = entry.get("id")
        filename = entry.get("filename")
        media_type_value = entry.get("media_type")
        if (
            not isinstance(identifier, str)
            or not _SAFE_FIXTURE_ID.fullmatch(identifier)
            or not isinstance(filename, str)
            or not isinstance(media_type_value, str)
        ):
            raise _error(dataset_path, "invalid_document_reference")
        if identifier in fixtures:
            raise _error(dataset_path, "invalid_document_reference")
        filename_path = Path(filename)
        if filename_path.is_absolute() or ".." in filename_path.parts:
            raise _error(dataset_path, "invalid_document_reference")
        try:
            media_type = DocumentMediaType(media_type_value)
        except ValueError as exc:
            raise _error(dataset_path, "invalid_document_reference") from exc
        fixture_path = (root / filename_path).resolve()
        try:
            contained = fixture_path.is_relative_to(root)
        except AttributeError:
            contained = str(fixture_path).startswith(str(root))
        if not contained or not fixture_path.is_file():
            raise _error(dataset_path, "invalid_document_reference")
        if fixture_path in resolved_files:
            raise _error(dataset_path, "invalid_document_reference")
        resolved_files.add(fixture_path)
        fixtures[identifier] = ResolvedDocumentFixture(
            id=identifier,
            path=fixture_path,
            media_type=media_type,
        )
    return fixtures


def _raw_case_id(raw_case: object) -> str | None:
    if isinstance(raw_case, dict) and isinstance(raw_case.get("id"), str):
        return raw_case["id"]
    return None


def _raw_document_refs(raw: object) -> list[tuple[str | None, object]]:
    if not isinstance(raw, dict) or not isinstance(raw.get("cases"), list):
        return []
    references: list[tuple[str | None, object]] = []
    for raw_case in raw["cases"]:
        if not isinstance(raw_case, dict):
            continue
        source = raw_case.get("source")
        if not isinstance(source, dict):
            continue
        kind = source.get("kind")
        if kind == "document":
            references.append((_raw_case_id(raw_case), source.get("fixture_id")))
        elif kind == "inbound_webhook":
            reference = source.get("document_fixture_id")
            if reference is not None:
                references.append((_raw_case_id(raw_case), reference))
    return references


def _validate_category_counts(dataset: FreightEvalDataset, dataset_path: Path) -> None:
    if len(dataset.cases) != sum(CORE_COUNTS.values()) + sum(ADVERSARIAL_COUNTS.values()):
        raise _error(dataset_path, "invalid_counts")
    core = Counter(
        case.category for case in dataset.cases if not case.metadata.adversarial
    )
    adversarial = Counter(
        case.category for case in dataset.cases if case.metadata.adversarial
    )
    if dict(core) != CORE_COUNTS or dict(adversarial) != ADVERSARIAL_COUNTS:
        raise _error(dataset_path, "invalid_counts")


def load_freight_dataset(
    dataset_path: Path,
    *,
    document_manifest_path: Path,
) -> LoadedFreightDataset:
    """Load one closed dataset and its contained, declared document fixtures."""

    dataset_path = Path(dataset_path)
    document_manifest_path = Path(document_manifest_path)
    fixtures = _manifest_fixtures(
        document_manifest_path,
        dataset_path=dataset_path,
    )
    raw = _read_yaml(dataset_path, dataset_path=dataset_path, reason="invalid_schema")

    if not isinstance(raw, dict) or raw.get("version") != 1:
        raise _error(dataset_path, "invalid_schema")
    raw_cases = raw.get("cases")
    if not isinstance(raw_cases, list):
        raise _error(dataset_path, "invalid_schema")
    seen_ids: set[str] = set()
    for raw_case in raw_cases:
        case_id = _raw_case_id(raw_case)
        if case_id is not None:
            if case_id in seen_ids:
                raise _error(dataset_path, "duplicate_case_id", case_id=case_id)
            seen_ids.add(case_id)

    for case_id, reference in _raw_document_refs(raw):
        if not isinstance(reference, str) or not _SAFE_FIXTURE_ID.fullmatch(reference):
            raise _error(dataset_path, "invalid_document_reference", case_id=case_id)
        if reference not in fixtures:
            raise _error(dataset_path, "invalid_document_reference", case_id=case_id)

    try:
        dataset = FreightEvalDataset.model_validate(raw)
    except ValidationError as exc:
        case_id = None
        if isinstance(raw_cases, list):
            for item in raw_cases:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    case_id = item["id"]
                    break
        raise _error(dataset_path, "invalid_schema", case_id=case_id) from exc
    _validate_category_counts(dataset, dataset_path)
    return LoadedFreightDataset(dataset=dataset, document_fixtures=fixtures)


__all__ = [
    "FreightDatasetError",
    "LoadedFreightDataset",
    "ResolvedDocumentFixture",
    "load_freight_dataset",
]
