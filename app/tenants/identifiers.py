"""Bounded identifiers used by trusted tenant configuration."""

from typing import Annotated

from pydantic import StringConstraints, StrictStr


SAFE_IDENTIFIER_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
SafeIdentifier = Annotated[
    StrictStr,
    StringConstraints(pattern=SAFE_IDENTIFIER_PATTERN),
]
