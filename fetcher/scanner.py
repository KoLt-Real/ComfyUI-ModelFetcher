"""Category resolution, disk index of models, installed/duplicate classification.

Also provides the "relink" target: the relative path to hand to the loader node when the file
already exists in a subfolder of the category.

Depends on ``folder_paths`` (ComfyUI) at run time — imported lazily to stay testable.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .notes_parser import MODEL_EXTS, ModelRef

# Statuses returned to the frontend.
ST_INSTALLED = "installed"
ST_DUP_SAME = "duplicate_same_size"
ST_DUP_DIFF = "duplicate_diff_size"
ST_MISSING = "missing"
ST_UNKNOWN = "unknown_dest"


def _fp():
    import folder_paths  # type: ignore
    return folder_paths


def _output_root() -> str | None:
    """ComfyUI's output directory (realpath), or None when unavailable."""
    try:
        return os.path.realpath(_fp().get_output_directory())
    except Exception:
        return None


def _without_output(dirs: list[str]) -> list[str]:
    """``dirs`` minus the folders that sit under ComfyUI's output directory.

    ComfyUI registers ``output/<category>`` in ``folder_names_and_paths`` so that models
    WRITTEN by save nodes stay loadable. Those are therefore places where a model can be
    found — never places to download one to.
    """
    out = _output_root()
    if out is None:
        return list(dirs)
    return [d for d in dirs if not is_under(os.path.realpath(d), out)]


def dest_dirs(ci: CategoryInfo) -> list[str]:
    """Folders a download may be written to (⊆ ``all_dirs``).

    Single definition, shared by the destination menu and the download validation: what the
    UI does not offer, the API does not accept either.
    """
    return _without_output(ci.all_dirs) or list(ci.all_dirs)


@dataclass
class CategoryInfo:
    name: str            # name as requested (after normalisation)
    known: bool          # category registered in folder_names_and_paths
    target_dir: str      # download folder (created if missing)
    all_dirs: list[str]  # every scanned folder (extra paths included)


def resolve_category(name: str | None) -> CategoryInfo:
    """Map a folder name taken from a note to its real paths on disk.

    - goes through ``map_legacy`` (unet→diffusion_models, clip→text_encoders);
    - ``target_dir`` = the dir whose basename == category (avoids the trap where the first
      dir of ``diffusion_models`` is ``models/unet``); else the first existing dir; else the
      first one;
    - unknown category → fall back to ``models/<name>`` (created at download time).
    """
    fp = _fp()
    models_dir = fp.models_dir
    if not name:
        return CategoryInfo(name="", known=False, target_dir=models_dir, all_dirs=[])

    canonical = fp.map_legacy(name)
    reg = fp.folder_names_and_paths.get(canonical)
    if reg is not None:
        all_dirs = list(reg[0])
        # The default target is only looked for among the folders we are allowed to write
        # to (see _without_output): output/<category> is never a destination.
        candidates = _without_output(all_dirs) or all_dirs
        target = None
        for d in candidates:
            if os.path.basename(os.path.normpath(d)) == canonical:
                target = d
                break
        if target is None:
            target = next((d for d in candidates if os.path.isdir(d)),
                          candidates[0] if candidates else models_dir)
        return CategoryInfo(name=canonical, known=True, target_dir=target, all_dirs=all_dirs)

    # Unknown category: fall back straight under models/. Sub-paths are kept BUT every
    # traversal segment is stripped: a category comes from the client (download JSON) or from
    # a possibly untrusted workflow note and must never take the destination out of models/
    # (that would be an arbitrary file write).
    safe = name.replace("\\", "/")
    segs = [seg for seg in safe.split("/") if seg not in ("", ".", "..")]
    target = os.path.normpath(os.path.join(models_dir, *segs)) if segs else models_dir
    return CategoryInfo(name=name, known=False, target_dir=target, all_dirs=[target])


def _iter_model_files(root: str):
    if not os.path.isdir(root):
        return
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        # skip hidden directories (.cache/huggingface, .git)
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for fn in filenames:
            low = fn.lower()
            if any(low.endswith(ext) for ext in MODEL_EXTS):
                full = os.path.join(dirpath, fn)
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = -1
                yield fn, full, size


