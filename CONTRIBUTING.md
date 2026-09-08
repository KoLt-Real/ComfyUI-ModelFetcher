# Contributing

Bug reports and patches are welcome. This package downloads files onto your disk from
URLs it found in a workflow note, so it carries real security invariants — please read
them before your first patch.

## Invariants

[`AGENTS.md`](AGENTS.md) is the reference: it lists every invariant, with the code that
enforces it and the test that guards it, and every entry is there because the thing it
describes went wrong once. Nothing here overrides it. The ones that are security
invariants deserve the emphasis:

1. **The token stays on a leash** (AGENTS.md invariant 1). URLs come from workflow
   notes, i.e. from an untrusted source: a note authored by someone else must never be
   able to make your token leave for their server. `hf_token.auth_headers(url)` is the
   one place that builds an `Authorization` header for *note-supplied URLs*, and it
   returns `{}` for any non-HuggingFace host — the token-validation path
   (`hf_token.validate`, which honours the operator-controlled `HF_ENDPOINT`) is the
   deliberate exception, not a template to copy.
2. **Downloads stay under the models directory** (AGENTS.md invariants 2 and 3).
   Category names and subfolders also come from notes or from the client:
   `scanner.resolve_category()` strips traversal segments, `routes._safe_join()`
   re-checks containment, and `base_dir` must already be registered for the category.
   The downloader then re-checks the final path itself (`scanner.is_allowed_dest`) before
   writing. All three layers must stay — `tests/test_security.py` covers them.
3. **The routes answer the local machine only** (AGENTS.md invariant 13). Every handler is
   wrapped in `access.local_only`, which reads the socket peer address and nothing else;
   the opt-out is an environment variable on the server, never anything a request can
   send. A new route gets the decorator.
4. **Only allow-listed hosts are contacted, on every redirect hop** (AGENTS.md invariant
   14). `urlpolicy.check_url` is the predicate and `urlpolicy.open_url` the only place that
   follows a redirect — never `allow_redirects=True`. `tests/test_routes.py` and
   `tests/test_downloader_resume.py` cover both.

## Running the tests

```bash
./tests/run_all.sh                # the whole suite: Python + JS + browser (Playwright)
./tests/run_all.sh --strict       # any skipped test fails the run — use this before a PR
```

The Python and JS tests need nothing beyond `python3`, `node`, and what ComfyUI already
ships. The browser tests additionally need
`pip install playwright && playwright install chromium`; without them the suite skips
that tier and says so out loud. A skipped test is not a passing test — `--strict` turns
any skip into a failure, which is why it is the pre-PR command. There is no CI on this
repository yet: that check runs only if you run it.

## Style

See the Conventions section of [`AGENTS.md`](AGENTS.md). The short version: everything
is in English — code, comments, docstrings, test names, UI strings, log messages,
commit messages — and changes match the surrounding style rather than the style you
would pick.
