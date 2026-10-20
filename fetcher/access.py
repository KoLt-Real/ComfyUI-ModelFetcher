"""Who may call the ``/cf_mf`` routes: the machine running ComfyUI, and nobody else.

The routes queue downloads onto the disk, cancel them, and write or delete the HuggingFace
token file. With ``--listen`` ComfyUI answers the whole network, and nothing in ComfyUI
authenticates a caller — so the plugin checks the peer address itself. Only loopback peers
are served; everything else gets a 403 that says how to opt in.

The check reads ``request.remote`` — aiohttp's socket peername, never a header such as
``X-Forwarded-For`` — so it cannot be spoofed by the caller. Two operator-controlled
environment variables widen the rule, read on the server like ``HF_ENDPOINT``:

- ``CF_MF_TRUSTED_PROXIES``: a comma-separated list of addresses or networks
  (``10.40.10.237, 10.40.0.0/16``) whose requests are served. For a reverse proxy on
  another host (a router in front of several ComfyUI servers): the proxy's address is
  trusted, the rest of the network still gets a 403.
- ``CF_MF_ALLOW_REMOTE=1``: every peer is served. For a container with a mapped port,
  where the browser arrives from the bridge address, or a trusted single-user network.
"""

from __future__ import annotations

import functools
import ipaddress
import logging
import os

from aiohttp import web

logger = logging.getLogger("comfyfactory.modelfetcher")

REMOTE_ENV = "CF_MF_ALLOW_REMOTE"
PROXIES_ENV = "CF_MF_TRUSTED_PROXIES"
_TRUE = ("1", "true", "yes", "on")

REFUSED_MESSAGE = (
    "Model Fetcher only answers requests from the machine running ComfyUI. "
    "Set %s=<proxy address> for a reverse proxy, or %s=1 to allow every client, "
    "in ComfyUI's environment." % (PROXIES_ENV, REMOTE_ENV)
)

_warned = False
_warned_bad: set = set()


def remote_allowed() -> bool:
    """Has the operator opted out of the loopback-only rule? (env, read on every call)"""
    global _warned
    allowed = (os.environ.get(REMOTE_ENV) or "").strip().lower() in _TRUE
    if allowed and not _warned:
        _warned = True
        logger.warning("cf_mf: %s is set — the /cf_mf routes answer remote clients.", REMOTE_ENV)
    return allowed


def peer_address(request):
    """The peer as an ``ipaddress`` object, IPv4 unmapped; None when unknown or unparsable."""
    remote = getattr(request, "remote", None)
    if not remote:
        return None
    try:
        addr = ipaddress.ip_address(remote)
    except ValueError:
        return None
    # ``::ffff:127.0.0.1`` is how a dual-stack socket reports an IPv4 loopback peer.
    return getattr(addr, "ipv4_mapped", None) or addr


def is_local_request(request) -> bool:
    """Does the request come from a loopback address? Unknown or unparsable → no."""
    addr = peer_address(request)
    return addr is not None and addr.is_loopback


def trusted_proxies() -> list:
    """The networks of ``CF_MF_TRUSTED_PROXIES`` (env, read on every call). A malformed
    entry is skipped and reported once; it never opens anything."""
    networks = []
    for raw in (os.environ.get(PROXIES_ENV) or "").split(","):
        entry = raw.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            if entry not in _warned_bad:
                _warned_bad.add(entry)
                logger.warning("cf_mf: %s entry %r is not an address or network, ignored.",
                               PROXIES_ENV, entry)
    return networks


def is_trusted_proxy(request) -> bool:
    """Does the request come from a proxy the operator trusts?"""
    addr = peer_address(request)
    if addr is None:
        return False
    return any(addr in network for network in trusted_proxies() if addr.version == network.version)


def local_only(handler):
    """Route decorator: 403 for any peer that is not loopback or a trusted proxy, unless
    opted out."""
    @functools.wraps(handler)
    async def gated(request, *args, **kwargs):
        if not (remote_allowed() or is_local_request(request) or is_trusted_proxy(request)):
            return web.json_response(
                {"ok": False, "error": "local_only", "message": REFUSED_MESSAGE}, status=403)
        return await handler(request, *args, **kwargs)
    return gated
