"""Path and runtime configuration for the AMI survey system.

Everything is resolved relative to the package root so the system can be moved
or symlinked without breaking, and every path is overridable via environment
variables so a user can point the API at a different inventory or data dir.
"""

from __future__ import annotations

import os
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent  # .../ami-survey
PROJECT_ROOT = PACKAGE_ROOT.parent  # the directory holding Collection_Inventory.csv


def _path_env(name: str, default: Path) -> Path:
    raw = os.environ.get(name)
    return Path(raw).expanduser().resolve() if raw else default


# The collection inventory is the single source of truth for the survey fields.
INVENTORY_CSV = _path_env("AMI_INVENTORY_CSV", PROJECT_ROOT / "Collection_Inventory.csv")

CONFIG_DIR = _path_env("AMI_CONFIG_DIR", PACKAGE_ROOT / "config")
DATA_DIR = _path_env("AMI_DATA_DIR", PACKAGE_ROOT / "data")

RUNS_DIR = DATA_DIR / "runs"  # in-flight runs (mutable)
RESPONSES_DIR = DATA_DIR / "responses"  # submitted survey responses (immutable)
CACHE_DIR = DATA_DIR / "cache"  # LiteLLM price map cache

GRADING_SCALE_FILE = CONFIG_DIR / "grading_scale.json"
WORKFLOW_CATEGORIES_FILE = CONFIG_DIR / "workflow_categories.json"
BENCHMARK_POLICY_FILE = CONFIG_DIR / "benchmark_policy.json"
PRICING_OVERRIDES_FILE = CONFIG_DIR / "pricing_overrides.json"

# The hosted survey. Deliberately a constant and not an environment variable:
# a published client submits here and nowhere else.
SURVEY_SERVICE_URL = "https://survey.agentbenchmark.dev"

#: The address this server is reachable at from outside, used to turn the links
#: in a submission into ones a human can click. Not derived from the request's
#: Host header: that is set by the caller, and a link is exactly the thing not to
#: build out of caller-controlled input. A fork on another domain sets this.
PUBLIC_URL = os.environ.get("AMI_PUBLIC_URL", SURVEY_SERVICE_URL).rstrip("/")

# The server half - the API, the field definitions, the scoring - ships only in
# the development checkout. Its presence is what separates "this machine can run
# a survey" from "this machine can take one", and it is the single switch behind
# every local-versus-hosted decision below.
SERVER_HALF_PRESENT = (PACKAGE_ROOT / "ami_survey" / "api.py").exists()

# API binding. The MCP server talks to the API over this URL.
if SERVER_HALF_PRESENT:
    API_HOST = os.environ.get("AMI_API_HOST", "127.0.0.1")
    API_PORT = int(os.environ.get("AMI_API_PORT", "8787"))
    API_URL = os.environ.get("AMI_API_URL", f"http://{API_HOST}:{API_PORT}")
else:
    # Nothing to bind, and AMI_API_URL is not read at all. A survey that lands
    # on the submitter's own disk is not a benchmark anyone can compare against,
    # so the published client has no local destination to be misdirected to -
    # by a stale environment variable, or on purpose.
    API_HOST, API_PORT = "", 0
    API_URL = SURVEY_SERVICE_URL

# Claude Code transcript location (the concrete telemetry source for this runtime).
CLAUDE_PROJECTS_DIR = _path_env(
    "AMI_CLAUDE_PROJECTS_DIR", Path.home() / ".claude" / "projects"
)

LITELLM_PRICE_URL = os.environ.get(
    "AMI_LITELLM_PRICE_URL",
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json",
)
# Refresh the cached price map when it is older than this many seconds.
LITELLM_CACHE_TTL_SECONDS = int(os.environ.get("AMI_LITELLM_CACHE_TTL", str(7 * 24 * 3600)))
# Set to "0" to forbid network access; the cache/local litellm package is then the only source.
ALLOW_NETWORK = os.environ.get("AMI_ALLOW_NETWORK", "1") != "0"

# Largest request body the API will read. A survey submission is tens of KB; the
# default leaves generous headroom while making a memory-exhaustion POST fail fast.
MAX_BODY_BYTES = int(os.environ.get("AMI_MAX_BODY_BYTES", str(5 * 1024 * 1024)))
# Most call records one run may carry, so a single run cannot grow unbounded.
MAX_CALLS_PER_RUN = int(os.environ.get("AMI_MAX_CALLS_PER_RUN", "5000"))

