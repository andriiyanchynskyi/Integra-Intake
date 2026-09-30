"""Bounded document normalization contracts and legacy decoders."""

from app.documents.legacy import (
    LegacyNormalizedRateConfirmationDocument,
    NormalizedRateConfirmationDocument,
    decode_document_snapshot,
    decode_legacy_normalized_document,
)
from app.documents.models import (
    MAX_DOCUMENT_BYTES,
    MAX_DOCUMENT_PAGES,
    MAX_DOCUMENT_TEXT_CHARS,
    DocumentExtractionError,
    DocumentInput,
    DocumentMediaType,
    NormalizedDocument,
    RateConfirmationDocumentInput,
)
from app.documents.registry import (
    BUILTIN_DOCUMENT_REGISTRY,
    DocumentCapabilityUnavailable,
    DocumentNormalizerCapability,
    DocumentNormalizerRegistry,
)


def __getattr__(name: str) -> object:
    if name == "DocumentNormalizer":
        from app.documents.normalizer import DocumentNormalizer

        return DocumentNormalizer
    raise AttributeError(name)


__all__ = [
    "BUILTIN_DOCUMENT_REGISTRY",
    "DocumentCapabilityUnavailable",
    "DocumentExtractionError",
    "DocumentInput",
    "DocumentMediaType",
    "DocumentNormalizer",
    "DocumentNormalizerCapability",
    "DocumentNormalizerRegistry",
    "LegacyNormalizedRateConfirmationDocument",
    "MAX_DOCUMENT_BYTES",
    "MAX_DOCUMENT_PAGES",
    "MAX_DOCUMENT_TEXT_CHARS",
    "NormalizedDocument",
    "NormalizedRateConfirmationDocument",
    "RateConfirmationDocumentInput",
    "decode_document_snapshot",
    "decode_legacy_normalized_document",
]
