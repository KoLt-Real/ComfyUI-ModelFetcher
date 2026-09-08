"""Download resume: Range honoured, Range ignored, .part kept or discarded — and the two
guards the worker applies on its own: redirects stay on the host allow-list, and the
destination stays under the model folders.

Serves a real local HTTP server — no external network access.
"""
import os, sys, types, http.server, socketserver, threading, tempfile
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# The worker refuses any destination outside the registered model folders, and any host
# outside the allow-list: the test's tmp dir is the models dir, and the local server is
# allow-listed the way an operator would do it (CF_MF_ALLOWED_HOSTS).
tmp = tempfile.mkdtemp()
os.environ["CF_MF_ALLOWED_HOSTS"] = "127.0.0.1"
fp = types.ModuleType("folder_paths")
fp.models_dir=tmp
fp.folder_names_and_paths={}
fp.get_user_directory=lambda:"/u"
fp.map_legacy=lambda n:n
sys.modules["folder_paths"] = fp

import fetcher.downloader as dl

events = []
dl._push = lambda ev, payload: events.append((ev, payload))

fails = []
def ck(n, c, e=""):
    print(("OK   " if c else "FAIL ") + n + ("" if c else "  -> " + str(e)))
    if not c: fails.append(n)

BODY = bytes(range(256)) * 400          # 102,400 bytes of verifiable content
HALF = len(BODY) // 2

class Handler(http.server.BaseHTTPRequestHandler):
    honour_range = True                 # flipped by the tests
    served_ranges = []

    def do_GET(self):
        if self.path == "/redir":            # same host: must be followed, Range and all
            self.send_response(302)
            self.send_header("Location", "/model.safetensors")
            self.end_headers()
            return
        if self.path == "/evil":             # off the allow-list: must NOT be followed
            self.send_response(302)
            self.send_header("Location", "http://blocked.invalid/model.safetensors")
            self.end_headers()
            return
        if self.path == "/echo-auth":        # says whether Authorization arrived
            self.send_response(200)
            self.send_header("X-Got-Auth", "yes" if self.headers.get("Authorization") else "no")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith("/redir-to-"):   # /redir-to-<port>: same host, other port
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.path.rsplit('-', 1)[1]}/echo-auth")
            self.end_headers()
            return
        rng = self.headers.get("Range")
        Handler.served_ranges.append(rng)
        start = 0
        if rng and Handler.honour_range:
            start = int(rng.split("=")[1].split("-")[0])
            if start >= len(BODY):
                self.send_response(416)
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(BODY)-1}/{len(BODY)}")
        else:
            self.send_response(200)
        chunk = BODY[start:]
        self.send_header("Content-Length", str(len(chunk)))
        self.end_headers()
        self.wfile.write(chunk)

    def log_message(self, *a): pass

socketserver.TCPServer.allow_reuse_address = True
srv = socketserver.TCPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]
URL = f"http://127.0.0.1:{PORT}/model.safetensors"

dest = os.path.join(tmp, "model.safetensors")
part = dl._part_path(dest, URL)

# ---------- the .part name isolates the sources -----------------------------
other = dl._part_path(dest, "http://elsewhere/model.safetensors")
ck("two URLs -> two distinct .part files", part != other, part)
ck("the .part sits next to the destination", part.startswith(dest) and part.endswith(".part"))
ck("stable name for the same URL", part == dl._part_path(dest, URL))

mgr = dl.DownloadManager()

def run(job_id, overwrite=False):
    events.clear()
    mgr._download(dl._Job(job_id, URL, dest, overwrite))
    return [e for e in events]

# ---------- nominal resume --------------------------------------------------
open(part, "wb").write(BODY[:HALF])        # a previous attempt stopped halfway
Handler.served_ranges.clear()
evs = run("resume")
ck("Range requested at the right position", Handler.served_ranges == [f"bytes={HALF}-"],
   Handler.served_ranges)
ck("download completed", any(e[0] == "cf_mf.done" for e in evs), evs)
ck("final file complete and intact", open(dest, "rb").read() == BODY)
ck(".part removed after success", not os.path.exists(part))
prog = [p for e, p in evs if e == "cf_mf.progress"]
ck("progress starts from the resumed position, not from 0",
   all(p["downloaded"] >= HALF for p in prog), prog[:2])
ck("the announced total is that of the whole file",
   all(p["total"] == len(BODY) for p in prog), prog[:2])

# ---------- a server that ignores Range -------------------------------------
os.remove(dest)
open(part, "wb").write(BODY[:HALF])
Handler.honour_range = False
evs = run("noresume")
ck("server ignoring Range: the file is correct all the same",
   open(dest, "rb").read() == BODY)
Handler.honour_range = True

