"""The handlers must not block the event loop, and must keep their contract — and refuse
anyone who is not on the machine running ComfyUI.

Needs aiohttp, which ships with ComfyUI. Run outside a ComfyUI environment, the test skips
itself (exit code 77) instead of turning red.
"""
import os, sys, types, asyncio, time, tempfile, importlib.util
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if importlib.util.find_spec("aiohttp") is None:
    print("skipped: aiohttp missing (it ships with ComfyUI)")
    sys.exit(77)

# --- ComfyUI stubs ----------------------------------------------------------
tmp = tempfile.mkdtemp()
ckpt = os.path.join(tmp, "models", "checkpoints")
os.makedirs(os.path.join(ckpt, "Flux"), exist_ok=True)
open(os.path.join(ckpt, "Flux", "flux1-dev.safetensors"), "wb").write(b"\0" * 100)

out_ckpt = os.path.join(tmp, "output", "checkpoints")
os.makedirs(os.path.join(out_ckpt, "Saved"), exist_ok=True)

fp = types.ModuleType("folder_paths")
fp.models_dir = os.path.join(tmp, "models")
# ComfyUI also registers output/<category> (so saved models can be loaded again).
fp.folder_names_and_paths = {"checkpoints": ([ckpt, out_ckpt], set())}
fp.get_user_directory = lambda: tmp
fp.get_output_directory = lambda: os.path.join(tmp, "output")
fp.map_legacy = lambda n: n
sys.modules["folder_paths"] = fp

class _Routes:
    def __init__(self): self.handlers = {}
    def post(self, path):
        def deco(fn):
            self.handlers[("POST", path)] = fn
            return fn
        return deco
    def get(self, path):
        def deco(fn):
            self.handlers[("GET", path)] = fn
            return fn
        return deco

srv = types.ModuleType("server")
class PromptServer:
    class _I:
        routes = _Routes()
        def send_sync(self, *a): pass
    instance = _I()
srv.PromptServer = PromptServer
sys.modules["server"] = srv

from fetcher import routes as R, remote

fails = []
def ck(n, c, e=""):
    print(("OK   " if c else "FAIL ") + n + ("" if c else "  -> " + str(e)))
    if not c: fails.append(n)

class FakeReq:
    # ``remote`` is aiohttp's socket peername: the local browser by default. ``headers``
    # carries a forged X-Forwarded-For so the test can prove the gate never reads it.
    def __init__(self, body, remote="127.0.0.1", headers=None):
        self._b = body
        self.query = {}
        self.remote = remote
        self.headers = headers or {}
    async def json(self): return self._b

def body_of(resp):
    import json
    return json.loads(resp.body.decode() if isinstance(resp.body, bytes) else resp.text)

NOTE = {"node_id": 1, "title": "t", "text":
        "**checkpoints**\n- [flux1-dev.safetensors](https://huggingface.co/x/resolve/main/flux1-dev.safetensors)\n"}

# --- a SLOW (2 s) simulated remote HEAD, to measure the blocking -------------
def slow_size(url):
    time.sleep(2.0)
    return (100, None)
remote._fetch = slow_size
remote.invalidate()

