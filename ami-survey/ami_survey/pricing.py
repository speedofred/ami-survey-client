"""Model pricing resolution via LiteLLM's price map.

Resolution order, most authoritative first:
  1. a local operator override in config/pricing_overrides.json
  2. the installed `litellm` package's model_cost table
  3. a cached copy of LiteLLM's model_prices_and_context_window.json
  4. a fresh fetch of that file (cached for later offline use)

If none of these resolve the observed model, prices are reported as null with an
"unresolved" provenance. Prices are never guessed - a missing price is recorded
as missing so the human analysing the results knows the cost figures are absent
rather than invented.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, asdict
from pathlib import Path

from . import config

_CACHE_FILE = config.CACHE_DIR / "model_prices_and_context_window.json"
_CACHE_META = config.CACHE_DIR / "model_prices_and_context_window.meta.json"

_price_map: dict | None = None
_price_map_source: str = "unresolved"


@dataclass
class ModelPricing:
    model_name: str
    matched_key: str | None
    provider: str | None
    input_price_per_1m: float | None
    output_price_per_1m: float | None
    cache_write_price_per_1m: float | None
    cache_read_price_per_1m: float | None
    source: str
    resolved: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _read_cache() -> dict | None:
    if not _CACHE_FILE.exists():
        return None
    try:
        return json.loads(_CACHE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _cache_age_seconds() -> float:
    try:
        return time.time() - _CACHE_FILE.stat().st_mtime
    except OSError:
        return float("inf")


def _fetch_remote() -> dict | None:
    if not config.ALLOW_NETWORK:
        return None
    try:
        with urllib.request.urlopen(config.LITELLM_PRICE_URL, timeout=20) as resp:
            raw = resp.read()
        data = json.loads(raw)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
        return None
    _write_cache(raw)
    return data


def _write_cache(raw: bytes) -> None:
    """Save the fetched price map, or carry on without saving it.

    Caching is an optimisation: the prices have already been fetched and are
    about to be used either way. Somewhere read-only - a service with
    ProtectSystem=strict, a checkout mounted read-only - must not turn a
    successful lookup into a failed run, which is what it did the first time
    ingest resolved a price under systemd.
    """
    try:
        config.CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_bytes(raw)
        _CACHE_META.write_text(
            json.dumps(
                {
                    "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "source": config.LITELLM_PRICE_URL,
                    "bytes": len(raw),
                },
                indent=2,
            )
        )
    except OSError:
        return


def load_price_map(force_refresh: bool = False) -> tuple[dict, str]:
    """Return (price_map, source_label). Cached in-process after first call."""
    global _price_map, _price_map_source
    if _price_map is not None and not force_refresh:
        return _price_map, _price_map_source

    if not force_refresh:
        try:  # installed litellm wins over any file copy
            import litellm  # type: ignore

            if getattr(litellm, "model_cost", None):
                _price_map = dict(litellm.model_cost)
                _price_map_source = f"litellm-package=={getattr(litellm, '__version__', '?')}"
                return _price_map, _price_map_source
        except Exception:  # noqa: BLE001 - litellm is optional
            pass

    cached = None if force_refresh else _read_cache()
    if cached is not None and _cache_age_seconds() <= config.LITELLM_CACHE_TTL_SECONDS:
        _price_map, _price_map_source = cached, f"litellm-cache ({_CACHE_FILE.name})"
        return _price_map, _price_map_source

    fresh = _fetch_remote()
    if fresh is not None:
        _price_map, _price_map_source = fresh, f"litellm-remote ({config.LITELLM_PRICE_URL})"
        return _price_map, _price_map_source

    if cached is None:
        cached = _read_cache()  # stale is still better than nothing, and is labelled as such
    if cached is not None:
        age_days = _cache_age_seconds() / 86400
        _price_map = cached
        _price_map_source = f"litellm-cache-stale ({age_days:.1f}d old)"
        return _price_map, _price_map_source

    _price_map, _price_map_source = {}, "unresolved (no litellm package, cache or network)"
    return _price_map, _price_map_source


def _overrides() -> dict:
    if config.PRICING_OVERRIDES_FILE.exists():
        try:
            return json.loads(config.PRICING_OVERRIDES_FILE.read_text()).get("models", {})
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _candidate_keys(model: str) -> list[str]:
    """Progressively looser lookup keys for a model identifier."""
    m = model.strip()
    cands = [m, m.lower()]
    # strip provider prefixes: "anthropic/claude-opus-5", "us.anthropic.claude-opus-5"
    if "/" in m:
        cands.append(m.split("/", 1)[1])
    if "." in m:
        cands.append(m.rsplit(".", 1)[-1])
    # strip a trailing date stamp: claude-opus-4-5-20251101 -> claude-opus-4-5
    stripped = re.sub(r"[-@]?20\d{6}(-v\d+:\d+)?$", "", m)
    if stripped != m:
        cands.append(stripped)
    seen, out = set(), []
    for c in cands:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def _per_1m(entry: dict, key: str) -> float | None:
    v = entry.get(key)
    return round(float(v) * 1_000_000, 6) if isinstance(v, (int, float)) else None


def resolve(model_name: str, provider_hint: str | None = None) -> ModelPricing:
    """Resolve per-million-token prices for an observed model identifier.

    `provider_hint` is the API the call actually went to, when the runtime knows
    it. The same model id can appear in the price map under several providers -
    `gemini-2.5-pro` is Vertex, `gemini/gemini-2.5-pro` is the Gemini API - and
    without the hint the first match wins, which mislabels `model_provider`.
    """
    override = _overrides().get(model_name)
    if override:
        return ModelPricing(
            model_name=model_name,
            matched_key=model_name,
            provider=override.get("model_provider"),
            input_price_per_1m=override.get("input_price_per_1m"),
            output_price_per_1m=override.get("output_price_per_1m"),
            cache_write_price_per_1m=override.get("cache_write_price_per_1m"),
            cache_read_price_per_1m=override.get("cache_read_price_per_1m"),
            source=f"operator-override ({config.PRICING_OVERRIDES_FILE.name})",
            resolved=True,
        )

    price_map, source = load_price_map()
    candidates = _candidate_keys(model_name)
    if provider_hint:
        candidates = [f"{provider_hint}/{model_name}"] + candidates
    for key in candidates:
        entry = price_map.get(key)
        if isinstance(entry, dict) and "input_cost_per_token" in entry:
            return ModelPricing(
                model_name=model_name,
                matched_key=key,
                provider=entry.get("litellm_provider"),
                input_price_per_1m=_per_1m(entry, "input_cost_per_token"),
                output_price_per_1m=_per_1m(entry, "output_cost_per_token"),
                cache_write_price_per_1m=_per_1m(entry, "cache_creation_input_token_cost"),
                cache_read_price_per_1m=_per_1m(entry, "cache_read_input_token_cost"),
                source=source,
                resolved=True,
            )

    return ModelPricing(
        model_name=model_name,
        matched_key=None,
        provider=None,
        input_price_per_1m=None,
        output_price_per_1m=None,
        cache_write_price_per_1m=None,
        cache_read_price_per_1m=None,
        source=source,
        resolved=False,
    )


def cache_status() -> dict:
    meta: dict = {}
    if _CACHE_META.exists():
        try:
            meta = json.loads(_CACHE_META.read_text())
        except (json.JSONDecodeError, OSError):
            meta = {}
    price_map, source = load_price_map()
    return {
        "source": source,
        "entries": len(price_map),
        "cache_file": str(_CACHE_FILE) if _CACHE_FILE.exists() else None,
        "cache_age_days": round(_cache_age_seconds() / 86400, 2)
        if _CACHE_FILE.exists()
        else None,
        "cache_meta": meta,
    }