def build_disk_index(dirs: list[str]) -> dict[str, list[tuple[str, int]]]:
    """basename.lower() -> [(abs_path, size)] over all the given folders."""
    index: dict[str, list[tuple[str, int]]] = {}
    seen_paths: set[str] = set()
    for root in dirs:
        rp = os.path.realpath(root)
        if rp in seen_paths:
            continue
        seen_paths.add(rp)
        for fn, full, size in _iter_model_files(root):
            index.setdefault(fn.lower(), []).append((full, size))
    return index


def list_subfolders(all_dirs: list[str]) -> list[str]:
    """Relative subfolders (any depth) that could hold models.

    Always returns '' (the root) first. Hidden folders are ignored.
    """
    subs: set[str] = {""}
    for root in all_dirs:
        if not os.path.isdir(root):
            continue
        for dirpath, dirnames, _files in os.walk(root, followlinks=True):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            rel = os.path.relpath(dirpath, root)
            if rel != ".":
                subs.add(rel.replace(os.sep, "/"))
    ordered = sorted(subs, key=lambda s: (s != "", s.lower()))
    return ordered


def list_locations(ci: CategoryInfo) -> list[dict]:
    """Downloadable root locations of a category (main folder + extra paths).

    One dict per unique dir (deduplicated by realpath, ``folder_paths`` order preserved) with
    its own subfolders — so that the UI suggestions match the chosen location. Output folders
    (``output/<category>``) are excluded: see ``dest_dirs``.
    """
    locations: list[dict] = []
    seen: set[str] = set()
    target_real = os.path.realpath(ci.target_dir)
    for d in dest_dirs(ci):
        rp = os.path.realpath(d)
        if rp in seen:
            continue
        seen.add(rp)
        locations.append({
            "dir": d,
            "exists": os.path.isdir(d),
            "is_default": rp == target_real,
            "subfolders": list_subfolders([d]),
        })
    # Safety net: if target_dir does not show up in all_dirs (should not happen), add it.
    if target_real not in seen:
        locations.insert(0, {
            "dir": ci.target_dir,
            "exists": os.path.isdir(ci.target_dir),
            "is_default": True,
            "subfolders": list_subfolders([ci.target_dir]),
        })
    return locations


def _at_dir_root(match: dict) -> bool:
    """Does the copy sit at the ROOT of its folder, hence resolve by its bare name?

    ComfyUI searches EVERY folder registered for the category. That is what makes "installed"
    and "nothing to relink" equivalent: both decisions go through this one predicate,
    otherwise a row could read "likely duplicate" with no Relink button.

    Reads the value ``classify`` resolved rather than walking the folders again: one answer,
    one place, and no second opinion possible.
    """
    return "/" not in match["value"]


def relink_target(matches: list[dict], cat: CategoryInfo) -> dict | None:
    """Value to give the loader node's widget so it points at the copy already on disk.

    ``matches`` must be ``classify``'s IN-CATEGORY list: only those entries carry the
    ``value``/``root`` this reads (``other_category_matches`` entries do not).

    Among same-size copies, the one on the earliest of ``cat.all_dirs`` wins — that order
    is ``folder_paths`` order, which ``is_default: true`` in ``extra_model_paths.yaml``
    puts the user's preferred install at the head of. Ties (several copies on the same
    root) keep ``classify``'s alphabetical order, so the pick stays deterministic.

    ComfyUI lists a category's models as paths relative to each of its folders
    (``Flux/model.safetensors``); templates, on the other hand, point at the root. A file
    already at the root of one of those folders is reachable by its bare name → nothing to
    fix, ``None`` is returned.
    """
    pool = [m for m in matches if m["same_size"]] or matches
    # ``root`` is the verbatim string classify copied out of ``cat.all_dirs``, so index()
    # is an exact lookup; min() is stable, so equal ranks keep the list's own order.
    best = min(pool, key=lambda m: cat.all_dirs.index(m["root"])) if pool else None
    if best is None or _at_dir_root(best):
        return None
    return {"value": best["value"], "path": best["path"], "root": best["root"]}


