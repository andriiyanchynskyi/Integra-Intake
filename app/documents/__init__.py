"""Bounded, internal freight-document normalization contracts."""

from app.documents.models import (
    MAX_DOCUMENT_BYTES,
    MAX_DOCUMENT_PAGES,
    MAX_DOCUMENT_TEXT_CHARS,
    DocumentExtractionError,
    DocumentMediaType,
    NormalizedRateConfirmationDocument,
    RateConfirmationDocumentInput,
)
from app.documents.normalizer import DocumentNormalizer

__all__ = [
    "DocumentExtractionError",
    "DocumentMediaType",
    "DocumentNormalizer",
    "MAX_DOCUMENT_BYTES",
    "MAX_DOCUMENT_PAGES",
    "MAX_DOCUMENT_TEXT_CHARS",
    "NormalizedRateConfirmationDocument",
    "RateConfirmationDocumentInput",
]
