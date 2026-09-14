from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class CreateCaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel: str
    subject: str
    body: str
    customer_id: UUID | None = None
    extracted_fields: dict[str, object] = Field(default_factory=dict)
