# AGENTS.md — Comfy Model Fetcher

Working notes for AI coding agents. Read this before touching the code.

## What this is

A ComfyUI extension that **registers no nodes**. It adds a top-bar button, a popup, and HTTP
routes under `/cf_mf`. Everything is wrapped defensively: an error here must never stop ComfyUI
from starting (see `__init__.py`).

## Layout

```
__init__.py          route registration + empty V3 extension shim (no nodes)
fetcher/
  notes_parser.py    pure parsing — no ComfyUI import, no I/O. Keep it that way.
  scanner.py         folder_paths → categories, disk index, classification, relink target
  remote.py          remote size via HEAD (cached)
  downloader.py      queue + single worker thread, .part writes, websocket progress
  routes.py          the /cf_mf/* API
  access.py          loopback-only gate on every route (CF_MF_ALLOW_REMOTE opt-out)
  urlpolicy.py       host allow-list + the only code that follows redirects
  hf_token.py        token storage/validation + the HuggingFace-only auth header
web/                 frontend, plain ES modules — no build step, no bundler, no npm
  main.js            button, badge, note collection from the graph
  popup.js           the panel and all its state
  relink.js          reads/rewrites loader widgets in the open graph
docs/                README screenshots
tests/               see below — run them
```

## Invariants — do not break these

