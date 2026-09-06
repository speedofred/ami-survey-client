"""The envelope that crosses the public tier.

Everything here is metadata the landing server is allowed to see: an id, a size,
which key the payload was sealed to, and how it arrived. The submission itself
is one opaque base64 field that landing has no key for.

The id is supplied by the sender rather than derived from the ciphertext. A
sealed box is randomised - the same submission sealed twice produces different
bytes - so a content hash would give every retry a new identity and turn a
network timeout into a duplicate run.
"""

from __future__ import annotations

import base64
import json
import re
import uuid
from datetime import datetime, timezone

ENVELOPE_VERSION = 1
MAX_BODY_BYTES = 5 * 1024 * 1024

TRANSPORT_SEALED = "sealed"
TRANSPORT_TLS_ONLY = "tls_only"
TRANSPORTS = (TRANSPORT_SEALED, TRANSPORT_TLS_ONLY)

SUBMISSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{7,63}")


class WireError(ValueError):
    """A message did not conform to the envelope contract."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def new_submission_id() -> str:
    return uuid.uuid4().hex


def validate_submission_id(value: str) -> str:
    """Check an id before it is ever used as a filename.

    The spool is a directory keyed by this value, so an id containing a path
    separator or a parent reference would write outside the spool.
    """
    text = str(value or "").strip()
    if not SUBMISSION_ID_PATTERN.fullmatch(text):
        raise WireError(
            "submission_id must be 8-64 characters of letters, digits, hyphen or "
            f"underscore, starting alphanumeric; got {value!r}"
        )
    return text


def build_envelope(*, submission_id: str, sealed: bytes, key_id: str, transport: str) -> dict:
    if transport not in TRANSPORTS:
        raise WireError(f"transport must be one of {TRANSPORTS}, got {transport!r}")
    return {
        "envelope": ENVELOPE_VERSION,
        "submission_id": validate_submission_id(submission_id),
        "key_id": key_id,
        "transport": transport,
        "bytes": len(sealed),
        "sealed": base64.b64encode(sealed).decode("ascii"),
    }


def sealed_bytes(envelope: dict) -> bytes:
    try:
        return base64.b64decode(envelope["sealed"], validate=True)
    except (KeyError, ValueError, TypeError) as exc:
        raise WireError(f"Envelope has no readable sealed payload: {exc}") from exc


def parse_body(raw: bytes) -> dict:
    if len(raw) > MAX_BODY_BYTES:
        raise WireError(f"Body is {len(raw)} bytes; the limit is {MAX_BODY_BYTES}.")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WireError(f"Body is not JSON: {exc}") from exc
    if not isinstance(body, dict):
        raise WireError("Body must be a JSON object.")
    return body
