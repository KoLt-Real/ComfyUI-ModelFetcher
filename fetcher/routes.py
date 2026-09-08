"""The Model Fetcher HTTP API, under ``/cf_mf``.

- ``POST /cf_mf/analyze``: MD notes → parsed models + installed/duplicate status + remote
  sizes + per-category subfolders.
- ``POST /cf_mf/download``: queue jobs (destination resolved server-side, path-traversal
  proof). Progress is pushed on the websocket (``cf_mf.*`` events).
- ``POST /cf_mf/cancel`` / ``GET /cf_mf/status``.
- ``GET|POST /cf_mf/token`` (+ ``/clear``): state and registration of the HuggingFace token.

Every route is loopback-only (``access.local_only``): the caller must be on the machine that
runs ComfyUI, unless the operator sets ``CF_MF_ALLOW_REMOTE=1``. Every URL a job may fetch
must pass ``urlpolicy.check_url`` (host allow-list), and every destination is confined twice —
here, and again by the downloader before it writes.
"""

from __future__ import annotations

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor

from aiohttp import web
from server import PromptServer

from . import access, hf_token, remote, scanner, urlpolicy
from .downloader import manager
from .notes_parser import parse_notes

logger = logging.getLogger("comfyfactory.modelfetcher")
routes = PromptServer.instance.routes


async def _no_sizes() -> dict:
    """Empty counterpart of ``_probe_sizes`` when the note holds no URL at all."""
    return {}


async def _json_object(request) -> dict | None:
    """The request body as a JSON object — ``None`` for anything else (a 400, never a 500).

    ``request.json()`` happily returns a list or a string; every handler then calls ``.get``
    on it, and an ``AttributeError`` deep in a handler is a traceback in ComfyUI's log where a
    400 belongs.
    """
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


_BAD_JSON = {"ok": False, "error": "invalid JSON"}

# At plugin load time: reload a previously saved token into the environment.
hf_token.load_saved_into_env()


def _probe_sizes(urls: list[str]) -> dict[str, tuple[int | None, str | None]]:
    """HEAD the remote sizes, bounded parallelism. Must run off the event loop."""
    with ThreadPoolExecutor(max_workers=8) as ex:
        return dict(zip(urls, ex.map(remote.remote_size, urls)))


def _scan_disk(cat_cache: dict, all_dirs: list[str]) -> tuple[dict, dict]:
    """Disk index + per-category metadata. Must run off the event loop."""
    index = scanner.build_disk_index(all_dirs)
    categories = {}
    for cat_key, ci in cat_cache.items():
        display = ci.name or (cat_key or "")
        locations = scanner.list_locations(ci)
        # A category's subfolders are the union of its locations' subfolders, already
        # collected by list_locations: no need to walk the whole tree again.
        subfolders = sorted({s for loc in locations for s in loc["subfolders"]},
                            key=lambda s: (s != "", s.lower()))
        categories[display or "?"] = {
            "target_dir": ci.target_dir,
            "all_dirs": ci.all_dirs,
            "known": ci.known,
            "subfolders": subfolders,
            "locations": locations,
        }
    return index, categories


