"""Remote size of a model (used to compare duplicates).

On HuggingFace the size comes from ``huggingface_hub`` (the protocol is maintained upstream);
elsewhere, a hand-rolled HEAD then a fallback on ``Content-Length``.
Results are cached in memory per URL: a size is final, an error never is (network down, gated
model before the token is pasted) — it expires quickly, and ``invalidate()`` clears it when the
token changes.
"""

from __future__ import annotations

import logging
import time

import requests

from . import hf_token, urlpolicy

logger = logging.getLogger("comfyfactory.modelfetcher")

# An error is only kept briefly: just long enough that a single analysis does not probe the
# same dead URL three times, not long enough to survive a reconnection.
ERROR_TTL = 60.0  # s

# url -> (result, monotonic expiry | None when final)
_size_cache: dict[str, tuple[tuple[int | None, str | None], float | None]] = {}


def remote_size(url: str) -> tuple[int | None, str | None]:
    """→ (size_in_bytes | None, error_code | None). Result is cached."""
    hit = _size_cache.get(url)
    if hit is not None:
        result, expires = hit
        if expires is None or time.monotonic() < expires:
            return result
    result = _fetch(url)
    size, _err = result
    _size_cache[url] = (result, None if size is not None else time.monotonic() + ERROR_TTL)
    return result


def invalidate(url: str | None = None) -> None:
    """Forget cached results (all of them, or a single URL).

    Called when the HuggingFace token changes: the ``401_gated`` entries already seen must be
    probed again immediately, otherwise pasting a token would only take effect on restart.
    """
    if url is None:
        _size_cache.clear()
    else:
        _size_cache.pop(url, None)


def _fetch(url: str) -> tuple[int | None, str | None]:
    """Size of a URL. HuggingFace URLs go through the official library.

    A host outside the allow-list is answered without any network call: the probe itself is
    an outbound request the note's author would otherwise control.
    """
    if urlpolicy.check_url(url):
        return None, "host_not_allowed"
    if hf_token.is_hf_host(url):
        via_hub = _fetch_via_hub(url)
        if via_hub is not None:
            return via_hub
    return _fetch_generic(url)


def _fetch_via_hub(url: str) -> tuple[int | None, str | None] | None:
    """HF metadata via ``huggingface_hub`` — ``None`` when the library is missing.

    The HF protocol (HEAD without redirection, ``x-linked-size``, ``Accept-Encoding: identity``
    so that ``Content-Length`` is the real size) is maintained upstream; reimplementing it would
    mean drifting silently at HuggingFace's next change — and a wrong size flips a model between
    "duplicate" and "different content".
    """
    try:
        from huggingface_hub import get_hf_file_metadata
    except Exception:
        return None
    try:
        meta = get_hf_file_metadata(url, token=hf_token.get_token())
    except Exception as exc:
        # Exception classes vary between versions: read the HTTP status when there is one.
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (401, 403):
            return None, "401_gated"
        if status == 404:
            return None, "404"
        logger.debug("cf_mf: HF metadata unavailable for %s (%s)", url, exc)
        return None, "network"
    size = getattr(meta, "size", None)
    return (size, None) if size else (None, "no_size")


def _fetch_generic(url: str) -> tuple[int | None, str | None]:
    """Hand-rolled HEAD, for everything that is not HuggingFace (Civitai, GitHub, CDNs…)."""
    headers = hf_token.auth_headers(url)
    try:
        # 1) HEAD without following the redirect: HF exposes x-linked-size on the 302.
        r = requests.head(url, allow_redirects=False, timeout=10, headers=headers)
        if r.status_code in (401, 403):
            return None, "401_gated"
        xls = r.headers.get("x-linked-size") or r.headers.get("X-Linked-Size")
        if xls and xls.isdigit():
            return int(xls), None
        cl = r.headers.get("Content-Length")
        if r.status_code == 200 and cl and cl.isdigit():
            return int(cl), None
        # 2) Follow the redirect — by hand, every hop checked against the allow-list.
        r2 = urlpolicy.open_url("HEAD", url, headers=headers, timeout=10)
        if r2.status_code in (401, 403):
            return None, "401_gated"
        if r2.status_code == 404:
            return None, "404"
        cl2 = r2.headers.get("Content-Length")
        if cl2 and cl2.isdigit():
            return int(cl2), None
        return None, "no_size"
    except urlpolicy.HostNotAllowed:
        return None, "host_not_allowed"
    except requests.RequestException:
        return None, "network"
