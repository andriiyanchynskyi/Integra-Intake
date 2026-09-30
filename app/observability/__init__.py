"""Safe local observability contracts."""

from app.observability.context import ObservationContext
from app.observability.events import (
    ClientCorrelationId,
    CapabilityErrorCode,
    Component,
    CredentialKind,
    EventLevel,
    EventName,
    ObservationEvent,
    OutcomeCode,
    PersistenceOperation,
    PolicyReason,
    PreflightOutcome,
    ProfileFingerprint,
    RouteName,
    SafeFieldName,
    WorkerErrorCode,
)
from app.observability.observer import (
    NULL_OBSERVER,
    NullObserver,
    Observer,
    RecordingObserver,
    StructlogObserver,
    safe_emit,
)
from app.observability.middleware import (
    bind_request_context,
    request_context,
    request_observer,
)

__all__ = [
    "ClientCorrelationId",
    "CapabilityErrorCode",
    "Component",
    "CredentialKind",
    "EventLevel",
    "EventName",
    "NULL_OBSERVER",
    "NullObserver",
    "ObservationContext",
    "ObservationEvent",
    "Observer",
    "OutcomeCode",
    "PersistenceOperation",
    "PolicyReason",
    "PreflightOutcome",
    "ProfileFingerprint",
    "RecordingObserver",
    "RouteName",
    "SafeFieldName",
    "StructlogObserver",
    "WorkerErrorCode",
    "safe_emit",
    "bind_request_context",
    "request_context",
    "request_observer",
]
