"""Public API-key authentication and provisioning interfaces."""

from app.auth.api_keys import (
    API_KEY_FORMAT,
    API_KEY_PREFIX,
    API_KEY_TOKEN_PATTERN,
    AuthenticatedOperator,
    SAFE_PREFIX_LENGTH,
    generate_api_key,
    get_current_operator,
    get_current_tenant,
    hash_api_key,
)
from app.auth.inbound_webhook import (
    MAX_SIGNATURE_AGE_SECONDS,
    SIGNATURE_VERSION,
    get_current_inbound_tenant,
    read_bounded_inbound_body,
    sign_inbound_webhook,
    signature_payload,
    verify_inbound_webhook_signature,
)

__all__ = [
    "API_KEY_FORMAT",
    "API_KEY_PREFIX",
    "API_KEY_TOKEN_PATTERN",
    "AuthenticatedOperator",
    "SAFE_PREFIX_LENGTH",
    "generate_api_key",
    "get_current_operator",
    "get_current_tenant",
    "hash_api_key",
    "MAX_SIGNATURE_AGE_SECONDS",
    "SIGNATURE_VERSION",
    "get_current_inbound_tenant",
    "read_bounded_inbound_body",
    "sign_inbound_webhook",
    "signature_payload",
    "verify_inbound_webhook_signature",
]
