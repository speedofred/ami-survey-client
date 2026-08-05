"""Thin HTTP client for the survey API, with optional autostart.

The MCP server is the only consumer. Autostart means an agent can take the
survey without the human having to remember to boot the API first; set
AMI_AUTOSTART_API=0 to require an explicitly managed service.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

AUTOSTART = os.environ.get("AMI_AUTOSTART_API", "1") != "0"


class ApiUnavailable(RuntimeError):
    pass


class ApiCallFailed(RuntimeError):
    def __init__(self, status: int, payload):
        self.status = status
        self.payload = payload
        detail = payload.get("error") if isinstance(payload, dict) else payload
        super().__init__(f"API returned {status}: {detail}")


def _request(method: str, path: str, body: dict | None = None, timeout: float = 30.0):
    url = config.API_URL.rstrip("/") + path
    data = json.dumps(body or {}).encode() if method == "POST" else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    # Set when this machine's local MCP server submits to a hosted API that
    # requires a token. A purely local install leaves it unset and sends nothing.
    if config.API_TOKEN:
        req.add_header("Authorization", f"Bearer {config.API_TOKEN}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            ctype = resp.headers.get("Content-Type", "")
            return json.loads(raw) if "json" in ctype else raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = raw
        raise ApiCallFailed(exc.code, payload) from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise ApiUnavailable(f"Cannot reach the survey API at {config.API_URL}: {exc}") from exc


def is_up() -> bool:
    try:
        _request("GET", "/health", timeout=2.0)
        return True
    except (ApiUnavailable, ApiCallFailed):
        return False


def start_api() -> bool:
    """Launch the API as a detached background process and wait for it to answer."""
    log = config.DATA_DIR / "api.log"
    config.ensure_dirs()
    with log.open("a") as fh:
        subprocess.Popen(
            [sys.executable, "-m", "ami_survey.api"],
            cwd=str(config.PACKAGE_ROOT),
            stdout=fh,
            stderr=fh,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ, "PYTHONPATH": str(config.PACKAGE_ROOT)},
        )
    for _ in range(40):  # up to ~8s
        time.sleep(0.2)
        if is_up():
            return True
    return False


_api_confirmed = False


_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0", ""})


def _is_local(url: str) -> bool:
    """Is this URL a loopback address?

    Compares the parsed hostname exactly. Substring matching looks equivalent
    and is not: `127.0.0.10` contains `127.0.0.1`, and `localhost.example.com`
    contains `localhost`, so a remote host would be treated as local and a stray
    server spawned against it.
    """
    try:
        hostname = urllib.parse.urlparse(url).hostname
    except ValueError:
        return False
    return (hostname or "").lower() in _LOCAL_HOSTS


def ensure_api() -> None:
    """Health-check once per process, then trust it; individual calls surface
    their own connection errors if the API goes away mid-session."""
    global _api_confirmed
    if _api_confirmed:
        return
    if is_up():
        _api_confirmed = True
        return
    # Autostart only makes sense for a local API. Pointed at a hosted one, a
    # spawned local server can never satisfy the health check, so it would leave
    # a stray process listening and then fail anyway with a confusing message.
    if not _is_local(config.API_URL):
        raise ApiUnavailable(
            f"Cannot reach the survey API at {config.API_URL}. It is not a local "
            "address, so nothing was started here. Check the URL, the server, and "
            "your network."
        )
    if not AUTOSTART:
        raise ApiUnavailable(
            f"The survey API is not running at {config.API_URL} and autostart is "
            "disabled. Start it with: python3 -m ami_survey.api"
        )
    if not start_api():
        raise ApiUnavailable(
            f"Failed to autostart the survey API at {config.API_URL}. "
            f"Check {config.DATA_DIR / 'api.log'}."
        )
    _api_confirmed = True


def get(path: str, timeout: float = 30.0):
    ensure_api()
    return _request("GET", path, timeout=timeout)


def post(path: str, body: dict, timeout: float = 30.0):
    ensure_api()
    return _request("POST", path, body, timeout=timeout)
