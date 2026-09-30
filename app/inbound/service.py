"""Map authenticated inbound messages into the existing enqueue flow."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from uuid import uuid4

from app.documents import DocumentNormalizer
from app.domain.intake import (
    EnqueueDocumentIntakeCommand,
    EnqueueIntakeCommand,
    EnqueueResult,
    IntakeEnqueueService,
)
from app.observability import NULL_OBSERVER, ObservationContext, Observer
from app.policy.models import TrustedSource

from .models import InboundMessage, InvalidInboundPayload


class InboundIntakeService:
    """Normalize one bounded inbound message and enqueue one tenant job."""

    def __init__(
        self,
        enqueue_service: IntakeEnqueueService,
        *,
        normalizer: DocumentNormalizer | None = None,
        to_thread: Callable[..., Awaitable[object]] = asyncio.to_thread,
        observer: Observer = NULL_OBSERVER,
        context: ObservationContext | None = None,
    ) -> None:
        self._enqueue_service = enqueue_service
        self._observer = observer
        self._context = context
        self._to_thread = to_thread
        if normalizer is not None:
            self._enqueue_service.document_normalizer = normalizer

    async def enqueue(self, message: InboundMessage) -> EnqueueResult:
        if len(message.attachments) > 1:
            raise InvalidInboundPayload("invalid inbound payload")

        if message.attachments:
            document_input = message.attachments[0].as_document_input(
                channel=message.channel,
                subject=message.subject,
                body=message.body,
            )
            return await self._enqueue_service.enqueue_document(
                EnqueueDocumentIntakeCommand(
                    tenant_id=message.tenant_id,
                    tenant_slug=message.tenant_slug,
                    document=document_input,
                    idempotency_key=f"email_webhook:{message.provider_id}",
                    trace_id=(
                        self._context.trace_id
                        if self._context is not None
                        else uuid4()
                    ),
                    sender=message.from_addr,
                )
            )

        source = TrustedSource(
            channel=message.channel,
            sender=message.from_addr,
            subject=message.subject,
            body=message.body,
        )
        return await self._enqueue_service.enqueue(
            EnqueueIntakeCommand(
                tenant_id=message.tenant_id,
                tenant_slug=message.tenant_slug,
                source=source,
                idempotency_key=f"email_webhook:{message.provider_id}",
                trace_id=(self._context.trace_id if self._context is not None else uuid4()),
            )
        )


__all__ = ["InboundIntakeService"]
