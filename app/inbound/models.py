"""Closed, provider-neutral contracts for the email-like inbound webhook."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError, field_validator, model_validator

from app.documents import (
    MAX_DOCUMENT_BYTES,
    DocumentMediaType,
    RateConfirmationDocumentInput,
)


MAX_INBOUND_WEBHOOK_BODY_BYTES = 8 * 1024 * 1024
MAX_INBOUND_BODY_CHARS = 100_000
MAX_PROVIDER_ID_CHARS = 200
MAX_BASE64_ATTACHMENT_CHARS = 4 * ((MAX_DOCUMENT_BYTES + 2) // 3)


class InvalidInboundPayload(ValueError):
    """A caller-visible invalid inbound envelope without source details."""


class _WebhookModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class WebhookAttachmentPayload(_WebhookModel):
    media_type: Literal["text/plain", "application/pdf"]
    content_base64: StrictStr = Field(
        min_length=1,
        max_length=MAX_BASE64_ATTACHMENT_CHARS,
    )

    @field_validator("content_base64")
    @classmethod
    def require_compact_base64(cls, value: str) -> str:
        if not value.strip() or any(character.isspace() for character in value):
            raise ValueError("attachment content is invalid")
        return value


class WebhookEmailPayload(_WebhookModel):
    provider_id: StrictStr = Field(max_length=MAX_PROVIDER_ID_CHARS)
    from_addr: StrictStr = Field(max_length=320)
    subject: StrictStr = Field(default="", max_length=500, validate_default=True)
    body: StrictStr = Field(default="", max_length=MAX_INBOUND_BODY_CHARS, validate_default=True)
    attachments: list[WebhookAttachmentPayload] = Field(default_factory=list, max_length=1)

    @field_validator("provider_id", "from_addr")
    @classmethod
    def require_non_blank_identity(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("identity value must not be blank")
        return value

    @model_validator(mode="after")
    def require_body_or_attachment(self) -> WebhookEmailPayload:
        if not self.attachments and not self.body.strip():
            raise ValueError("body is required when no attachment is present")
        return self

    def to_message(self, tenant_id: UUID, tenant_slug: str) -> InboundMessage:
        attachments: list[InboundAttachment] = []
        try:
            for item in self.attachments:
                decoded = base64.b64decode(item.content_base64, validate=True)
                if not decoded or len(decoded) > MAX_DOCUMENT_BYTES:
                    raise ValueError("attachment size is invalid")
                media_type = DocumentMediaType(item.media_type)
                if media_type is DocumentMediaType.TEXT:
                    attachments.append(
                        InboundAttachment(
                            media_type=media_type,
                            text=decoded.decode("utf-8"),
                        )
                    )
                else:
                    attachments.append(
                        InboundAttachment(media_type=media_type, content=decoded)
                    )
        except (binascii.Error, UnicodeDecodeError, ValueError):
            raise InvalidInboundPayload("invalid inbound payload") from None

        return InboundMessage(
            tenant_id=tenant_id,
            tenant_slug=tenant_slug,
            channel="email_webhook",
            from_addr=self.from_addr,
            subject=self.subject.strip() or "(no subject)",
            body=self.body,
            attachments=tuple(attachments),
            provider_id=self.provider_id,
        )


@dataclass(frozen=True, slots=True)
class InboundAttachment:
    media_type: DocumentMediaType
    text: str | None = None
    content: bytes | None = None

    def as_document_input(
        self,
        *,
        channel: str,
        subject: str,
        body: str,
    ) -> RateConfirmationDocumentInput:
        return RateConfirmationDocumentInput(
            channel=channel,
            subject=subject,
            body=body,
            media_type=self.media_type,
            text=self.text,
            content=self.content,
        )


@dataclass(frozen=True, slots=True)
class InboundMessage:
    tenant_id: UUID
    tenant_slug: str
    channel: str
    from_addr: str
    subject: str
    body: str
    attachments: tuple[InboundAttachment, ...]
    provider_id: str


def parse_webhook_email_payload(raw_body: bytes) -> WebhookEmailPayload:
    """Decode one strict JSON envelope without exposing validation details."""

    try:
        return WebhookEmailPayload.model_validate_json(raw_body)
    except (ValidationError, ValueError, UnicodeDecodeError):
        raise InvalidInboundPayload("invalid inbound payload") from None


__all__ = [
    "InboundAttachment",
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