@routes.post("/cf_mf/analyze")
@access.local_only
async def analyze(request):
    body = await _json_object(request)
    if body is None:
        return web.json_response(_BAD_JSON, status=400)

    notes = body.get("notes") or []
    # Notes come from the client: a shape parse_notes cannot read must answer 400, like any
    # other malformed input — not leak an AttributeError as a 500.
    if not isinstance(notes, list) or not all(isinstance(n, dict) for n in notes):
        return web.json_response({"ok": False, "error": "invalid notes"}, status=400)
    refs = parse_notes(notes)

    # Category resolution (once per name).
    cat_cache: dict[str | None, scanner.CategoryInfo] = {}
    for r in refs:
        if r.category not in cat_cache:
            cat_cache[r.category] = scanner.resolve_category(r.category)

    all_dirs: list[str] = []
    for ci in cat_cache.values():
        all_dirs.extend(ci.all_dirs)
    urls = list({r.url for r in refs})

    # Disk walk (possibly over network shares) and remote HEADs: both block and both overlap,
    # so they go to threads. Leaving them on the event loop would freeze ALL of ComfyUI — the
    # progress websocket included — for the duration of the analysis.
    (index, categories), sizes = await asyncio.gather(
        asyncio.to_thread(_scan_disk, cat_cache, all_dirs),
        asyncio.to_thread(_probe_sizes, urls) if urls else _no_sizes(),
    )

    models = []
    for r in refs:
        ci = cat_cache[r.category]
        rsize, rerr = sizes.get(r.url, (None, None))
        cls = scanner.classify(r, ci, index, rsize)
        models.append({
            "id": r.id,
            "filename": r.filename,
            "url": r.url,
            "category": ci.name if ci.name else (r.category or None),
            "category_known": ci.known,
            "status": cls["status"],
            "remote_size": rsize,
            "remote_size_error": rerr,
            "local_matches": cls["local_matches"],
            "other_category_matches": cls["other_category_matches"],
            # Likely duplicate: relative path to push back into the loader node (or None).
            "relink": cls["relink"],
            "source_note": r.source_note,
        })

    # Every known category (feeds the select for unknown destinations).
    try:
        import folder_paths
        known_categories = sorted(
            k for k in folder_paths.folder_names_and_paths
            if k not in ("configs", "custom_nodes", "diffusers", "classifiers")
        )
    except Exception:
        known_categories = []

    return web.json_response({
        "ok": True,
        "models": models,
        "categories": categories,
        "known_categories": known_categories,
        # Same definition as /cf_mf/token: hf_token alone decides "a token is present".
        "hf_token": hf_token.get_token() is not None,
    })


@routes.post("/cf_mf/count")
@access.local_only
async def count(request):
    """Light count (no network request) of how many models are still to download.

    Drives the live refresh of the button badge on every workflow change. "To download" = no
    file of that name anywhere in the category's folders (the same-size/different-size
    duplicate distinction is not needed here).
    """
    body = await _json_object(request)
    if body is None:
        return web.json_response(_BAD_JSON, status=400)

    notes = body.get("notes") or []
    if not isinstance(notes, list) or not all(isinstance(n, dict) for n in notes):
        return web.json_response({"ok": False, "error": "invalid notes"}, status=400)
    refs = parse_notes(notes)
    cat_cache: dict[str | None, scanner.CategoryInfo] = {}
    for r in refs:
        if r.category not in cat_cache:
            cat_cache[r.category] = scanner.resolve_category(r.category)
    all_dirs: list[str] = []
    for ci in cat_cache.values():
        all_dirs.extend(ci.all_dirs)
    # Called on every workflow change: the walk goes to a thread (see analyze).
    index = await asyncio.to_thread(scanner.build_disk_index, all_dirs)

    missing = 0
    for r in refs:
        # A URL the download route would refuse is not "to download": the badge must not
        # advertise a model the popup then shows with its button disabled. Pure parsing, so
        # the route stays network-free.
        if urlpolicy.check_url(r.url):
            continue
        cls = scanner.classify(r, cat_cache[r.category], index, None)
        if cls["status"] in (scanner.ST_MISSING, scanner.ST_UNKNOWN):
            missing += 1

    return web.json_response({"ok": True, "total": len(refs), "missing": missing})


def _safe_join(base: str, *parts: str) -> str | None:
    """Join under ``base``, refusing any escape (``..``, absolute paths).

    Confinement leans on ``scanner.is_under`` — the same predicate as the classification — so
    that there is only ever one definition of "inside".
    """
    base_real = os.path.realpath(base)
    candidate = os.path.realpath(os.path.join(base, *parts))
    return candidate if scanner.is_under(candidate, base_real) else None


