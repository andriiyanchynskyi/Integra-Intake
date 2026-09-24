"""Map authenticated inbound messages into the existing enqueue flow."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from app.documents import DocumentNormalizer, NormalizedRateConfirmationDocument
from app.domain.intake import EnqueueIntakeCommand, EnqueueResult, IntakeEnqueueService
from app.policy.models import TrustedSource

from .models import InboundMessage, InvalidInboundPayload


class InboundIntakeService:
    """Normalize one bounded inbound message and enqueue one tenant job."""

    def __init__(
        self,
        enqueue_service: IntakeEnqueueService,
        *,
        normalizer: DocumentNormalizer | None = None,
        to_thread: Callable[..., Awaitable[NormalizedRateConfirmationDocument]] = asyncio.to_thread,
    ) -> None:
        self._enqueue_service = enqueue_service
        self._normalizer = normalizer or DocumentNormalizer()
        self._to_thread = to_thread

    async def enqueue(self, message: InboundMessage) -> EnqueueResult:
        if len(message.attachments) > 1:
            raise InvalidInboundPayload("invalid inbound payload")

        document = None
        if message.attachments:
            document_input = message.attachments[0].as_document_input(
                channel=message.channel,
                subject=message.subject,
                body=message.body,
            )
            document = await self._to_thread(self._normalizer.normalize, document_input)

        source = TrustedSource(
            channel=message.channel,
            sender=message.from_addr,
            subject=message.subject,
            body=message.body,
            document=document,
        )
        return await self._enqueue_service.enqueue(
            EnqueueIntakeCommand(
                tenant_id=message.tenant_id,
                tenant_slug=message.tenant_slug,
                source=source,
                idempotency_key=f"email_webhook:{message.provider_id}",
            )
        )


__all__ = ["InboundIntakeService"]