# Public-collection mode: set on a server that accepts submissions from other
# people's agents. Call records then keep only what the survey actually measures,
# and drop the things that describe the submitter's machine - shell commands they
# ran, and local filesystem paths carrying their username. Off by default, so a
# local install keeps full fidelity for its own analysis.
PUBLIC_MODE = os.environ.get("AMI_PUBLIC_MODE", "0") == "1"

# Require a bearer token on every mutating request. Off by default, so a local
# install keeps working with no tokens at all; turn it on before exposing the
# API to anyone else. Issue tokens with bin/ami-token.
REQUIRE_AUTH = os.environ.get("AMI_REQUIRE_AUTH", "0") == "1"
# Per-token sliding windows, applied only when REQUIRE_AUTH is on.
RATE_REQUESTS_PER_MINUTE = int(os.environ.get("AMI_RATE_REQUESTS_PER_MINUTE", "60"))
RATE_RUNS_PER_HOUR = int(os.environ.get("AMI_RATE_RUNS_PER_HOUR", "20"))

# Token the client sends when talking to a server that requires one. Set this on
# a machine whose local MCP server submits to a hosted API.
API_TOKEN = os.environ.get("AMI_API_TOKEN", "")

# --------------------------------------------------------------------------- #
# self-serve admission
# --------------------------------------------------------------------------- #

# Let an agent obtain its own submission token from POST /tokens, with no human
# in the loop. OFF by default and deliberately so: a local install has no
# business exposing it, and turning it on is a decision about who may write to
# your dataset. When off the endpoint does not exist at all rather than
# refusing, so a scanner learns nothing from the difference.
ALLOW_SELF_REGISTRATION = os.environ.get("AMI_ALLOW_SELF_REGISTRATION", "0") == "1"

# Address of the reverse proxy in front of this service. Set it and the API will
# believe the client address that proxy reports; leave it unset and every request
# is attributed to whatever opened the socket.
#
# This has to fail closed. Behind a proxy every connection arrives from
# 127.0.0.1, so without this a per-IP limit counts one bucket for the whole
# internet - but trusting X-Forwarded-For unconditionally is worse, because then
# any caller picks their own identity by setting a header.
TRUSTED_PROXY = os.environ.get("AMI_TRUSTED_PROXY", "")

# New tokens one address may mint per day, and a global ceiling that acts as a
# circuit breaker if something automated finds the endpoint.
RATE_TOKENS_PER_IP_PER_DAY = int(os.environ.get("AMI_RATE_TOKENS_PER_IP_PER_DAY", "15"))
RATE_TOKENS_PER_DAY = int(os.environ.get("AMI_RATE_TOKENS_PER_DAY", "50"))

# Lifetime submissions allowed on a token nobody vetted. Reaching it is not a
# punishment - it is the point at which a human should look at what arrived and
# decide whether to raise it.
MAX_SUBMISSIONS_SELF_ISSUED = int(os.environ.get("AMI_MAX_SUBMISSIONS_SELF_ISSUED", "5"))

# Serve MCP over HTTP at /mcp, so an agent can be given the survey's tools by a
# user who adds this server, rather than being told to fetch a page and obey it.
# Off by default like every other exposure switch. Everything submitted through
# it is self-reported - a remote server cannot read anyone's session log.
ALLOW_REMOTE_MCP = os.environ.get("AMI_ALLOW_REMOTE_MCP", "0") == "1"

# Origins permitted on the MCP endpoint. MCP requires Origin validation against
# DNS rebinding. claude.ai connects server-to-server and sends no Origin at all,
# which is allowed; a *present* Origin outside this list is refused.
MCP_ALLOWED_ORIGINS = tuple(
    o.strip() for o in os.environ.get(
        "AMI_MCP_ALLOWED_ORIGINS",
        "https://claude.ai,https://www.claude.ai,https://claude.com,"
        "https://chatgpt.com,https://chat.openai.com",
    ).split(",") if o.strip()
)


def ensure_dirs() -> None:
    for d in (DATA_DIR, RUNS_DIR, RESPONSES_DIR, CACHE_DIR, CONFIG_DIR):
        d.mkdir(parents=True, exist_ok=True)