async def main():
    analyze = R.routes.handlers[("POST", "/cf_mf/analyze")]
    count = R.routes.handlers[("POST", "/cf_mf/count")]

    # The loop must stay responsive DURING the analysis: make it beat in parallel.
    ticks = {"n": 0}
    async def heartbeat():
        while True:
            await asyncio.sleep(0.05)
            ticks["n"] += 1

    hb = asyncio.create_task(heartbeat())
    t0 = time.monotonic()
    resp = await analyze(FakeReq({"notes": [NOTE]}))
    elapsed = time.monotonic() - t0
    hb.cancel()

    data = body_of(resp)
    ck("analysis succeeded", data["ok"] is True)
    ck("the model is seen as a likely duplicate",
       data["models"][0]["status"] == "duplicate_same_size", data["models"][0]["status"])
    ck("relink offered", data["models"][0]["relink"]["value"] == "Flux/flux1-dev.safetensors")
    ck("category metadata present",
       "checkpoints" in data["categories"] and data["categories"]["checkpoints"]["locations"])
    ck("subfolders = union of the locations",
       data["categories"]["checkpoints"]["subfolders"] == ["", "Flux"],
       data["categories"]["checkpoints"]["subfolders"])
    ck("the output folder does not pollute the destination menu",
       [l["dir"] for l in data["categories"]["checkpoints"]["locations"]] == [ckpt],
       data["categories"]["checkpoints"]["locations"])
    # ~2 s of HEAD: the loop should have beaten ~40 times if it were not blocked
    ck("the event loop stayed responsive during the analysis",
       ticks["n"] > 20, f"{ticks['n']} beats in {elapsed:.1f}s")

    # count: no network, but a walk -> must stay non-blocking as well
    ticks["n"] = 0
    hb = asyncio.create_task(heartbeat())
    c = body_of(await count(FakeReq({"notes": [NOTE]})))
    hb.cancel()
    ck("count: contract preserved", c["ok"] and c["total"] == 1 and c["missing"] == 0, c)

    # download: what the menu does not offer, the API must refuse.
    dl = R.routes.handlers[("POST", "/cf_mf/download")]
    enqueued = []
    R.manager.enqueue = lambda *a, **k: enqueued.append(a)
    job = {"url": "https://huggingface.co/x/resolve/main/a.safetensors",
           "filename": "a.safetensors", "category": "checkpoints"}
    d = body_of(await dl(FakeReq({"jobs": [
        dict(job, id="ok", base_dir=ckpt),
        dict(job, id="out", base_dir=out_ckpt),
    ]})))
    ck("download to a legitimate location: accepted",
       [q["id"] for q in d["queued"]] == ["ok"] and len(enqueued) == 1, d)
    ck("download to the output folder: refused",
       [(r["id"], r["reason"]) for r in d["rejected"]] == [("out", "destination path refused")], d)

    # Malformed notes (the shape, not the JSON): 400, never an unhandled 500. Seen live with
    # a hand-written payload sending bare strings where the frontend sends {node_id, title,
    # text} dicts — parse_notes would raise AttributeError deep in the request.
    for label, notes in (("a bare string in the list", ["just text"]),
                         ("notes not even a list", "just text")):
        for name, handler in (("analyze", analyze), ("count", count)):
            r = await handler(FakeReq({"notes": notes}))
            ck(f"{name}: {label} -> 400",
               r.status == 400 and body_of(r)["error"] == "invalid notes",
               (r.status, body_of(r)))

    # analysis with no URL at all (the _no_sizes path)
    empty = body_of(await analyze(FakeReq({"notes": [{"node_id": 2, "title": "", "text": "pas de lien"}]})))
    ck("note with no link -> 0 models, no error", empty["ok"] and empty["models"] == [], empty)

    # --- the host allow-list: what a note's author may not choose, the API refuses --------
    d = body_of(await dl(FakeReq({"jobs": [
        dict(job, id="cdn", url="https://cdn-lfs-us-1.hf.co/repo/abc", base_dir=ckpt),
        dict(job, id="evil", url="https://evil.example/a.safetensors", base_dir=ckpt),
        dict(job, id="lookalike", url="https://huggingface.co.evil.example/a", base_dir=ckpt),
        dict(job, id="creds", url="https://huggingface.co@evil.example/a", base_dir=ckpt),
        dict(job, id="ftp", url="ftp://huggingface.co/a", base_dir=ckpt),
    ]})))
    ck("an allow-listed CDN host is accepted", [q["id"] for q in d["queued"]] == ["cdn"], d)
    ck("hosts off the allow-list are refused with the host named",
       [(r["id"], r["reason"]) for r in d["rejected"]] == [
           ("evil", "host not allowed: evil.example"),
           ("lookalike", "host not allowed: huggingface.co.evil.example"),
           ("creds", "URL must not carry credentials"),
           ("ftp", "URL is not http(s)")], d["rejected"])

    # --- the token route never writes junk, and never validates it either -----------------
    set_token = R.routes.handlers[("POST", "/cf_mf/token")]
    validated = []
    R.hf_token.validate = lambda tok: (validated.append(tok), (False, None))[1]
    for label, tok, err in (("a token with a newline", "hf_abc\nrm -rf", "invalid token"),
                            ("a token with a space", "hf_abc def", "invalid token"),
                            ("an over-long token", "x" * 600, "invalid token"),
                            ("a blank token", "   ", "empty token"),
                            ("a non-string token", 123, "empty token")):
        r = await set_token(FakeReq({"token": tok}))
        ck(f"{label} -> 400", r.status == 400 and body_of(r)["error"] == err, (r.status, body_of(r)))
    ck("malformed tokens never reach validation", validated == [], validated)

    # --- the local-only gate: loopback peers only, the opt-out is the server's env --------
    status_h = R.routes.handlers[("GET", "/cf_mf/status")]
    gated = (("analyze", analyze, {"notes": [NOTE]}), ("count", count, {"notes": [NOTE]}),
             ("download", dl, {"jobs": []}), ("cancel", R.routes.handlers[("POST", "/cf_mf/cancel")], {}),
             ("status", status_h, None), ("token GET", R.routes.handlers[("GET", "/cf_mf/token")], None),
             ("token POST", set_token, {"token": "x"}),
             ("token/clear", R.routes.handlers[("POST", "/cf_mf/token/clear")], None))
    for label, remote in (("a LAN peer", "192.168.1.20"), ("a public peer", "203.0.113.9"),
                          ("a link-local peer", "fe80::1%eth0"), ("an unknown peer", None),
                          ("an unparsable peer", "not-an-ip")):
        for name, handler, body in gated:
            r = await handler(FakeReq(body, remote=remote))
            ck(f"{name} from {label} -> 403",
               r.status == 403 and body_of(r)["error"] == "local_only", (name, remote, r.status))
    for remote in ("127.0.0.1", "127.0.0.2", "::1", "::ffff:127.0.0.1"):
        r = await status_h(FakeReq(None, remote=remote))
        ck(f"loopback peer {remote} -> served", r.status == 200 and body_of(r)["ok"], r.status)
    r = await status_h(FakeReq(None, remote="192.168.1.20", headers={"X-Forwarded-For": "127.0.0.1"}))
    ck("a forged X-Forwarded-For does not open the gate", r.status == 403, r.status)
    r = await status_h(FakeReq(None, remote="127.0.0.1", headers={"X-Forwarded-For": "203.0.113.9"}))
    ck("a proxied loopback peer is still served (the header is never read)", r.status == 200)
    ck("the 403 says how to opt in",
       "CF_MF_ALLOW_REMOTE" in body_of(await status_h(FakeReq(None, remote="10.0.0.5")))["message"])
    os.environ["CF_MF_ALLOW_REMOTE"] = "1"
    try:
        r = await status_h(FakeReq(None, remote="192.168.1.20"))
        ck("CF_MF_ALLOW_REMOTE=1 -> remote peers served", r.status == 200, r.status)
    finally:
        del os.environ["CF_MF_ALLOW_REMOTE"]
    r = await status_h(FakeReq(None, remote="192.168.1.20"))
    ck("the opt-out is read live: unset -> refused again", r.status == 403, r.status)

asyncio.run(main())
print(f"\n{'FAILURES: ' + ', '.join(fails) if fails else 'async routes OK'}")
sys.exit(1 if fails else 0)
