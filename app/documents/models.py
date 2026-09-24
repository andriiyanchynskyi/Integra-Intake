"""Strict contracts for the Phase-9 rate-confirmation boundary."""

from __future__ import annotations

from enum import Enum
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictStr,
    field_validator,
    model_validator,
)


MAX_DOCUMENT_BYTES = 5 * 1024 * 1024
MAX_DOCUMENT_PAGES = 20
MAX_DOCUMENT_TEXT_CHARS = 100_000


class DocumentMediaType(str, Enum):
    TEXT = "text/plain"
    PDF = "application/pdf"


class DocumentExtractionError(str, Enum):
    UNSUPPORTED_MEDIA_TYPE = "unsupported_media_type"
    DOCUMENT_TOO_LARGE = "document_too_large"
    DOCUMENT_TEXT_TOO_LARGE = "document_text_too_large"
    PDF_MALFORMED = "pdf_malformed"
    PDF_ENCRYPTED = "pdf_encrypted"
    PDF_EMPTY = "pdf_empty"
    DOCUMENT_TEXT_EMPTY = "document_text_empty"


class _StrictDocumentModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class RateConfirmationDocumentInput(_StrictDocumentModel):
    """Internal input; exactly one bounded plaintext/PDF payload is allowed."""

    channel: StrictStr = Field(min_length=1, max_length=100)
    subject: StrictStr = Field(min_length=1, max_length=500)
    body: StrictStr = ""
    media_type: DocumentMediaType
    text: StrictStr | None = None
    content: bytes | None = None

    @field_validator("channel", "subject")
    @classmethod
    def require_non_blank_metadata(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must not be blank")
        return value

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

    @model_validator(mode="after")
    def validate_payload(self) -> Self:
        has_text = self.text is not None
        has_content = self.content is not None
        if has_text == has_content:
            raise ValueError("exactly one document payload is required")
        if self.media_type is DocumentMediaType.TEXT and not has_text:
            raise ValueError("text/plain requires text payload")
        if self.media_type is DocumentMediaType.PDF and not has_content:
            raise ValueError("application/pdf requires PDF bytes")
        if self.content == b"":
            raise ValueError("document content must not be empty")
        if self.text is not None and not self.text.strip():
            raise ValueError("document text must not be blank")
        return self


class NormalizedRateConfirmationDocument(_StrictDocumentModel):
    """JSON-safe trusted snapshot; original bytes are never represented here."""

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


__all__ = [
    "DocumentExtractionError",
    "DocumentMediaType",
    "MAX_DOCUMENT_BYTES",
    "MAX_DOCUMENT_PAGES",
    "MAX_DOCUMENT_TEXT_CHARS",
    "NormalizedRateConfirmationDocument",
    "RateConfirmationDocumentInput",
]
