"""HuggingFace token handling: environment lookup, persistent storage, validation.

A token pasted in the popup is applied immediately (`os.environ["HF_TOKEN"]`, re-read on every
request by `remote.py`/`downloader.py`) AND saved in ComfyUI's ``user`` folder (0600, outside
the plugin's repo) so it is reloaded on later starts — a beginner pastes it once. Same approach
as ``huggingface-cli``.

The token is NEVER logged nor returned to the client.
"""

from __future__ import annotations

import logging
import os

import requests

logger = logging.getLogger("comfyfactory.modelfetcher")

TOKEN_FILENAME = "cf_mf_hf_token.txt"

# ``HF_TOKEN`` is shared by the whole process (huggingface_hub, other custom nodes).
# We remember whether WE set it: otherwise clearing it from the popup would cut HF
# authentication for everything else in ComfyUI until the next restart.
_env_set_by_plugin = False


def _token_path() -> str:
    import folder_paths  # type: ignore
    return os.path.join(folder_paths.get_user_directory(), TOKEN_FILENAME)


def get_token() -> str | None:
    tok = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    return tok or None


# Hosts allowed to receive the token. It is only ever sent to HuggingFace: a workflow's notes
# (possibly shared, possibly untrusted) can list URLs to any host at all (Civitai, GitHub,
# CDNs…) and the Bearer must never be handed to them.
_HF_HOSTS = ("huggingface.co", "hf.co")


def is_hf_host(url: str) -> bool:
    """Does the URL point at HuggingFace? The only criterion for sending the token.

    Same suffix rule as the download allow-list (``urlpolicy.host_matches``): an entry or a
    subdomain of it — never a look-alike such as ``huggingface.co.evil.com``.
    """
    from urllib.parse import urlparse
    from .urlpolicy import host_matches
    return host_matches(urlparse(url).hostname or "", _HF_HOSTS)


def auth_headers(url: str) -> dict:
    """Authorization header for ``url`` — only when the host is HuggingFace."""
    tok = get_token()
    return {"Authorization": f"Bearer {tok}"} if (tok and is_hf_host(url)) else {}


def has_saved_file() -> bool:
    try:
        return os.path.isfile(_token_path())
    except Exception:
        return False


def load_saved_into_env() -> None:
    """At startup: when no token is in the environment, load the saved file."""
    if get_token():
        return
    try:
        path = _token_path()
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                tok = f.read().strip()
            if tok:
                global _env_set_by_plugin
                os.environ["HF_TOKEN"] = tok
                _env_set_by_plugin = True
                logger.info("cf_mf: HuggingFace token loaded from the user file.")
    except Exception:
        logger.exception("cf_mf: failed to load the saved token")


# Longest token shape HuggingFace has issued is well under this; the bound only keeps a
# runaway payload out of the file.
TOKEN_MAX_LEN = 512


def looks_like_token(token: str) -> bool:
    """Could this string be a token at all? One printable line, no whitespace, bounded.

    The token file must never hold anything else: the route checks this before spending a
    network round-trip on validation, and ``save_token`` refuses to write otherwise.
    """
    token = token or ""
    return (0 < len(token) <= TOKEN_MAX_LEN
            and token.isprintable()
            and not any(c.isspace() for c in token))


def save_token(token: str) -> None:
    """Apply the token immediately + persist it with mode 0600."""
    global _env_set_by_plugin
    token = (token or "").strip()
    if not looks_like_token(token):
        raise ValueError("refusing to save a malformed token")
    os.environ["HF_TOKEN"] = token
    _env_set_by_plugin = True
    path = _token_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Create the file with restricted permissions from the very start.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, token.encode("utf-8"))
    finally:
        os.close(fd)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def clear_token() -> None:
    """Forget the plugin's token.

    The environment is only cleaned when the plugin is the one that set the token: an
    ``HF_TOKEN`` exported by the user before launch belongs to the whole process
    (huggingface_hub, other extensions) and is not ours to remove.
    """
    global _env_set_by_plugin
    if _env_set_by_plugin:
        os.environ.pop("HF_TOKEN", None)
        _env_set_by_plugin = False
    try:
        os.remove(_token_path())
    except OSError:
        pass


def validate(token: str) -> tuple[bool, str | None]:
    """Check the token through whoami → (valid, username).

    Goes through ``huggingface_hub`` (shipped with ComfyUI): it is the one that follows the
    API's evolutions and honours ``HF_ENDPOINT``, hence mirror/enterprise instances. Falls back
    to a direct call when the library is missing.
    """
    token = (token or "").strip()
    if not token:
        return False, None
    try:
        from huggingface_hub import HfApi
    except Exception:
        return _validate_http(token)
    try:
        info = HfApi(token=token).whoami()
    except Exception:
        # Token refused OR network unavailable: neither case confirms anything.
        return False, None
    return True, (info or {}).get("name") or (info or {}).get("fullname")


def _validate_http(token: str) -> tuple[bool, str | None]:
    """Fallback without ``huggingface_hub``: whoami directly, ``HF_ENDPOINT`` honoured."""
    endpoint = (os.environ.get("HF_ENDPOINT") or "https://huggingface.co").rstrip("/")
    try:
        r = requests.get(
            f"{endpoint}/api/whoami-v2",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            return True, data.get("name") or data.get("fullname")
        return False, None
    except requests.RequestException:
        # Network unavailable: we cannot confirm, so we refuse to be safe.
        return False, None
