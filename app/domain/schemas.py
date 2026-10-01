from __future__ import annotations

import json
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


MAX_CHANNEL_CHARS = 100
MAX_SUBJECT_CHARS = 500
MAX_BODY_CHARS = 100_000
MAX_EXTRACTED_FIELDS_BYTES = 100_000


class CreateCaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: str = Field(min_length=1, max_length=MAX_CHANNEL_CHARS)
    subject: str = Field(min_length=1, max_length=MAX_SUBJECT_CHARS)
    body: str = Field(min_length=1, max_length=MAX_BODY_CHARS)
    customer_id: UUID | None = None
    extracted_fields: dict[str, object] = Field(default_factory=dict)

    @field_validator("extracted_fields")
    @classmethod
    def validate_extracted_fields_size(
        cls,
        value: dict[str, object],
    ) -> dict[str, object]:
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, OverflowError) as error:
            raise ValueError("extracted_fields must be finite JSON") from error
        if len(encoded) > MAX_EXTRACTED_FIELDS_BYTES:
            raise ValueError("extracted_fields exceeds the allowed size")
        return value
