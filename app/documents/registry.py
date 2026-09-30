"""Immutable, server-owned metadata for document normalizers."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import re
from types import MappingProxyType

from app.documents.models import DocumentMediaType
from app.tenants.identifiers import SAFE_IDENTIFIER_PATTERN


class DocumentCapabilityUnavailable(ValueError):
    """The requested document normalizer is not available in this deployment."""


def _bounded_text_pdf_factory(**kwargs: object) -> object:
    """Load the built-in implementation only after the package is initialized."""

    from app.documents.normalizer import DocumentNormalizer

    return DocumentNormalizer(**kwargs)


@dataclass(frozen=True, slots=True)
class DocumentNormalizerCapability:
    key: str
    version: int
    supported_media_types: frozenset[DocumentMediaType]
    normalizer_factory: Callable[..., object] | None = None

    def __post_init__(self) -> None:
        if re.fullmatch(SAFE_IDENTIFIER_PATTERN, self.key) is None:
            raise ValueError("document normalizer key must be a safe identifier")
        if type(self.version) is not int or self.version < 1:
            raise ValueError("document normalizer version must be a positive integer")
        if not isinstance(self.supported_media_types, frozenset) or not all(
            isinstance(value, DocumentMediaType)
            for value in self.supported_media_types
        ):
            raise TypeError("supported_media_types must contain document media types")
        if self.normalizer_factory is not None and not callable(
            self.normalizer_factory
        ):
            raise TypeError("normalizer_factory must be callable or None")

    def create(
        self,
        *,
        observer: object | None = None,
        context: object | None = None,
    ) -> object:
        """Construct the server-owned normalizer bound to this capability."""

        if self.normalizer_factory is None:
            raise DocumentCapabilityUnavailable("document capability unavailable")
        from app.observability import NULL_OBSERVER

        normalizer = self.normalizer_factory(
            observer=observer if observer is not None else NULL_OBSERVER,
            context=context,
        )
        if not callable(getattr(normalizer, "normalize", None)):
            raise DocumentCapabilityUnavailable("document capability unavailable")
        return normalizer


class DocumentNormalizerRegistry:
    """Immutable lookup table for exact normalizer capabilities."""

    def __init__(self, capabilities: Iterable[DocumentNormalizerCapability]) -> None:
        values: dict[tuple[str, int], DocumentNormalizerCapability] = {}
        keys: set[str] = set()
        for capability in capabilities:
            if capability.key in keys:
                raise ValueError("document normalizer keys must be unique")
            keys.add(capability.key)
            identity = (capability.key, capability.version)
            if identity in values:
                raise ValueError("document normalizer capabilities must be unique")
            values[identity] = capability
        self._capabilities = MappingProxyType(values)

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(key for key, _ in self._capabilities)

    def require(self, key: str, version: int) -> DocumentNormalizerCapability:
        capability = self._capabilities.get((key, version))
        if capability is None:
            raise DocumentCapabilityUnavailable("document capability unavailable")
        return capability

    def create(
        self,
        key: str,
        version: int,
        *,
        observer: object | None = None,
        context: object | None = None,
    ) -> object:
        """Create the exact registered normalizer implementation."""

        return self.require(key, version).create(observer=observer, context=context)


BUILTIN_DOCUMENT_REGISTRY = DocumentNormalizerRegistry(
    (
        DocumentNormalizerCapability(
            key="bounded_text_pdf",
            version=1,
            supported_media_types=frozenset(
                {DocumentMediaType.TEXT, DocumentMediaType.PDF}
            ),
            normalizer_factory=_bounded_text_pdf_factory,
        ),
    )
)


__all__ = [
    "BUILTIN_DOCUMENT_REGISTRY",
    "DocumentCapabilityUnavailable",
    "DocumentNormalizerCapability",
    "DocumentNormalizerRegistry",
]