1. **The token goes to HuggingFace only.** `hf_token.auth_headers(url)` is the *single* place
   that builds an `Authorization` header, and it returns `{}` for any non-HF host. Model URLs
   come from workflow notes, which are untrusted input — a shared workflow must never be able
   to point the downloader at an attacker's server *with your credentials attached*. Never
   reintroduce a global `_auth_headers()`. On a redirect, `urlpolicy.open_url()` drops the
   header as soon as the origin (scheme, host or port — `requests`' own rule) changes and
   never adds one back: a bearer on a signed CDN URL is refused by the CDN, an `https` →
   `http` hop must not carry it in clear, and recomputing `auth_headers()` per hop would
   send it to `*.hf.co` where it is not wanted.
2. **Download destinations stay under the models directory — checked twice.**
   `scanner.resolve_category()` strips `..` and absolute segments; `routes._safe_join()`
   re-checks containment with `scanner.is_under()`; and `downloader._download()` re-checks
   the final `dest` with `scanner.is_allowed_dest()` (every registered model folder, `output/`
   excluded — with `dest_dirs()`' own fallback, so a category living only under `output/` is
   not offered by the menu and then refused by the worker) before its first `os.makedirs`. A client-supplied `category` is untrusted, and
   the worker trusts nothing it is handed. All three layers must stay.
3. **`base_dir` must be a folder already registered for that category** (main + extra paths).
   Never accept an arbitrary path from the client. The allowed set is `scanner.dest_dirs()` —
   the *same* function that builds the UI menu, so what is not offered is not accepted.
4. **Blocking I/O never runs on the aiohttp event loop.** `os.walk` over model directories
   (often network shares) and HEAD requests both go through `asyncio.to_thread`. Blocking the
   loop freezes all of ComfyUI, including websocket progress for the very downloads this
   plugin started.
5. **"Installed" means "the loader resolves the plain filename"** — i.e. the file is at the
   root of *any* of the category's roots, not only `target_dir`. Downloading into an extra
   path is a legitimate choice, and ComfyUI searches every registered root. `_at_dir_root()`
   decides this, and `relink_target()` uses the same predicate on purpose: a row labelled
   *Likely duplicate* must always have something to relink, or it is a dead end (status says
   "you have it", the only button says "download it again").
6. **A row is a grid of two independent lines**, `Link to … Relink` and `Save to … Download`
   (`.cf-mf-ops`, one `.cf-mf-opline` each, `display: contents` so both share the columns).
   `row.action` stays the download slot alone — progress, cancel, error and "✓ Installed" all
   land there — and `row.actionLink` holds the relink button. `flashRow()` takes the slot as an
   argument for the same reason: a message shown next to the wrong button blames it.
7. **Relink state is read from the graph, never cached.** `linkState()` is called on every
   render. A cached "already linked" flag would lie after an undo or a workflow switch. Which
   copy the row points at follows the same rule: `relinkPick()` prefers the user's choice, then
   whichever copy the graph already uses, and only then the server's automatic pick — a popup
   reopened on a relinked workflow must not claim the row is unlinked because it defaulted
   elsewhere. `relink_target()` stays the default — among same-size copies it takes the one on
   the earliest of `cat.all_dirs`, i.e. `folder_paths` order, which `is_default: true` in
   `extra_model_paths.yaml` puts the user's preferred install at the head of; alphabetical
   order only breaks ties within a root — and the invariant test checks it is always one of
   the copies the menu offers.
8. **Popup preferences are keyed per workflow.** Several workflows are open at once; state must
   not leak between tabs. See `currentWorkflowKey()` and its epoch fallback.
9. **Model ids must be unique.** `parse_notes()` disambiguates two different URLs that resolve
   to the same `category/filename`. The id is both the UI row key and the server job id.
10. **A `.part` file is only ever appended to for the same source URL.** `_part_path()` mixes a
   hash of the URL into the name; resume decisions rely on it. Never go back to a fixed
   `<dest>.part`, or two models sharing a filename will be spliced into one corrupt file.
11. **`huggingface_hub` is the HF protocol authority.** File sizes (`get_hf_file_metadata`) and
   token validation (`HfApi.whoami`) delegate to it; both fall back to a direct HTTP call only
   when the import fails. Do not reintroduce a hand-rolled HF HEAD dance — a wrong size flips a
   model between "duplicate" and "different content".
12. **Frontend has no build step.** Plain ES modules loaded by ComfyUI from `web/`. Do not add
   a bundler, TypeScript, or npm dependencies.
13. **Every `/cf_mf` route is loopback-only.** `access.local_only` wraps all eight handlers
   (`@routes.post(...)` *above* `@access.local_only`, so the registered function is the gated
   one) and reads `request.remote` — aiohttp's socket peername — never a header such as
   `X-Forwarded-For`. The only opt-out is the server's environment (`CF_MF_ALLOW_REMOTE=1`):
   a request can never open the gate for itself. A new route gets the decorator, no
   exceptions: `analyze` makes outbound requests and returns absolute paths, `status` leaks
   state, everything else mutates.
14. **Only allow-listed hosts are ever contacted, on every hop.** `urlpolicy.check_url()` is
   the one predicate (scheme, no credentials, host on `urlpolicy.allowed_hosts()`), applied in
   `routes.download`, `remote._fetch` (before any probe) and `downloader._download`.
   `urlpolicy.open_url()` is the *only* code that follows a redirect; it re-runs the predicate
   on each hop. Never pass `allow_redirects=True` to `requests` anywhere in the plugin. The
   list is extended by the operator alone (`CF_MF_ALLOWED_HOSTS`, `HF_ENDPOINT`), never by a
   note or a request. `check_url` does no DNS and no I/O — it runs on the event loop.
15. **The token file holds one printable line or nothing.** `hf_token.looks_like_token()`
   gates the route (400, before any network) and `save_token()` (raises). The token is still
   never returned to the client.

## Conventions

- Everything is in English: code, comments, docstrings, test names, UI strings, log messages
  and commit messages.
- ComfyUI frontend internals are probed defensively (`?.`, `try/catch`) — they churn between
  releases. When you touch `app.*` internals, assume the shape may be absent.
- `folder_paths` is imported lazily (inside functions) so the modules stay testable outside
  ComfyUI.
- Match the surrounding style; keep changes surgical.

## Tests

```bash
./tests/run_all.sh
```

Python and JS tests need nothing beyond what ComfyUI already provides; the browser tests need
`pip install playwright && playwright install chromium`. A test that self-skips exits 77.

| Suite | Covers |
|---|---|
| `tests/test_scanner_classify.py` | status ladder, proven equivalent to the pre-simplification version over the full truth table |
| `tests/test_scanner_relink.py` | relink target: subfolders, extra paths, size mismatch; the *no dead-end row* invariant over 162 disk layouts, and that the automatic pick is always one of the offered copies |
| `tests/test_security.py` | token host allowlist, download host allowlist (look-alikes, credentials in the URL, env extensions), the downloader's destination check, token hygiene, path-traversal confinement |
| `tests/test_notes_parser.py` | unique, stable, order-independent model ids |
| `tests/test_remote_cache.py` | sizes cached forever, errors briefly, `invalidate()` |
| `tests/test_downloader.py` | worker expiry/enqueue race, cancel after re-enqueue |
| `tests/test_downloader_resume.py` | resume against a real local server: Range honoured, ignored, stale `.part`, cancel keeps bytes; redirects followed with `Range` intact, a redirect off the allow-list refused after one request, a destination outside the model folders refused before any I/O |
| `tests/test_hf_delegation.py` | huggingface_hub delegation and its fallback, both paths |
| `tests/test_routes.py` | handlers stay off the event loop; response contract; the loopback-only gate on all eight routes (peers, forged headers, the env opt-out); host refusal reasons; malformed tokens answered 400 without validation |
| `tests/js/relink.test.js` | widget matching, subgraphs, promoted-widget double counting |
| `tests/e2e/test_popup.py` | Relink flow, the copy picker, per-workflow state, error/flash rows, and the relink line's independence from the download state machine |
| `tests/e2e/test_token_panel.py` | token panel: save, remove, env-vs-file |
| `tests/e2e/test_location_labels.py` | location labels stay unique and short, for the destination menu and for the offered copies (roots on different disks included); select cannot overflow the row |
| `tests/test_scanner_output_dirs.py` | `output/<category>` stays scanned but never becomes a destination |

The JS and browser tests copy the **real** `web/` sources into a temporary fake-ComfyUI tree at
run time — there is no duplicated copy to keep in sync.

## Traps found the hard way

- A **subgraph container node carries a promoted copy** of the inner node's widget. Both must be
  rewritten, but the pair counts as **one** target — otherwise Relink reports "×2" for a single
  loader. See `proxy` in `findTargets()`.
- ComfyUI lists model files **relative to each category root**, so a file at the root of an
  *extra* path is reachable by its bare name: no relink is needed there, only for real
  subfolders.
- **Case is never folded when comparing a widget value.** Two copies of one model can differ by
  the case of a folder alone (`Minimax/` against `MiniMax/`), and on a case-sensitive filesystem
  those are two different files. `relink.js` keeps `slashed()` (separators only) for every
  "is this the value?" question; `norm()` (case folded, both sides) decides only *known-ness* —
  whether a combo list proves the widget belongs to the value's category — and what gets
  WRITTEN is always the exact spelling of the pick, never a variant found in the list. Folding
  the comparisons made Relink write the copy the user had not picked while the tooltip promised
  theirs; folding only one side of the known-ness test made it unpassable for mixed-case values,
  which silently disabled the cross-category filter. `tests/js/relink.test.js` guards both.
- **`classify()` sorts its matches.** `os.scandir` order is neither sorted nor stable across
  filesystems, and it decides which copy the popup pre-selects and which of two copies sharing a
  widget value survives the dedup — two machines with identical trees would otherwise relink
  different files on the same click.
- Two copies sharing the same **relative path** under two different roots are one and the same
  choice: a widget value is resolved against every root in order, so both write the same string
  and load the same file — `get_filename_list()` deduplicates them for that reason. The copy
  picker does the same (`relinkCandidates()`); listing them separately offers a choice that
  does nothing. Found on a real install, where `models/unet` and `models/diffusion_models` are
  both roots of the `diffusion_models` category.
- `app.refreshComboInNodes()` resets widget values that are not in the refreshed list — never
  call it to "fix" a stale combo; add the value to `widget.options.values` instead.
- A widget's own value list is the only reliable way to tell two same-named models of different
  categories apart.
- The download worker exits after an idle timeout: it must de-register itself **while holding
  the lock**, or a job enqueued at that instant is never picked up.
- **`requests.RequestException` is an `OSError`.** In `_download`, the network clauses must
  come *before* `except OSError`, or every network error (and `HostNotAllowed`) is reported
  as `io`. It was that way for a while, silently.
- **An unregistered category that is a symlink out of `models/`** passes `routes._safe_join`
  (base is realpath'd) but is refused by `scanner.is_allowed_dest` (roots are realpath'd,
  the symlink target is not one of them). Registered categories are fine — their realpath
  *is* a root. Accepted: widening the roots to follow arbitrary symlinks would defeat the
  check.
- **Tests that download from a local server** must allow-list it (`CF_MF_ALLOWED_HOSTS=
  127.0.0.1` before importing the plugin) and point `folder_paths.models_dir` at their
  temporary directory: the worker refuses everything else, which is the point.
- A server may answer a `Range` request with plain `200` (whole file). Anything other than
  `206` must truncate and restart, never append — appending would double the file.
- `416` means the `.part` is larger than the resource: it belongs to another version of the
  file, so it is discarded and the download restarts once.
- `folder_names_and_paths` is **not** a list of download destinations. ComfyUI registers
  `output/<category>` there so that models saved by nodes stay loadable; those folders must
  keep being scanned (a model really can live there, and relink must point at it) but must
  never appear in the destination menu — `scanner.dest_dirs()` is the one place that draws
  the line, and `resolve_category()` never picks its default target outside it.
- Location labels must be built by `locationLabels()`, never by slicing a fixed number of
  path segments: two `extra_model_paths.yaml` roots very often share their tail
  (`models/clip`), and a fixed slice renders them identically. It grows the label to the
  smallest depth that is unique, then elides the middle — and falls back to the full label if
  eliding would make two entries look alike again.
- The "Link to" menu labels its roots over the category's **whole** location list (union'd
  with the copies' own roots — a copy can sit under `output/<category>`), not just the roots
  the copies happen to sit on: two subfolders of one install differ early (`models/unet`
  against `models/diffusion_models`), which stopped the depth escalation before the segment
  naming the install — on a machine with two installs, the menu could not say which one a
  copy lived in. Same set as the "Save to" menu ⇒ the same folder reads the same in both.
  `rootLabels()` in `popup.js` is where that set is built.
