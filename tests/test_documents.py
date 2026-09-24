from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
from uuid import UUID

import pytest
from pydantic import ValidationError
from pypdf import PdfWriter
import yaml

from app.agent import AgentProposal, ProposalPriority, ToolCall
from app.documents import (
    MAX_DOCUMENT_BYTES,
    MAX_DOCUMENT_PAGES,
    MAX_DOCUMENT_TEXT_CHARS,
    DocumentExtractionError,
    DocumentMediaType,
    DocumentNormalizer,
    NormalizedRateConfirmationDocument,
    RateConfirmationDocumentInput,
)
from app.policy import RiskSignals, TrustedSource, TrustedToolRuntimeContext
from app.tenants.loader import load_tenant_config
from app.tools import InMemoryTenantToolPort, PolicyGatedToolExecutor


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCUMENT_FIXTURES = PROJECT_ROOT / "evals" / "fixtures" / "docs"
MANIFEST_PATH = DOCUMENT_FIXTURES / "manifest.yaml"
_EXPECTED_FIXTURE_IDS = (
    "complete-en",
    "complete-uk",
    "complete-pdf",
    "complete-two-page-pdf",
    "complete-no-rate-hint",
    "complete-injection-pdf",
    "incomplete",
    "malformed-pdf",
)
_FIELD_LABELS = {
    "origin": "Origin",
    "destination": "Destination",
    "equipment": "Equipment",
    "pickup_window": "Pickup window",
    "commodity": "Commodity",
    "contact": "Contact",
    "quoted_rate": "Quoted rate",
    "valid_until": "Valid until",
}


def make_document_input(
    *,
    media_type: DocumentMediaType = DocumentMediaType.TEXT,
    text: str | None = "Origin: Chicago\r\nDestination: Detroit\rRate: 2500 USD",
    content: bytes | None = None,
) -> RateConfirmationDocumentInput:
    payload: dict[str, object] = {
        "channel": "email",
        "subject": "Rate confirmation",
        "body": "Please process this document.",
        "media_type": media_type,
    }
    if text is not None:
        payload["text"] = text
    if content is not None:
        payload["content"] = content
    return RateConfirmationDocumentInput.model_validate(payload)


def pdf_bytes(*, pages: int = 1, encrypted: bool = False) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    if encrypted:
        writer.encrypt("fixture-password")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def normalize(
    value: RateConfirmationDocumentInput,
) -> NormalizedRateConfirmationDocument:
    return DocumentNormalizer().normalize(value)


