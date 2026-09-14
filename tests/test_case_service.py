from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.db.models import CaseEvent, IntakeCase
from app.domain.repositories import CaseRepository
from app.domain.schemas import CreateCaseRequest
from app.domain.service import CaseService


class Transaction(AbstractAsyncContextManager["Transaction"]):
    def __init__(self, session: "InMemorySession") -> None:
        self.session = session

    async def __aenter__(self) -> "Transaction":
        self.session.transaction_entries += 1
        self.session.in_transaction = True
        return self

    async def __aexit__(self, exc_type, exc_value, traceback) -> None:
        self.session.transaction_exits += 1
        self.session.in_transaction = False
        if exc_type is None:
            self.session.commit()
        else:
            self.session.rollback()


@dataclass
class ScalarResult:
    value: IntakeCase | None

    def scalar_one_or_none(self) -> IntakeCase | None:
        return self.value


class InMemorySession:
    """Unit-level async session double that enforces the scoped lookup query."""

    def __init__(self, *, fail_on_event_add: bool = False) -> None:
        self.cases: list[IntakeCase] = []
        self.events: list[CaseEvent] = []
        self.pending_cases: list[IntakeCase] = []
        self.pending_events: list[CaseEvent] = []
        self.transaction_entries = 0
        self.transaction_exits = 0
        self.commits = 0
        self.rollbacks = 0
        self.in_transaction = False
        self.fail_on_event_add = fail_on_event_add

    def begin(self) -> Transaction:
        return Transaction(self)

    def add(self, instance: IntakeCase | CaseEvent) -> None:
        if isinstance(instance, IntakeCase):
            target = self.pending_cases if self.in_transaction else self.cases
            target.append(instance)
        else:
            if self.fail_on_event_add:
                raise RuntimeError("event insert failed")
            target = self.pending_events if self.in_transaction else self.events
            target.append(instance)

    async def flush(self) -> None:
        for case in self.pending_cases:
            if case.id is None:
                case.id = uuid4()

    def commit(self) -> None:
        self.cases.extend(self.pending_cases)
        self.events.extend(self.pending_events)
        self.pending_cases.clear()
        self.pending_events.clear()
        self.commits += 1

    def rollback(self) -> None:
        self.pending_cases.clear()
        self.pending_events.clear()
        self.rollbacks += 1

    async def execute(self, statement: object) -> ScalarResult:
        compiled = statement.compile(dialect=postgresql.dialect())
        query = str(compiled)
        assert "FROM intake_cases" in query
        assert "intake_cases.id = %(id_1)s" in query
        assert "intake_cases.tenant_id = %(tenant_id_1)s" in query

        case_id = compiled.params["id_1"]
        tenant_id = compiled.params["tenant_id_1"]
        return ScalarResult(
            next(
                (
                    case
                    for case in self.cases
                    if case.id == case_id and case.tenant_id == tenant_id
                ),
                None,
            )
        )


def make_request() -> CreateCaseRequest:
    return CreateCaseRequest(
        channel="email",
        subject="Password reset",
        body="Please reset my account password.",
        customer_id=None,
        extracted_fields={"priority": "high"},
    )


def test_create_case_request_defaults_optional_customer_and_extracted_fields() -> None:
    """Omitting unrecognized-customer data must create an empty normalized field map."""
    request = CreateCaseRequest(
        channel="email",
        subject="Password reset",
        body="Please reset my account password.",
    )

    assert request.customer_id is None
    assert request.extracted_fields == {}


def test_create_case_request_rejects_unrecognized_fields() -> None:
    """An unexpected field, including tenant ownership, must not enter the domain request."""
    with pytest.raises(ValidationError):
        CreateCaseRequest(
            channel="email",
            subject="Password reset",
            body="Please reset my account password.",
            customer_id=None,
            extracted_fields={},
            tenant_id=str(uuid4()),
        )


async def test_create_case_persists_received_case_and_one_created_event_in_one_transaction() -> None:
    """Removing the audit write or transaction would leave case creation without its required trail."""
    session = InMemorySession()
    tenant_id = uuid4()

    case = await CaseService(session).create_case(tenant_id, make_request())

    assert case.tenant_id == tenant_id
    assert case.status == "received"
    assert case.source == "email"
    assert case.channel == "email"
    assert case.subject == "Password reset"
    assert case.body == "Please reset my account password."
    assert case.customer_id is None
    assert case.raw_payload == {
        "channel": "email",
        "subject": "Password reset",
        "body": "Please reset my account password.",
    }
    assert case.extracted_fields == {"priority": "high"}
    assert case.id is not None
    assert len(session.events) == 1
    assert session.events[0].tenant_id == tenant_id
    assert session.events[0].case_id == case.id
    assert session.events[0].event_type == "created"
    assert session.events[0].actor is None
    assert session.transaction_entries == 1
    assert session.transaction_exits == 1
    assert session.commits == 1
    assert session.rollbacks == 0


async def test_create_case_rolls_back_case_and_event_when_event_add_fails() -> None:
    """An audit-write failure must not leave a received case committed by itself."""
    session = InMemorySession(fail_on_event_add=True)

    with pytest.raises(RuntimeError, match="event insert failed"):
        await CaseService(session).create_case(uuid4(), make_request())

    assert session.cases == []
    assert session.events == []
    assert session.pending_cases == []
    assert session.pending_events == []
    assert session.commits == 0
    assert session.rollbacks == 1


async def test_get_for_tenant_hides_a_case_owned_by_another_tenant() -> None:
    """Dropping the tenant predicate would disclose another tenant's case by UUID."""
    session = InMemorySession()
    owner_id, other_tenant_id = uuid4(), uuid4()
    case = IntakeCase(id=uuid4(), tenant_id=owner_id, status="received")
    session.add(case)

    result = await CaseRepository(session).get_for_tenant(case.id, other_tenant_id)

    assert result is None
