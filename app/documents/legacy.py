"""Narrow decoders for the version-1 freight document snapshot."""

from __future__ import annotations

from copy import deepcopy
from typing import Literal, Self

from pydantic import Field, StrictStr, field_validator, model_validator

from app.documents.models import (
    MAX_DOCUMENT_TEXT_CHARS,
    DocumentExtractionError,
    DocumentMediaType,
    NormalizedDocument,
    _StrictDocumentModel,
)


LEGACY_DOCUMENT_KIND = "rate_confirmation"
LEGACY_NORMALIZER_KEY = "bounded_text_pdf"
LEGACY_NORMALIZER_VERSION = 1
LEGACY_PARSER_VERSION = "rate_confirmation_document.v1"


class LegacyNormalizedRateConfirmationDocument(_StrictDocumentModel):
    """Read-only shape of the persisted version-1 freight snapshot."""

    kind: Literal["rate_confirmation"] = "rate_confirmation"
    media_type: DocumentMediaType
    sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    parser_version: Literal["rate_confirmation_document.v1"] = (
        "rate_confirmation_document.v1"
    )
    text: StrictStr | None = None
    extraction_error: DocumentExtractionError | None = None

    @field_validator("media_type", mode="before")
    @classmethod
    def parse_media_type(cls, value: object) -> object:
        if isinstance(value, DocumentMediaType):
            return value
        if isinstance(value, str):
            try:
                return DocumentMediaType(value)
            except ValueError:
                return value
        return value

    @field_validator("extraction_error", mode="before")
    @classmethod
    def parse_extraction_error(cls, value: object) -> object:
        if isinstance(value, DocumentExtractionError) or value is None:
            return value
        if isinstance(value, str):
            try:
                return DocumentExtractionError(value)
            except ValueError:
                return value
        return value

    @field_validator("text")
    @classmethod
    def validate_normalized_text(cls, value: str | None) -> str | None:
        if value is not None and (
            not value.strip() or len(value) > MAX_DOCUMENT_TEXT_CHARS
        ):
            raise ValueError("normalized document text is outside the allowed bounds")
        return value

    @model_validator(mode="after")
    def require_text_or_error(self) -> Self:
        if (self.text is None) == (self.extraction_error is None):
            raise ValueError("exactly one normalized text or extraction error is required")
        return self


def decode_legacy_normalized_document(value: object) -> NormalizedDocument:
    """Decode a v1 snapshot without mutating its stored mapping."""

    legacy = LegacyNormalizedRateConfirmationDocument.model_validate(deepcopy(value))
    return NormalizedDocument(
        document_kind=LEGACY_DOCUMENT_KIND,
        target_intake_type=LEGACY_DOCUMENT_KIND,
        media_type=legacy.media_type,
        sha256=legacy.sha256,
        normalizer_key=LEGACY_NORMALIZER_KEY,
        normalizer_version=LEGACY_NORMALIZER_VERSION,
        text=legacy.text,
        extraction_error=legacy.extraction_error,
    )


def decode_document_snapshot(value: object) -> NormalizedDocument:
    """Decode either a current v2 snapshot or the isolated legacy v1 shape."""

    if isinstance(value, dict) and value.get("snapshot_version") == 2:
        return NormalizedDocument.model_validate(deepcopy(value))
    return decode_legacy_normalized_document(value)


# Keep the old public import available to migration callers only.
NormalizedRateConfirmationDocument = LegacyNormalizedRateConfirmationDocument


__all__ = [
    "LEGACY_DOCUMENT_KIND",
    "LEGACY_NORMALIZER_KEY",
    "LEGACY_NORMALIZER_VERSION",
    "LEGACY_PARSER_VERSION",
    "LegacyNormalizedRateConfirmationDocument",
    "NormalizedRateConfirmationDocument",
    "decode_document_snapshot",
    "decode_legacy_normalized_document",
]
