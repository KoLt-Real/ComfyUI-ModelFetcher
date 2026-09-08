"""Which hosts the plugin may contact, and the only redirect-following code path.

Model URLs come from workflow notes (possibly shared, possibly hostile) and from the client.
Without a host policy, a note could point the downloader at anything reachable from the
ComfyUI machine — the LAN, a cloud metadata endpoint, a service bound to localhost — and a
redirect from a friendly host could do the same one hop later. So:

- ``check_url`` decides whether a URL may be fetched at all. It is pure string work (no DNS,
  no I/O) so that it can run on the aiohttp event loop.
- ``open_url`` is the single place that follows redirects. Every hop goes through
  ``check_url`` again, ``Authorization`` is dropped as soon as the origin (scheme, host or
  port) changes — the rule ``requests`` applies on its own, and what signed CDN URLs
  require — and it is never added back.

The allow-list is operator-controlled: ``CF_MF_ALLOWED_HOSTS`` (comma-separated) extends it,
and the host of ``HF_ENDPOINT`` (HuggingFace mirrors) is accepted automatically.
"""

from __future__ import annotations

import logging
import os
from urllib.parse import urljoin, urlparse

import requests

logger = logging.getLogger("comfyfactory.modelfetcher")

# Where models actually come from, redirect targets included (checked against the real
# redirects: HF → *.hf.co, GitHub releases → *.githubusercontent.com, Civitai → signed
# *.r2.cloudflarestorage.com URLs). A subdomain of an entry is allowed, a look-alike is not.
DEFAULT_HOSTS: tuple[str, ...] = (
    "huggingface.co",
    "hf.co",
    "civitai.com",
    "github.com",
    "githubusercontent.com",
    "r2.cloudflarestorage.com",
)

HOSTS_ENV = "CF_MF_ALLOWED_HOSTS"
MAX_REDIRECTS = 10
_REDIRECT_CODES = (301, 302, 303, 307, 308)


class HostNotAllowed(requests.RequestException):
    """The URL (or a redirect hop) points at a host outside the allow-list."""

    def __init__(self, host: str, url: str):
        super().__init__("host not allowed: %s" % host)
        self.host = host
        self.url = url


class RedirectError(requests.RequestException):
    """A redirect without a usable ``Location``, or too many of them."""


def _clean_host(entry: str) -> str | None:
    """One env/endpoint entry → a bare lower-case hostname (``None`` when empty).

    ``https://mirror.example:8443/path``, ``mirror.example:8443`` and ``mirror.example`` all
    mean the same host: the allow-list is about hosts, ports are not part of the match.
    """
    entry = (entry or "").strip()
    if not entry:
        return None
    try:
        host = urlparse(entry if "://" in entry else "//" + entry).hostname or ""
    except ValueError:
        return None
    return host.strip().lower().rstrip(".") or None


def allowed_hosts() -> tuple[str, ...]:
    """The allow-list, re-read on every call so a test or an operator can change the env."""
    hosts = list(DEFAULT_HOSTS)
    for extra in (os.environ.get("HF_ENDPOINT"), *os.environ.get(HOSTS_ENV, "").split(",")):
        h = _clean_host(extra) if extra else None
        if h and h not in hosts:
            hosts.append(h)
    return tuple(hosts)


def host_matches(host: str, allowed: tuple[str, ...] | list[str]) -> bool:
    """Exact match or subdomain of an entry — ``cdn-lfs.hf.co`` yes, ``hf.co.evil.com`` no."""
    host = (host or "").lower().rstrip(".")
    return any(host == a or host.endswith("." + a) for a in allowed)


def host_allowed(host: str) -> bool:
    return host_matches(host, allowed_hosts())


HOST_REASON = "host not allowed: "


def refusal_code(reason: str) -> str:
    """Short code for a ``check_url`` reason: the host is the only fixable one on the
    operator's side (``CF_MF_ALLOWED_HOSTS``), so the UI must not blame the allow-list for a
    URL refused for its scheme or for carrying credentials."""
    return "host_not_allowed" if reason.startswith(HOST_REASON) else "url_refused"


def check_url(url: str) -> str | None:
    """Reason the URL must not be fetched, or ``None`` when it may be.

    Pure parsing: safe to call from a request handler.
    """
    try:
        parsed = urlparse(url)
    except ValueError:
        return "invalid URL"
    if parsed.scheme.lower() not in ("http", "https"):
        return "URL is not http(s)"
    # ``http://huggingface.co@evil.com/`` reads as HF to a human and goes to evil.com.
    if parsed.username is not None or "@" in parsed.netloc:
        return "URL must not carry credentials"
    try:
        parsed.port  # raises on ``host:notanumber``
    except ValueError:
        return "invalid URL"
    host = (parsed.hostname or "").rstrip(".")
    if not host:
        return "invalid URL"
    if not host_allowed(host):
        return HOST_REASON + host
    return None


def _origin(url: str) -> tuple[str, str, int | None]:
    p = urlparse(url)
    return (p.scheme.lower(), (p.hostname or "").lower(), p.port)


def open_url(method: str, url: str, *, headers: dict | None = None, **kw) -> requests.Response:
    """``requests.request`` with redirects followed by hand, every hop policy-checked.

    ``headers`` are sent on every hop (``Range`` included, so a resume survives a redirect)
    except ``Authorization``, dropped the moment the origin — scheme, host or port — changes
    and never re-added: the token stays on the leash of ``hf_token.auth_headers`` (invariant
    1), it never travels down an ``https`` → ``http`` hop in clear, and signed CDN URLs
    refuse a request carrying both a signature and a bearer. Same rule as ``requests``'
    ``should_strip_auth``.
    """
    h = dict(headers or {})
    cur = url
    for _ in range(MAX_REDIRECTS + 1):
        reason = check_url(cur)
        if reason:
            if reason.startswith(HOST_REASON):
                raise HostNotAllowed(urlparse(cur).hostname or "", cur)
            raise RedirectError("%s (redirected to %s)" % (reason, cur))
        r = requests.request(method, cur, allow_redirects=False, headers=h, **kw)
        if r.status_code not in _REDIRECT_CODES:
            return r
        location = r.headers.get("Location")
        r.close()  # a streamed 3xx holds its connection until closed
        if not location:
            raise RedirectError("redirect without a Location header from %s" % cur)
        nxt = urljoin(cur, location)
        if _origin(nxt) != _origin(cur):
            h.pop("Authorization", None)
        cur = nxt
    raise RedirectError("too many redirects (%d) from %s" % (MAX_REDIRECTS, url))
