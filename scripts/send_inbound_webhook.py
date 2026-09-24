"""Send one bounded local signed email-like webhook message."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from app.auth import sign_inbound_webhook
from app.documents import MAX_DOCUMENT_BYTES


def _attachment_payload(path: Path) -> dict[str, str]:
    suffix = path.suffix.lower()
    media_type = {
        ".txt": "text/plain",
        ".pdf": "application/pdf",
    }.get(suffix)
    if media_type is None:
        raise ValueError("attachment must have a .txt or .pdf extension")
    if path.stat().st_size > MAX_DOCUMENT_BYTES:
        raise ValueError("attachment exceeds the document size limit")
    return {
        "media_type": media_type,
        "content_base64": base64.b64encode(path.read_bytes()).decode("ascii"),
    }


def build_request(
    url: str,
    *,
    api_key: str,
    provider_id: str,
    from_addr: str,
    subject: str,
    body: str,
    attachment: Path | None = None,
    timestamp: int | None = None,
) -> urllib.request.Request:
    payload: dict[str, object] = {
        "provider_id": provider_id,
        "from_addr": from_addr,
        "subject": subject,
        "body": body,
        "attachments": [],
    }
    if attachment is not None:
        payload["attachments"] = [_attachment_payload(attachment)]
    raw_body = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    signed_at = int(time.time()) if timestamp is None else timestamp
    return urllib.request.Request(
        url,
        data=raw_body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-API-Key": api_key,
            "X-Inbound-Timestamp": str(signed_at),
            "X-Inbound-Signature": sign_inbound_webhook(api_key, signed_at, raw_body),
        },
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--provider-id", required=True)
    parser.add_argument("--from-addr", required=True)
    parser.add_argument("--subject", default="")
    parser.add_argument("--body", default="")
    parser.add_argument("--attachment", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    api_key = os.environ.get("INBOUND_WEBHOOK_API_KEY")
    if not api_key:
        print("INBOUND_WEBHOOK_API_KEY is required", file=sys.stderr)
        return 2
    try:
        request = build_request(
            arguments.url,
            api_key=api_key,
            provider_id=arguments.provider_id,
            from_addr=arguments.from_addr,
            subject=arguments.subject,
            body=arguments.body,
            attachment=arguments.attachment,
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            print(response.status)
            print(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        print(error.code)
        print(error.read().decode("utf-8"))
        return 1
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
