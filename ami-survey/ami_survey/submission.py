"""Seal a finished submission and post it to the landing server.

The document is complete before it leaves this machine. It is sealed to a public
key the landing server publishes, so the machine that receives it cannot read it
- the private half exists only on the AMI server, which is not reachable from
here or from anywhere else on the internet.

Sealing is not a claim of trustworthiness. The AMI server recomputes prices, the
evidence tier, plausibility and grade validity from the call records this
document carries, and its answer wins over anything asserted here. What sealing
buys is that nobody in between gets to read a client's workflow.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import config

# amitransport sits beside ami_survey rather than inside it: the landing server
# and the AMI server use the same two modules, and one copy cannot drift from
# another.
from amitransport import crypto, wire


class SubmissionError(RuntimeError):
    """A submission could not be sealed, or the landing server refused it."""


_KEY_CACHE: dict[str, tuple[str, bytes]] = {}


def _get(url: str, timeout: float) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SubmissionError(f"{url} -> HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise SubmissionError(f"Cannot reach the landing server at {url}: {exc}") from exc


def public_key(landing_url: str | None = None, *, timeout: float = 15.0) -> tuple[str, bytes]:
    """The key to seal to, and its id.

    Fetched rather than shipped, so rotating it does not mean reinstalling every
    collector. Cached for the life of the process: a collector submits once or
    twice, and re-fetching per submission would make the landing server a
    dependency of every step rather than of the last one.
    """
    base = (landing_url or config.LANDING_URL).rstrip("/")
    if base not in _KEY_CACHE:
        payload = _get(f"{base}/public-key", timeout)
        try:
            _KEY_CACHE[base] = (payload["key_id"], crypto.decode_key(payload["public_key"]))
        except (KeyError, crypto.SealError) as exc:
            raise SubmissionError(f"{base}/public-key did not return a usable key: {exc}") from exc
    return _KEY_CACHE[base]


def seal(document: dict, *, landing_url: str | None = None, submission_id: str | None = None) -> dict:
    """The envelope that will cross the public tier. No token needed to build it."""
    key_id, key = public_key(landing_url)
    payload = json.dumps(document, ensure_ascii=False).encode("utf-8")
    if len(payload) > wire.MAX_BODY_BYTES:
        raise SubmissionError(
            f"This submission is {len(payload)} bytes; the limit is {wire.MAX_BODY_BYTES}. "
            "A run with very many call records can reach this."
        )
    return wire.build_envelope(
        submission_id=submission_id or wire.new_submission_id(),
        sealed=crypto.seal(payload, key),
        key_id=key_id,
        transport=wire.TRANSPORT_SEALED,
    )


def post(envelope: dict, *, landing_url: str | None = None, token: str | None = None,
         timeout: float = 60.0) -> dict:
    base = (landing_url or config.LANDING_URL).rstrip("/")
    bearer = token if token is not None else config.SUBMIT_TOKEN
    if not bearer:
        raise SubmissionError(
            "No submit token. Set AMI_SUBMIT_TOKEN to the token issued for this "
            "client on the landing server."
        )
    request = urllib.request.Request(
        f"{base}/submissions",
        data=json.dumps(envelope).encode("utf-8"),
        method="POST",
        headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        with exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise SubmissionError(f"The landing server refused this submission (HTTP {exc.code}): {detail}") from exc
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
        raise SubmissionError(f"Cannot reach the landing server at {base}: {exc}") from exc


def submit(document: dict, *, landing_url: str | None = None, token: str | None = None,
           submission_id: str | None = None) -> dict:
    """Seal one finished document and hand it over.

    The submission id is the sender's, and stable across retries on purpose: a
    sealed box is randomised, so the same document sealed twice is different
    bytes, and anything derived from the ciphertext would turn a timed-out retry
    into a second run.
    """
    envelope = seal(document, landing_url=landing_url, submission_id=submission_id)
    result = post(envelope, landing_url=landing_url, token=token)
    return {"submission_id": envelope["submission_id"], **result}