def classify(ref: ModelRef, cat: CategoryInfo,
             index: dict[str, list[tuple[str, int]]],
             remote_size: int | None) -> dict:
    """Work out a model's status and its local matches.

    Duplicate = same filename present in the category's folders. The size comparison (when
    ``remote_size`` is known) grades it: identical → very likely the same file.
    """
    matches_in_cat: list[dict] = []
    matches_other: list[dict] = []

    cat_roots = [os.path.realpath(d) for d in cat.all_dirs]
    for full, size in index.get(ref.filename.lower(), []):
        rp = os.path.realpath(full)
        same_size = (remote_size is not None and size == remote_size)
        entry = {"path": full, "size": size, "same_size": same_size}
        # The loop deciding "is this copy in the category?" already finds the folder holding
        # it, which is exactly what the loader value is relative to: taking it here spares one
        # realpath() per folder per copy. That matters — the roots are often network shares and
        # /cf_mf/count reruns this whole classification on every workflow change.
        i = next((i for i, root in enumerate(cat_roots) if is_under(rp, root)), None)
        if i is None:
            matches_other.append(entry)
            continue
        entry["value"] = os.path.relpath(rp, cat_roots[i]).replace(os.sep, "/")
        entry["root"] = cat.all_dirs[i]
        matches_in_cat.append(entry)

    # Disk order is os.scandir order: neither sorted nor stable across filesystems. Leaving it
    # alone would make the copy pre-selected in the popup — and the one kept when two copies
    # share a widget value — differ between two machines holding identical trees. Case-folded,
    # because byte order puts every uppercase folder before every lowercase one ("Flux" before
    # "archive"), which is not the alphabetical order the README promises nor the order a user
    # scanning the menu reads it in.
    matches_in_cat.sort(key=lambda m: (m["value"].casefold(), m["value"]))
    matches_other.sort(key=lambda m: (m["path"].casefold(), m["path"]))

    if matches_in_cat:
        any_same = any(m["same_size"] for m in matches_in_cat)
        # Installed = the loader finds the file under its bare name, so it sits at the root
        # of ONE of the category's folders — not necessarily ``target_dir``: downloading into
        # an extra path is a legitimate user choice, and ComfyUI searches them all. Counting
        # only ``target_dir`` made a model we had just installed elsewhere look like a
        # duplicate with nothing to relink: a dead end.
        # Copies of a different size do not count: the bare name would resolve to another
        # version of the file, and that really is a duplicate to relink to the right copy.
        usable = [m for m in matches_in_cat if remote_size is None or m["same_size"]]
        if any(_at_dir_root(m) for m in usable):
            status = ST_INSTALLED
        elif remote_size is not None and not any_same:
            status = ST_DUP_DIFF
        else:
            status = ST_DUP_SAME
    elif not cat.known and ref.category is None:
        status = ST_UNKNOWN
    else:
        status = ST_MISSING

    return {
        "status": status,
        "local_matches": matches_in_cat,
        "other_category_matches": matches_other,
        # Only offered for a same-size duplicate: on a different size the local file is
        # probably another version, and pointing at it would be wrong.
        "relink": relink_target(matches_in_cat, cat) if status == ST_DUP_SAME else None,
    }


def allowed_write_roots() -> list[str]:
    """Every folder a download may land under: ``models/`` plus each registered category
    folder (extra paths included), minus ``output/<category>``.

    This is the union of ``dest_dirs()`` over every category, plus ``models_dir`` itself (an
    unknown category is created right under it). Realpaths, deduplicated; empty when
    ``folder_paths`` is unavailable — and an empty list allows nothing.
    """
    try:
        fp = _fp()
        dirs = [fp.models_dir]
        for entry in fp.folder_names_and_paths.values():
            dirs.extend(entry[0])
    except Exception:
        return []
    roots: list[str] = []
    for d in _without_output(dirs):
        rp = os.path.realpath(d)
        if rp not in roots:
            roots.append(rp)
    return roots


def is_allowed_dest(path: str) -> bool:
    """May a file be written at ``path``? Second line of defence, checked by the downloader
    itself right before it touches the disk: whatever it was handed, the destination must sit
    under one of the folders ComfyUI registered for models. ``routes.download`` already
    confines the path it builds; this makes the writer refuse anything else on its own.
    """
    rp = os.path.realpath(path)
    return any(is_under(rp, root) for root in allowed_write_roots())


def is_under(path: str, root: str) -> bool:
    """Is ``path`` ``root`` itself or inside its tree? (paths must already be normalised)

    The plugin's single confinement predicate: classification ("is this file in the
    category?") and the destination refusal at download time must answer exactly the same
    thing, otherwise a path accepted on one side could be refused on the other.
    """
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False