@routes.post("/cf_mf/download")
@access.local_only
async def download(request):
    body = await _json_object(request)
    if body is None:
        return web.json_response(_BAD_JSON, status=400)

    jobs = body.get("jobs") or []
    if not isinstance(jobs, list):
        return web.json_response({"ok": False, "error": "invalid jobs"}, status=400)
    queued, rejected = [], []
    for j in jobs:
        if not isinstance(j, dict):
            rejected.append({"id": None, "reason": "missing fields"})
            continue
        jid = j.get("id")
        url = (j.get("url") or "").strip()
        filename = (j.get("filename") or "").strip()
        category = j.get("category")
        subfolder = (j.get("subfolder") or "").strip().strip("/")
        base_dir = (j.get("base_dir") or "").strip()
        overwrite = bool(j.get("overwrite"))

        if not jid or not url or not filename:
            rejected.append({"id": jid, "reason": "missing fields"})
            continue
        # Scheme, no credentials, host on the allow-list: what the note's author may not
        # choose, the API does not accept either.
        reason = urlpolicy.check_url(url)
        if reason:
            rejected.append({"id": jid, "reason": reason})
            continue
        if os.sep in filename or (os.altsep and os.altsep in filename) or filename in (".", ".."):
            rejected.append({"id": jid, "reason": "invalid filename"})
            continue

        ci = scanner.resolve_category(category)
        # Root location: target_dir by default, or one of the category's destination
        # locations (extra paths included, output excluded) if the client asks for one —
        # never an arbitrary path.
        base = ci.target_dir
        if base_dir:
            wanted = os.path.realpath(base_dir)
            allowed = next((d for d in scanner.dest_dirs(ci) if os.path.realpath(d) == wanted), None)
            if allowed is None:
                rejected.append({"id": jid, "reason": "destination path refused"})
                continue
            base = allowed
        # The final target must stay under that location (a subfolder is allowed, but bounded).
        dest = _safe_join(base, subfolder, filename) if subfolder else \
            _safe_join(base, filename)
        if dest is None:
            rejected.append({"id": jid, "reason": "destination path refused"})
            continue

        manager.enqueue(jid, url, dest, overwrite=overwrite)
        queued.append({"id": jid, "dest": dest})

    return web.json_response({"ok": True, "queued": queued, "rejected": rejected})


@routes.post("/cf_mf/cancel")
@access.local_only
async def cancel(request):
    body = await _json_object(request) or {}
    if body.get("all"):
        manager.cancel_all()
        return web.json_response({"ok": True})
    jid = body.get("id")
    ok = manager.cancel(jid) if jid else False
    return web.json_response({"ok": ok})


@routes.get("/cf_mf/status")
@access.local_only
async def status(request):
    return web.json_response({"ok": True, **manager.status()})


# --- HuggingFace token: never returned to the client, only its state -------------

@routes.get("/cf_mf/token")
@access.local_only
async def get_token_state(request):
    return web.json_response({
        "ok": True,
        "present": hf_token.get_token() is not None,
        "from_file": hf_token.has_saved_file(),
    })


@routes.post("/cf_mf/token")
@access.local_only
async def set_token(request):
    body = await _json_object(request)
    if body is None:
        return web.json_response(_BAD_JSON, status=400)

    token = body.get("token")
    token = token.strip() if isinstance(token, str) else ""
    if not token:
        return web.json_response({"ok": False, "error": "empty token"}, status=400)
    # One printable line or nothing: the token file must never hold anything else, and junk
    # does not deserve a network round-trip.
    if not hf_token.looks_like_token(token):
        return web.json_response({"ok": False, "error": "invalid token"}, status=400)

    # HTTP round-trip to HuggingFace (up to 10 s): off the event loop.
    valid, username = await asyncio.to_thread(hf_token.validate, token)
    if not valid:
        return web.json_response({"ok": True, "valid": False})
    hf_token.save_token(token)
    # Gated models were cached as "401_gated": clear that so the re-analysis that follows
    # finally sees their size (otherwise ComfyUI would have to be restarted).
    remote.invalidate()
    return web.json_response({"ok": True, "valid": True, "username": username})


@routes.post("/cf_mf/token/clear")
@access.local_only
async def clear_token(request):
    hf_token.clear_token()
    remote.invalidate()
    return web.json_response({"ok": True})
