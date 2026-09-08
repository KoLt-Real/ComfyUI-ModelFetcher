"""Who may call the ``/cf_mf`` routes: the machine running ComfyUI, and nobody else.

The routes queue downloads onto the disk, cancel them, and write or delete the HuggingFace
token file. With ``--listen`` ComfyUI answers the whole network, and nothing in ComfyUI
authenticates a caller — so the plugin checks the peer address itself. Only loopback peers
are served; everything else gets a 403 that says how to opt in.

The check reads ``request.remote`` — aiohttp's socket peername, never a header such as
``X-Forwarded-For`` — so it cannot be spoofed by the caller. The opt-out
(``CF_MF_ALLOW_REMOTE=1``) is an environment variable read on the server: operator-controlled,
like ``HF_ENDPOINT``. Running ComfyUI behind a reverse proxy on the same host, or in a
container with a mapped port, is exactly the case that needs it.
"""

from __future__ import annotations

import functools
import ipaddress
import logging
import os

from aiohttp import web

logger = logging.getLogger("comfyfactory.modelfetcher")

REMOTE_ENV = "CF_MF_ALLOW_REMOTE"
_TRUE = ("1", "true", "yes", "on")

REFUSED_MESSAGE = (
    "Model Fetcher only answers requests from the machine running ComfyUI. "
    "Set %s=1 in ComfyUI's environment to allow remote clients." % REMOTE_ENV
)

_warned = False


def remote_allowed() -> bool:
    """Has the operator opted out of the loopback-only rule? (env, read on every call)"""
    global _warned
    allowed = (os.environ.get(REMOTE_ENV) or "").strip().lower() in _TRUE
    if allowed and not _warned:
        _warned = True
        logger.warning("cf_mf: %s is set — the /cf_mf routes answer remote clients.", REMOTE_ENV)
    return allowed


def is_local_request(request) -> bool:
    """Does the request come from a loopback address? Unknown or unparsable → no."""
    remote = getattr(request, "remote", None)
    if not remote:
        return False
    try:
        addr = ipaddress.ip_address(remote)
    except ValueError:
        return False
    # ``::ffff:127.0.0.1`` is how a dual-stack socket reports an IPv4 loopback peer.
    mapped = getattr(addr, "ipv4_mapped", None)
    return (mapped or addr).is_loopback


def local_only(handler):
    """Route decorator: 403 for any peer that is not loopback, unless opted out."""
    @functools.wraps(handler)
    async def gated(request, *args, **kwargs):
        if not (remote_allowed() or is_local_request(request)):
            return web.json_response(
                {"ok": False, "error": "local_only", "message": REFUSED_MESSAGE}, status=403)
        return await handler(request, *args, **kwargs)
    return gated
