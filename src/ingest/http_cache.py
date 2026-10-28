"""Polite, cached HTTP GET for public JSON/CSV APIs.

Every response is written to disk before it is returned, so re-running a script
never downloads the same thing twice (idempotent + resumable).
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

import requests

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAW_DIR = PROJECT_ROOT / "data" / "raw"

_session = requests.Session()
_session.headers["User-Agent"] = "kalshi-vs-fed research project (github: kalshi-vs-fed)"
_last_request = 0.0


def _throttled_get(url: str, params: dict | None, min_interval: float, max_tries: int = 7) -> requests.Response:
    """GET with a minimum gap between requests and exponential backoff on 429/5xx."""
    global _last_request
    for attempt in range(max_tries):
        wait = _last_request + min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request = time.monotonic()
        try:
            resp = _session.get(url, params=params, timeout=60)
        except requests.RequestException as exc:
            backoff = 2 ** attempt
            log.warning("network error %s on %s; retrying in %ss", exc, url, backoff)
            time.sleep(backoff)
            continue
        if resp.status_code == 429 or resp.status_code >= 500:
            retry_after = resp.headers.get("Retry-After")
            backoff = float(retry_after) if retry_after and retry_after.isdigit() else 2 ** attempt
            log.warning("HTTP %s on %s; retrying in %ss", resp.status_code, url, backoff)
            time.sleep(backoff)
            continue
        return resp
    raise RuntimeError(f"gave up after {max_tries} tries: {url} {params}")


def get_json(url: str, cache_path: Path, params: dict | None = None, min_interval: float = 0.2) -> dict:
    """Return JSON from cache_path if present, else fetch, cache, and return it.

    Non-2xx responses (other than retried 429/5xx) are cached too, as
    {"__http_status": code, "__body": text}, so known-missing resources aren't re-requested.
    """
    if cache_path.exists():
        return json.loads(cache_path.read_text(encoding="utf-8"))
    resp = _throttled_get(url, params, min_interval)
    if resp.ok:
        data = resp.json()
    else:
        data = {"__http_status": resp.status_code, "__body": resp.text[:2000]}
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(cache_path)  # atomic: an interrupted run never leaves a half-written cache file
    return data


def get_text(url: str, cache_path: Path, params: dict | None = None, min_interval: float = 0.5) -> str:
    """Same as get_json but for text/CSV responses. Raises on non-2xx (nothing is cached)."""
    if cache_path.exists():
        return cache_path.read_text(encoding="utf-8")
    resp = _throttled_get(url, params, min_interval)
    resp.raise_for_status()
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = cache_path.with_suffix(cache_path.suffix + ".tmp")
    tmp.write_text(resp.text, encoding="utf-8")
    tmp.replace(cache_path)
    return resp.text
