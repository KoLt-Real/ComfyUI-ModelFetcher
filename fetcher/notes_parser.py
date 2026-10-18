"""Parsing of a ComfyUI workflow's Markdown notes → list of models to download.

PURE functions (no ComfyUI dependency) so they stay testable outside the server.

Target format (as observed on ComfyUI's 7 templates):
- ``**<folder>**`` sections (= subfolder of ``models/``) followed by lists of
  ``- [file.safetensors](https://huggingface.co/...resolve/main/...)``;
- a fenced ```` ``` ```` "Model Storage Location" block holding a
  ``📂 ComfyUI/ → models/ → <folder>/ → file`` tree — **the most reliable source** for the
  destination (it also expresses subfolders, e.g. ``checkpoints/SDXL``);
- odd case (MoGe): a table + ``### Downloads`` with no bold heading → the category comes from
  the Storage Location tree only.

Category resolution order: Storage Location tree > ``**bold**`` heading > folder named in the
HuggingFace file path (registered categories only, see ``_category_from_url``) > None.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

# Recognised model file extensions.
MODEL_EXTS = (
    ".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".sft",
    ".gguf", ".onnx", ".pkl", ".pt2",
)

_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")
_RAW_URL_RE = re.compile(r"(?<![\(\w])(https?://[^\s<>\"')\]]+)")
_BOLD_HEADING_RE = re.compile(r"^\s*\*\*([^*]+?)\*\*\s*:?\s*$")
_MD_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s")


@dataclass
class ModelRef:
    filename: str
    url: str
    category: str | None  # subfolder of models/ (may include a nested subfolder)
    source_note: str = ""  # title of the note the link came from
    label: str = ""        # markdown label of the link (informational)
    # Set by ``parse_notes`` when two distinct URLs target the same (category, filename):
    # the id is both the row key in the UI AND the job id on the server, so it must stay
    # unique. Empty in the normal case.
    disambiguator: str = ""

    @property
    def id(self) -> str:
        cat = self.category or "?"
        base = f"{cat}/{self.filename}"
        return f"{base}#{self.disambiguator}" if self.disambiguator else base


def _has_model_ext(path: str) -> bool:
    low = path.lower()
    return any(low.endswith(ext) for ext in MODEL_EXTS)


def _filename_from_url(url: str) -> str:
    path = urlparse(url).path
    name = path.rsplit("/", 1)[-1] if path else ""
    return name


def _is_download_url(url: str, label: str) -> bool:
    """Keeps direct-download URLs, drops links to pages or documentation."""
    low_url = url.lower()
    path = urlparse(url).path.lower()
    if _has_model_ext(path):
        return True
    if "/resolve/" in low_url and "huggingface.co" in low_url:
        return True
    if "civitai.com/api/download" in low_url:
        return True
    if "github.com" in low_url and "/releases/download/" in low_url:
        return True
    # A label carrying a model extension + a plausible URL: best effort.
    if _has_model_ext(label) and ("huggingface.co" in low_url or "/resolve/" in low_url):
        return True
    return False


def _clean_url(url: str) -> str:
    """URL kept as-is for the download, apart from stripping trailing punctuation."""
    return url.rstrip(".,);]")


_HF_HOSTS = ("huggingface.co", "hf.co")


def _hf_repo_path(url: str) -> tuple[list[str], int] | None:
    """Path segments of a HuggingFace file URL, and the index of its ``blob``/``resolve`` verb.

    ``/<org>/<repo>/<verb>/<rev>/<path>``, or ``/datasets|spaces/<org>/<repo>/<verb>/...``.
    ``None`` when the path does not have that shape.
    """
    segs = urlparse(url).path.split("/")
    i = 4 if len(segs) > 4 and segs[1] in ("datasets", "spaces") else 3
    if len(segs) <= i + 2:
        return None
    return segs, i


def _hf_file_url(url: str) -> str:
    """HuggingFace file *page* (``/blob/``) → its download URL (``/resolve/``).

    A ``blob`` URL ends with the model's filename, so it passes for a download link, but it
    serves the HTML page that displays the file: downloaded as-is, that page lands on disk under
    the model's name. Real templates link it by mistake
    (``comfyanonymous/flux_text_encoders/blob/main/clip_l.safetensors``).
    """
    p = urlparse(url)
    host = (p.hostname or "").lower().removeprefix("www.")
    if host not in _HF_HOSTS:
        return url
    found = _hf_repo_path(url)
    if not found or found[0][found[1]] != "blob":
        return url
    segs, i = found
    segs[i] = "resolve"
    return urlunparse(p._replace(path="/".join(segs)))


def _category_from_url(url: str, is_category: Callable[[str], bool]) -> str | None:
    """Folder of ``models/`` named by a HuggingFace file path: the last resort.

    Repositories laid out for ComfyUI mirror its folders
    (``.../resolve/main/split_files/diffusion_models/x.safetensors``), and a note can link a
    file outside its ``**bold**`` lists (a tip, a community note). Only the path INSIDE the
    repository is read, never the org or repo name, nearest folder first; and only a name the
    caller registers counts: anything else would create a stray folder under ``models/``.
    """
    found = _hf_repo_path(url)
    if not found or found[0][found[1]] != "resolve":
        return None
    segs, i = found
    for seg in reversed(segs[i + 2:-1]):
        if seg and is_category(seg):
            return seg
    return None


def parse_storage_tree(block: str) -> dict[str, str]:
    """Parse a 'Model Storage Location' tree → {filename: path_relative_to_models}.

    Indentation is tracked with a stack of folders. Tolerant to ``│ ├── └──`` prefixes and
    to varying spacing. Nothing enters the stack before the ``models/`` level.
    """
    result: dict[str, str] = {}
    # stack: list of (indent, folder_name); folder_name is relative UNDER models/
    stack: list[tuple[int, str]] = []
    in_models = False

    for raw in block.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        # Length of the tree prefix (emoji, │, ├──, └──, spaces).
        stripped = re.sub(r"^[\s│├└─┃┣┗┆|`+\-]*", "", line)
        # depth ~ the column where the real text starts
        indent = len(line) - len(stripped)
        # drop a leading 📂 if present
        name = stripped
        name = re.sub(r"^📂\s*", "", name).strip()
        if not name:
            continue

        is_dir = name.endswith("/")
        clean = name.rstrip("/")

        if not in_models:
            if clean == "models":
                in_models = True
                stack = []
            continue

        # We are under models/. Adjust the stack to the indentation.
        while stack and stack[-1][0] >= indent:
            stack.pop()

        if is_dir or (not _has_model_ext(clean) and "." not in clean.rsplit("/", 1)[-1]):
            # folder
            if clean == "models":
                stack = []
                continue
            stack.append((indent, clean))
        else:
            # leaf file
            rel = "/".join(seg for (_i, seg) in stack)
            result[clean] = rel
    return result


def _parse_one_note(text: str, title: str,
                    is_category: Callable[[str], bool] | None = None) -> list[ModelRef]:
    refs: list[ModelRef] = []

    # 1) Storage Location tree(s): filename -> category mapping (takes precedence).
    tree_map: dict[str, str] = {}
    for m in _FENCE_RE.finditer(text):
        block = m.group(1)
        if "📂" in block or re.search(r"\bmodels/", block):
            tree_map.update(parse_storage_tree(block))

    # 2) Walk line by line to track the current bold heading.
    current_bold: str | None = None
    seen: set[tuple[str, str]] = set()

    def _add(label: str, url: str) -> None:
        url = _clean_url(url)
        if not _is_download_url(url, label):
            return
        # After the check, not before: a blob link to a README must stay a page link.
        url = _hf_file_url(url)
        fname = label.strip() if _has_model_ext(label) else _filename_from_url(url)
        if not fname:
            fname = _filename_from_url(url) or label.strip()
        if not fname:
            return
        # category: tree > bold heading > folder in the URL > None
        cat = tree_map.get(fname)
        if cat is None and current_bold:
            cat = current_bold
        if cat is None and is_category is not None:
            cat = _category_from_url(url, is_category)
        cat = _norm_category(cat)
        key = (fname.lower(), url)
        if key in seen:
            return
        seen.add(key)
        refs.append(ModelRef(filename=fname, url=url, category=cat,
                             source_note=title or "", label=label.strip()))

    # Parsing happens outside the fenced blocks (they hold the tree only, no download links).
    text_no_fence = _FENCE_RE.sub("\n", text)
    for line in text_no_fence.splitlines():
        hb = _BOLD_HEADING_RE.match(line)
        if hb:
            current_bold = hb.group(1)
            continue
        if _MD_HEADING_RE.match(line):
            # A new section ends the bold list: in image_qwen_image_layered, a "## FP8" tip
            # after the **vae** list links a diffusion model, which would land in vae/.
            current_bold = None
        matched_spans: list[tuple[int, int]] = []
        for m in _MD_LINK_RE.finditer(line):
            _add(m.group(1), m.group(2))
            matched_spans.append(m.span())
        # bare URLs outside the markdown links already captured
        for m in _RAW_URL_RE.finditer(line):
            if any(s <= m.start() < e for (s, e) in matched_spans):
                continue
            _add("", m.group(1))

    return refs


def _norm_category(cat: str | None) -> str | None:
    if not cat:
        return None
    c = cat.strip().strip("`").strip()
    c = c.replace("\\", "/")
    # "models/vae" -> "vae"
    c = re.sub(r"^models/", "", c)
    c = c.strip("/").strip()
    if not c:
        return None
    # normalise inner spaces of a bold heading ("Text Encoders" -> "text_encoders")
    if " " in c:
        c = c.lower().replace(" ", "_")
    return c


def parse_notes(notes: list[dict],
                is_category: Callable[[str], bool] | None = None) -> list[ModelRef]:
    """notes = [{node_id, title, text}] → deduplicated list of ModelRef.

    ``is_category(name)`` says whether a folder name is a registered model category; without
    it, the URL is never used to guess a category (the parser stays free of ComfyUI).
    """
    out: list[ModelRef] = []
    seen: set[tuple[str, str]] = set()
    for note in notes or []:
        text = note.get("text") or ""
        title = note.get("title") or ""
        if not text.strip():
            continue
        for ref in _parse_one_note(text, title, is_category):
            key = (ref.filename.lower(), ref.url)
            if key in seen:
                continue
            seen.add(key)
            out.append(ref)
    _disambiguate(out)
    return out


def _disambiguate(refs: list[ModelRef]) -> None:
    """Make ids unique when two models share the same (category, filename).

    Real case: two notes list ``model.safetensors`` under ``checkpoints`` from two different
    repositories. With nothing to tell them apart, the two rows overwrite each other in the UI
    and the second job cancels the first on the server. The suffix comes from the URL (not
    from the rank) so the id stays stable across analyses — remembered settings are keyed on it.
    """
    groups: dict[str, list[ModelRef]] = {}
    for ref in refs:
        groups.setdefault(ref.id, []).append(ref)
    for group in groups.values():
        if len(group) < 2:
            continue
        for ref in group:
            ref.disambiguator = hashlib.sha1(ref.url.encode("utf-8")).hexdigest()[:8]