# ---------- .part too large (416) -> start over -----------------------------
os.remove(dest)
open(part, "wb").write(BODY + b"surplus")
evs = run("stale")
ck("a stale .part does not corrupt the result", open(dest, "rb").read() == BODY)

# ---------- cancellation: the bytes are KEPT --------------------------------
os.remove(dest)
job = dl._Job("cancel", URL, dest, False)
job.cancel.set()                            # cancelled before the 1st chunk
events.clear()
mgr._download(job)
ck("cancellation reported", any(p.get("code") == "cancelled" for _e, p in events), events)
ck("the .part is kept for the resume", os.path.exists(part), os.listdir(tmp))
ck("no incomplete model left under the final name", not os.path.exists(dest))

# the resume after a cancellation really picks up where it stopped
before = os.path.getsize(part)
Handler.served_ranges.clear()
run("after-cancel")
ck("resume after cancellation", Handler.served_ranges == [f"bytes={before}-"] if before else True,
   Handler.served_ranges)
ck("final file correct after cancel then resume", open(dest, "rb").read() == BODY)

# ---------- destination already present -------------------------------------
evs = run("exists")
ck("file already there -> no re-download",
   any(p.get("note") == "already_exists" for _e, p in evs), evs)

# ---------- redirect on the same host: followed, and the resume survives it --
os.remove(dest)
REDIR = f"http://127.0.0.1:{PORT}/redir"
rpart = dl._part_path(dest, REDIR)
open(rpart, "wb").write(BODY[:HALF])
Handler.served_ranges.clear(); events.clear()
mgr._download(dl._Job("redir", REDIR, dest, False))
ck("redirect followed to completion", os.path.exists(dest) and open(dest, "rb").read() == BODY,
   events)
ck("the Range header reached the final hop", Handler.served_ranges == [f"bytes={HALF}-"],
   Handler.served_ranges)
ck("no .part left after a redirected download", not os.path.exists(rpart))

# ---------- redirect off the allow-list: refused, one request, nothing written
def count_requests(fn):
    calls = []
    orig = dl.urlpolicy.requests.request
    def counting(method, url, **kw):
        calls.append(url)
        return orig(method, url, **kw)
    dl.urlpolicy.requests.request = counting
    try:
        fn()
    finally:
        dl.urlpolicy.requests.request = orig
    return calls

os.remove(dest)
EVIL = f"http://127.0.0.1:{PORT}/evil"
events.clear()
calls = count_requests(lambda: mgr._download(dl._Job("evil", EVIL, dest, False)))
ck("redirect off the allow-list -> host_not_allowed",
   any(p.get("code") == "host_not_allowed" for _e, p in events), events)
ck("only the first hop was ever requested", calls == [EVIL], calls)
ck("nothing written for a refused redirect",
   not os.path.exists(dest) and not os.path.exists(dl._part_path(dest, EVIL)), os.listdir(tmp))

# ---------- source URL off the allow-list: refused before any request --------
events.clear()
calls = count_requests(lambda: mgr._download(
    dl._Job("blocked", "http://blocked.invalid/model.safetensors", dest, False)))
ck("blocked host -> host_not_allowed", any(p.get("code") == "host_not_allowed" for _e, p in events),
   events)
ck("blocked host -> no request at all", calls == [], calls)

# ---------- Authorization survives a same-origin hop, never an origin change --
srv2 = socketserver.TCPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=srv2.serve_forever, daemon=True).start()
PORT2 = srv2.server_address[1]
AUTH = {"Authorization": "Bearer not-a-real-token"}
r = dl.urlpolicy.open_url("GET", f"http://127.0.0.1:{PORT}/redir-to-{PORT}", headers=AUTH, timeout=5)
ck("same origin redirect: Authorization kept", r.headers.get("X-Got-Auth") == "yes", r.headers)
r = dl.urlpolicy.open_url("GET", f"http://127.0.0.1:{PORT}/redir-to-{PORT2}", headers=AUTH, timeout=5)
ck("same host, other port: Authorization dropped (requests' own rule)",
   r.headers.get("X-Got-Auth") == "no", r.headers)
srv2.shutdown()

# ---------- destination outside the model folders: refused before any I/O ---
outside = os.path.join(tempfile.mkdtemp(), "model.safetensors")
events.clear(); Handler.served_ranges.clear()
calls = count_requests(lambda: mgr._download(dl._Job("outside", URL, outside, False)))
ck("destination outside the model folders -> dest_refused",
   any(p.get("code") == "dest_refused" for _e, p in events), events)
ck("nothing written outside", not os.path.exists(outside)
   and not os.path.exists(dl._part_path(outside, URL)))
ck("no request for a refused destination", calls == [], calls)

srv.shutdown()
print(f"\n{'FAILURES: ' + ', '.join(fails) if fails else 'resume OK'}")
sys.exit(1 if fails else 0)