def test_document_input_rejects_unknown_keys() -> None:
    payload = {
        "channel": "email",
        "subject": "Rate confirmation",
        "media_type": DocumentMediaType.TEXT,
        "text": "Origin: Chicago",
        "unexpected": "must not cross the boundary",
    }

    with pytest.raises(ValidationError):
        RateConfirmationDocumentInput.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {
            "channel": "email",
            "subject": "Rate confirmation",
            "media_type": DocumentMediaType.TEXT,
        },
        {
            "channel": "email",
            "subject": "Rate confirmation",
            "media_type": DocumentMediaType.PDF,
        },
        {
            "channel": "email",
            "subject": "Rate confirmation",
            "media_type": DocumentMediaType.TEXT,
            "text": "Origin: Chicago",
            "content": b"%PDF-1.7",
        },
        {
            "channel": "email",
            "subject": "Rate confirmation",
            "media_type": DocumentMediaType.PDF,
            "text": "Origin: Chicago",
            "content": b"%PDF-1.7",
        },
    ],
    ids=[
        "neither-payload",
        "pdf-neither-payload",
        "text-both-payloads",
        "pdf-both-payloads",
    ],
)
def test_document_input_requires_exactly_one_media_payload(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        RateConfirmationDocumentInput.model_validate(payload)


@pytest.mark.parametrize(
    ("media_type", "payload_key", "payload"),
    [
        (DocumentMediaType.TEXT, "content", b"%PDF-1.7"),
        (DocumentMediaType.PDF, "text", "Origin: Chicago"),
    ],
    ids=["text-media-with-pdf-bytes", "pdf-media-with-text"],
)
def test_document_input_rejects_mismatched_media_payload(
    media_type: DocumentMediaType,
    payload_key: str,
    payload: object,
) -> None:
    values: dict[str, object] = {
        "channel": "email",
        "subject": "Rate confirmation",
        "media_type": media_type,
        payload_key: payload,
    }

    with pytest.raises(ValidationError):
        RateConfirmationDocumentInput.model_validate(values)


@pytest.mark.parametrize(
    ("field", "value"),
    [("channel", ""), ("channel", "   "), ("subject", ""), ("subject", " \t")],
)
def test_document_input_rejects_blank_message_metadata(field: str, value: str) -> None:
    payload: dict[str, object] = {
        "channel": "email",
        "subject": "Rate confirmation",
        "media_type": DocumentMediaType.TEXT,
        "text": "Origin: Chicago",
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        RateConfirmationDocumentInput.model_validate(payload)


def test_document_input_rejects_blank_text() -> None:
    with pytest.raises(ValidationError):
        make_document_input(text=" \r\n\t ")


def test_document_input_rejects_empty_pdf_bytes() -> None:
    with pytest.raises(ValidationError):
        make_document_input(media_type=DocumentMediaType.PDF, text=None, content=b"")


def test_document_input_rejects_unsupported_media_type() -> None:
    with pytest.raises(ValidationError):
        RateConfirmationDocumentInput.model_validate(
            {
                "channel": "email",
                "subject": "Rate confirmation",
                "media_type": "application/octet-stream",
                "content": b"bytes",
            }
        )


def test_normalizer_normalizes_line_endings_and_hashes_original_plaintext() -> None:
    source = "Origin: Chicago\r\nDestination: Detroit\rRate: 2500 USD"
    result = normalize(make_document_input(text=source))

    assert result.kind == "rate_confirmation"
    assert result.media_type is DocumentMediaType.TEXT
    assert result.text == "Origin: Chicago\nDestination: Detroit\nRate: 2500 USD"
    assert result.extraction_error is None
    assert result.sha256 == sha256(source.encode("utf-8")).hexdigest()
    assert "content" not in result.model_dump(mode="json")


def test_plaintext_over_character_limit_returns_stable_error() -> None:
    source = "x" * (MAX_DOCUMENT_TEXT_CHARS + 1)

    result = normalize(make_document_input(text=source))

    assert result.text is None
    assert result.extraction_error is DocumentExtractionError.DOCUMENT_TEXT_TOO_LARGE
    assert result.sha256 == sha256(source.encode("utf-8")).hexdigest()


def test_pdf_over_byte_limit_is_rejected_before_parsing() -> None:
    content = b"%PDF-1.7\n" + b"x" * MAX_DOCUMENT_BYTES

    result = normalize(
        make_document_input(
            media_type=DocumentMediaType.PDF,
            text=None,
            content=content,
        )
    )

    assert result.text is None
    assert result.extraction_error is DocumentExtractionError.DOCUMENT_TOO_LARGE
    assert result.sha256 == sha256(content).hexdigest()


def test_pdf_over_page_limit_is_rejected() -> None:
    content = pdf_bytes(pages=MAX_DOCUMENT_PAGES + 1)

    result = normalize(
        make_document_input(
            media_type=DocumentMediaType.PDF,
            text=None,
            content=content,
        )
    )

    assert result.text is None
    assert result.extraction_error is DocumentExtractionError.DOCUMENT_TOO_LARGE
    assert result.sha256 == sha256(content).hexdigest()


def test_malformed_pdf_returns_stable_error_without_exception_details() -> None:
    content = b"not a PDF and not a parser traceback"

    result = normalize(
        make_document_input(
            media_type=DocumentMediaType.PDF,
            text=None,
            content=content,
        )
    )

    assert result.text is None
    assert result.extraction_error is DocumentExtractionError.PDF_MALFORMED
    assert "parser traceback" not in repr(result.model_dump(mode="json"))


def test_encrypted_pdf_returns_stable_error() -> None:
    content = pdf_bytes(encrypted=True)

    result = normalize(
        make_document_input(
            media_type=DocumentMediaType.PDF,
            text=None,
            content=content,
        )
    )

    assert result.text is None
    assert result.extraction_error is DocumentExtractionError.PDF_ENCRYPTED


@pytest.mark.parametrize(
    "pages",
    [0, 1],
    ids=["zero-page-pdf", "textless-pdf"],
)
def test_empty_or_textless_pdf_returns_stable_error(pages: int) -> None:
    content = pdf_bytes(pages=pages)
    result = normalize(
        make_document_input(
            media_type=DocumentMediaType.PDF,
            text=None,
            content=content,
        )
    )

    assert result.text is None
    assert result.extraction_error is DocumentExtractionError.PDF_EMPTY


def test_normalized_snapshot_rejects_unknown_keys() -> None:
    result = normalize(make_document_input())
    payload = result.model_dump(mode="json")
    payload["unexpected"] = "not part of the snapshot"

    with pytest.raises(ValidationError):
        NormalizedRateConfirmationDocument.model_validate(payload)


def test_normalized_snapshot_requires_exactly_one_text_or_error() -> None:
    result = normalize(make_document_input())
    payload = result.model_dump(mode="json")

    both = dict(payload)
    both["extraction_error"] = DocumentExtractionError.DOCUMENT_TEXT_EMPTY.value
    with pytest.raises(ValidationError):
        NormalizedRateConfirmationDocument.model_validate(both)

    neither = dict(payload)
    neither["text"] = None
    with pytest.raises(ValidationError):
        NormalizedRateConfirmationDocument.model_validate(neither)


def _fixture_manifest() -> dict[str, object]:
    return yaml.safe_load(MANIFEST_PATH.read_text(encoding="utf-8"))


def _normalize_fixture(
    entry: dict[str, object],
) -> tuple[bytes, NormalizedRateConfirmationDocument]:
    raw = (DOCUMENT_FIXTURES / str(entry["filename"])).read_bytes()
    media_type = DocumentMediaType(str(entry["media_type"]))
    payload: dict[str, object] = {
        "channel": "fixture",
        "subject": str(entry["id"]),
        "body": "Synthetic fixture",
        "media_type": media_type,
    }
    if media_type is DocumentMediaType.TEXT:
        payload["text"] = raw.decode("utf-8")
    else:
        payload["content"] = raw
    return raw, DocumentNormalizer().normalize(
        RateConfirmationDocumentInput.model_validate(payload)
    )


def _fixture_proposal(
    entry: dict[str, object],
    document: NormalizedRateConfirmationDocument,
    *,
    invent_missing: bool = False,
) -> AgentProposal:
    fields: list[dict[str, object]] = []
    text = document.text or ""
    expected_present = [str(name) for name in entry["expected_verified_fields"]]
    expected_missing = [str(name) for name in entry["expected_missing_fields"]]
    for name in (*expected_present, *(expected_missing if invent_missing else ())):
        label = _FIELD_LABELS[name]
        match = next(
            (
                line.strip()
                for line in text.splitlines()
                if line.strip().lower().startswith(label.lower() + ":")
            ),
            None,
        )
        if match is None:
            fields.append({"name": name, "value": "invented"})
            continue
        fields.append(
            {
                "name": name,
                "value": match.split(":", 1)[1].strip(),
                "source_excerpt": match,
            }
        )
    if invent_missing:
        fields.append(
            {
                "name": "not_a_profile_field",
                "value": "invented",
                "source_excerpt": "not present in the source",
            }
        )
    return AgentProposal.model_validate(
        {
            "intake_type": entry["intake_type"],
            "fields": fields,
            "missing_required_fields": [],
            "priority": ProposalPriority.NORMAL,
            "contains_injection_or_override_attempt": (
                entry["id"] == "complete-injection-pdf"
            ),
            "rationale_short": "Synthetic fixture proposal",
            "tool_calls": [],
            "confidence": 0.8,
        }
    )


def test_document_fixture_manifest_is_closed_and_all_files_are_declared() -> None:
    manifest = _fixture_manifest()
    documents = manifest["documents"]

    assert manifest["version"] == 1
    assert isinstance(documents, list)
    assert len(documents) == 8
    assert tuple(item["id"] for item in documents) == _EXPECTED_FIXTURE_IDS
    assert len({item["filename"] for item in documents}) == 8
    for item in documents:
        assert {
            "id",
            "filename",
            "media_type",
            "intake_type",
            "expected_extraction_error",
            "expected_verified_fields",
            "expected_missing_fields",
        } <= set(item)
        assert (DOCUMENT_FIXTURES / item["filename"]).is_file()


@pytest.mark.parametrize("entry_index", range(8), ids=_EXPECTED_FIXTURE_IDS)
def test_document_fixtures_normalize_to_manifest_outcomes(entry_index: int) -> None:
    entry = _fixture_manifest()["documents"][entry_index]
    raw, normalized = _normalize_fixture(entry)
    snapshot = normalized.model_dump(mode="json")

    assert normalized.kind == "rate_confirmation"
    assert normalized.media_type.value == entry["media_type"]
    actual_error = (
        normalized.extraction_error.value
        if normalized.extraction_error is not None
        else None
    )
    assert actual_error == entry["expected_extraction_error"]
    if entry["expected_extraction_error"] is None:
        assert normalized.text is not None and normalized.text.strip()
    else:
        assert normalized.text is None
    assert not any(isinstance(value, bytes) for value in snapshot.values())
    if normalized.media_type is DocumentMediaType.PDF:
        assert raw not in json.dumps(snapshot, ensure_ascii=False).encode("utf-8")


@pytest.mark.parametrize(
    "entry_index",
    [0, 1, 2, 3, 4],
    ids=_EXPECTED_FIXTURE_IDS[:5],
)
def test_complete_document_fixtures_allow_create_case_with_exact_excerpts(
    entry_index: int,
) -> None:
    entry = _fixture_manifest()["documents"][entry_index]
    _, document = _normalize_fixture(entry)
    config = load_tenant_config(PROJECT_ROOT / "examples" / "freight-broker.yaml")
    port = InMemoryTenantToolPort()
    runtime = TrustedToolRuntimeContext(
        tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
        tenant_config=config,
        source=TrustedSource(
            channel="fixture",
            subject=str(entry["id"]),
            body="Rate confirmation document received.",
            document=document,
        ),
        risk_signals=RiskSignals(),
    )
    result = PolicyGatedToolExecutor(runtime=runtime, port=port).execute(
        _fixture_proposal(entry, document),
        ToolCall(name="create_case", arguments={}),
    )

    assert result.data is not None
    assert result.data["status"] == "received"
    assert len(port.cases) == 1
    assert port.approval_requests == []
    assert set(next(iter(port.cases.values())).extracted_fields) == set(
        entry["expected_verified_fields"]
    )


def test_injection_fixture_preserves_urgent_approval_precedence() -> None:
    entry = _fixture_manifest()["documents"][5]
    _, document = _normalize_fixture(entry)
    config = load_tenant_config(PROJECT_ROOT / "examples" / "freight-broker.yaml")
    port = InMemoryTenantToolPort()
    runtime = TrustedToolRuntimeContext(
        tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
        tenant_config=config,
        source=TrustedSource(
            channel="fixture",
            subject=str(entry["id"]),
            body="Rate confirmation document received.",
            document=document,
        ),
        risk_signals=RiskSignals(),
    )
    result = PolicyGatedToolExecutor(runtime=runtime, port=port).execute(
        _fixture_proposal(entry, document),
        ToolCall(name="create_case", arguments={}),
    )

    assert result.data["status"] == "urgent"
    assert result.data["decision"] == "needs_approval"
    assert result.data["reason"] == "safety_or_legal_risk"
    assert port.cases == {}
    assert len(port.approval_requests) == 1


@pytest.mark.parametrize("entry_index", [6, 7], ids=_EXPECTED_FIXTURE_IDS[6:])
def test_incomplete_and_malformed_fixtures_await_input_without_side_effects(
    entry_index: int,
) -> None:
    entry = _fixture_manifest()["documents"][entry_index]
    _, document = _normalize_fixture(entry)
    config = load_tenant_config(PROJECT_ROOT / "examples" / "freight-broker.yaml")
    port = InMemoryTenantToolPort()
    runtime = TrustedToolRuntimeContext(
        tenant_id=UUID("00000000-0000-0000-0000-000000000001"),
        tenant_config=config,
        source=TrustedSource(
            channel="fixture",
            subject=str(entry["id"]),
            body="Rate confirmation document received.",
            document=document,
        ),
        risk_signals=RiskSignals(),
    )
    result = PolicyGatedToolExecutor(runtime=runtime, port=port).execute(
        _fixture_proposal(entry, document, invent_missing=True),
        ToolCall(name="create_case", arguments={}),
    )

    assert result.data["status"] == "awaiting_input"
    assert result.data["decision"] == "deny"
    expected_reason = (
        "document_unreadable" if entry["id"] == "malformed-pdf" else "missing_required_fields"
    )
    assert result.data["reason"] == expected_reason
    assert result.data["missing_required_fields"] == sorted(entry["expected_missing_fields"])
    assert port.cases == {}
    assert port.approval_requests == []
