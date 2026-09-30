"""Pure, bounded plaintext and PDF normalization for trusted bindings."""

from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
from io import BytesIO
import time
from uuid import uuid4

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from app.documents.models import (
    MAX_DOCUMENT_BYTES,
    MAX_DOCUMENT_PAGES,
    MAX_DOCUMENT_TEXT_CHARS,
    DocumentExtractionError,
    DocumentInput,
    DocumentMediaType,
    NormalizedDocument,
)
from app.observability import (
    Component,
    EventLevel,
    EventName,
    NULL_OBSERVER,
    ObservationContext,
    ObservationEvent,
    Observer,
    OutcomeCode,
    safe_emit,
)
from app.tenants.identifiers import SafeIdentifier


class DocumentNormalizer:
    """Normalize one internal document without I/O beyond PDF byte parsing."""

    def __init__(
        self,
        *,
        observer: Observer = NULL_OBSERVER,
        context: ObservationContext | None = None,
        clock: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        self.observer = observer
        self.context = context
        self.clock = clock

    def normalize(
        self,
        value: DocumentInput,
        *,
        document_kind: SafeIdentifier,
        target_intake_type: SafeIdentifier,
        normalizer_key: SafeIdentifier,
        normalizer_version: int,
    ) -> NormalizedDocument:
        started_ns = self.clock()
        raw = (
            value.text.encode("utf-8")
            if value.media_type is DocumentMediaType.TEXT
            else value.content
        )
        result: NormalizedDocument | None = None
        try:
            result = self._normalize(
                value,
                document_kind=document_kind,
                target_intake_type=target_intake_type,
                normalizer_key=normalizer_key,
                normalizer_version=normalizer_version,
            )
            return result
        finally:
            context = self.context or ObservationContext(trace_id=uuid4())
            duration_ms = max(0, (self.clock() - started_ns) // 1_000_000)
            safe_emit(
                self.observer,
                ObservationEvent(
                    event=EventName.DOCUMENT_NORMALIZATION_COMPLETED,
                    trace_id=context.trace_id,
                    request_id=context.request_id,
                    tenant_id=context.tenant_id,
                    job_id=context.job_id,
                    component=Component.DOCUMENT,
                    outcome=(
                        OutcomeCode.SUCCESS
                        if result is not None and result.extraction_error is None
                        else OutcomeCode.FAILED
                    ),
                    level=(
                        EventLevel.INFO
                        if result is not None and result.extraction_error is None
                        else EventLevel.WARNING
                    ),
                    duration_ms=duration_ms,
                    document_media_type=value.media_type,
                    document_error=(
                        result.extraction_error if result is not None else None
                    ),
                    document_bytes=len(raw) if raw is not None else None,
                    document_text_chars=(
                        len(result.text)
                        if result is not None and result.text is not None
                        else None
                    ),
                    scenario_key=context.scenario_key,
                    profile_fingerprint=context.profile_fingerprint,
                    document_kind=document_kind,
                    normalizer_key=normalizer_key,
                    normalizer_version=normalizer_version,
                    intake_type=target_intake_type,
                    intake_type_known=True,
                ),
            )

    def _normalize(
        self,
        value: DocumentInput,
        *,
        document_kind: SafeIdentifier,
        target_intake_type: SafeIdentifier,
        normalizer_key: SafeIdentifier,
        normalizer_version: int,
    ) -> NormalizedDocument:
        raw = (
            value.text.encode("utf-8")
            if value.media_type is DocumentMediaType.TEXT
            else value.content
        )
        if raw is None:
            raise ValueError("document payload is unavailable")
        digest = sha256(raw).hexdigest()

        binding = {
            "document_kind": document_kind,
            "target_intake_type": target_intake_type,
            "normalizer_key": normalizer_key,
            "normalizer_version": normalizer_version,
        }

        if len(raw) > MAX_DOCUMENT_BYTES:
            return self._error(
                value.media_type,
                digest,
                DocumentExtractionError.DOCUMENT_TOO_LARGE,
                **binding,
            )

        if value.media_type is DocumentMediaType.TEXT:
            normalized = self._normalize_text(value.text or "")
            if not normalized.strip():
                return self._error(
                    value.media_type,
                    digest,
                    DocumentExtractionError.DOCUMENT_TEXT_EMPTY,
                    **binding,
                )
            if len(normalized) > MAX_DOCUMENT_TEXT_CHARS:
                return self._error(
                    value.media_type,
                    digest,
                    DocumentExtractionError.DOCUMENT_TEXT_TOO_LARGE,
                    **binding,
                )
            return self._success(
                value.media_type,
                digest,
                normalized,
                **binding,
            )

        try:
            reader = PdfReader(BytesIO(raw), strict=True)
            if reader.is_encrypted:
                return self._error(
                    value.media_type,
                    digest,
                    DocumentExtractionError.PDF_ENCRYPTED,
                    **binding,
                )
            if len(reader.pages) > MAX_DOCUMENT_PAGES:
                return self._error(
                    value.media_type,
                    digest,
                    DocumentExtractionError.DOCUMENT_TOO_LARGE,
                    **binding,
                )
            extracted = "\n".join(page.extract_text() or "" for page in reader.pages)
        except (PdfReadError, OSError, ValueError, KeyError, IndexError):
            return self._error(
                value.media_type,
                digest,
                DocumentExtractionError.PDF_MALFORMED,
                **binding,
            )
        except Exception:
            # Provider-visible and persisted data must never contain parser
            # exception details. Treat unknown parser failures as malformed.
            return self._error(
                value.media_type,
                digest,
                DocumentExtractionError.PDF_MALFORMED,
                **binding,
            )

        normalized = self._normalize_text(extracted)
        if not normalized.strip():
            return self._error(
                value.media_type,
                digest,
                DocumentExtractionError.PDF_EMPTY,
                **binding,
            )
        if len(normalized) > MAX_DOCUMENT_TEXT_CHARS:
            return self._error(
                value.media_type,
                digest,
                DocumentExtractionError.DOCUMENT_TEXT_TOO_LARGE,
                **binding,
            )
        return self._success(
            value.media_type,
            digest,
            normalized,
            **binding,
        )

    @staticmethod
    def _normalize_text(value: str) -> str:
        return value.replace("\r\n", "\n").replace("\r", "\n")

    @staticmethod
    def _success(
        media_type: DocumentMediaType,
        digest: str,
        text: str,
        *,
        document_kind: SafeIdentifier,
        target_intake_type: SafeIdentifier,
        normalizer_key: SafeIdentifier,
        normalizer_version: int,
    ) -> NormalizedDocument:
        return NormalizedDocument(
            document_kind=document_kind,
            target_intake_type=target_intake_type,
            media_type=media_type,
            sha256=digest,
            normalizer_key=normalizer_key,
            normalizer_version=normalizer_version,
            text=text,
        )

    @staticmethod
    def _error(
        media_type: DocumentMediaType,
        digest: str,
        error: DocumentExtractionError,
        *,
        document_kind: SafeIdentifier,
        target_intake_type: SafeIdentifier,
        normalizer_key: SafeIdentifier,
        normalizer_version: int,
    ) -> NormalizedDocument:
        return NormalizedDocument(
            document_kind=document_kind,
            target_intake_type=target_intake_type,
            media_type=media_type,
            sha256=digest,
            normalizer_key=normalizer_key,
            normalizer_version=normalizer_version,
            extraction_error=error,
        )


__all__ = ["DocumentNormalizer"]
