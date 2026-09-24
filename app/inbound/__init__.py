"""Provider-neutral inbound message contracts and enqueue service."""

from app.inbound.models import (
    InboundAttachment,
    InboundMessage,
    InvalidInboundPayload,
    MAX_BASE64_ATTACHMENT_CHARS,
    MAX_INBOUND_BODY_CHARS,
    MAX_INBOUND_WEBHOOK_BODY_BYTES,
    MAX_PROVIDER_ID_CHARS,
    WebhookAttachmentPayload,
    WebhookEmailPayload,
    parse_webhook_email_payload,
)
from app.inbound.service import InboundIntakeService

__all__ = [
    "InboundAttachment",
    "InboundIntakeService",
    "InboundMessage",
    "InvalidInboundPayload",
    "MAX_BASE64_ATTACHMENT_CHARS",
    "MAX_INBOUND_BODY_CHARS",
    "MAX_INBOUND_WEBHOOK_BODY_BYTES",
    "MAX_PROVIDER_ID_CHARS",
    "WebhookAttachmentPayload",
    "WebhookEmailPayload",
    "parse_webhook_email_payload",
]
