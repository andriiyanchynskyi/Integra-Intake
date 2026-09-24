"""Pure, bounded plaintext and PDF normalization for rate confirmations."""

from __future__ import annotations

from hashlib import sha256
from io import BytesIO

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.documents.models import (
    MAX_DOCUMENT_BYTES,
    MAX_DOCUMENT_PAGES,
    MAX_DOCUMENT_TEXT_CHARS,
    DocumentExtractionError,
    DocumentMediaType,
    NormalizedRateConfirmationDocument,
    RateConfirmationDocumentInput,
)


class DocumentNormalizer:
    """Normalize one internal document without I/O beyond PDF byte parsing."""

    def normalize(
        self, value: RateConfirmationDocumentInput
    ) -> NormalizedRateConfirmationDocument:
        raw = (
            value.text.encode("utf-8")
            if value.media_type is DocumentMediaType.TEXT
            else value.content
        )
        if raw is None:
            raise ValueError("document payload is unavailable")
        digest = sha256(raw).hexdigest()

        if len(raw) > MAX_DOCUMENT_BYTES:
            return self._error(
                value.media_type,
                digest,
                DocumentExtractionError.DOCUMENT_TOO_LARGE,
            )

        if value.media_type is DocumentMediaType.TEXT:
            normalized = self._normalize_text(value.text or "")
            if not normalized.strip():
                return self._error(
                    value.media_type,
                    digest,
                    DocumentExtractionError.DOCUMENT_TEXT_EMPTY,
                )
            if len(normalized) > MAX_DOCUMENT_TEXT_CHARS:
                return self._error(
                    value.media_type,
                    digest,
                    DocumentExtractionError.DOCUMENT_TEXT_TOO_LARGE,
                )
            return self._success(value.media_type, digest, normalized)

        try:
            reader = PdfReader(BytesIO(raw), strict=True)
            if reader.is_encrypted:
                return self._error(
                    value.media_type,
                    digest,
                    DocumentExtractionError.PDF_ENCRYPTED,
                )
            if len(reader.pages) > MAX_DOCUMENT_PAGES:
                return self._error(
                    value.media_type,
                    digest,
                    DocumentExtractionError.DOCUMENT_TOO_LARGE,
                )
            extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
        except (PdfReadError, OSError, ValueError, KeyError, IndexError):
            return self._error(
                value.media_type,
                digest,
                DocumentExtractionError.PDF_MALFORMED,
            )
        except Exception:
            # Provider-visible and persisted data must never contain parser
            # exception details. Treat unknown parser failures as malformed.
            return self._error(
                value.media_type,
                digest,
                DocumentExtractionError.PDF_MALFORMED,
            )

        normalized = self._normalize_text(extracted)
        if not normalized.strip():
            return self._error(
                value.media_type,
                digest,
                DocumentExtractionError.PDF_EMPTY,
            )
        if len(normalized) > MAX_DOCUMENT_TEXT_CHARS:
            return self._error(
                value.media_type,
                digest,
                DocumentExtractionError.DOCUMENT_TEXT_TOO_LARGE,
            )
        return self._success(value.media_type, digest, normalized)

    @staticmethod
    def _normalize_text(value: str) -> str:
        return value.replace("\r\n", "\n").replace("\r", "\n")

    @staticmethod
    def _success(
        media_type: DocumentMediaType,
        digest: str,
        text: str,
    ) -> NormalizedRateConfirmationDocument:
        return NormalizedRateConfirmationDocument(
            media_type=media_type,
            sha256=digest,
            text=text,
        )

    @staticmethod
    def _error(
        media_type: DocumentMediaType,
        digest: str,
        error: DocumentExtractionError,
    ) -> NormalizedRateConfirmationDocument:
        return NormalizedRateConfirmationDocument(
            media_type=media_type,
            sha256=digest,
            extraction_error=error,
        )


__all__ = ["DocumentNormalizer"]
