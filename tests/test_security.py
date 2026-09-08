import os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# folder_paths stub for hf_token / scanner
fp = types.ModuleType("folder_paths")
fp.models_dir = "/comfy/models"
fp.folder_names_and_paths = {}
fp.get_user_directory = lambda: "/comfy/user"
fp.get_output_directory = lambda: "/comfy/output"
fp.map_legacy = lambda n: n
sys.modules["folder_paths"] = fp

from fetcher import hf_token, scanner, urlpolicy

fails = []
def ck(name, cond, extra=""):
    print(("OK   " if cond else "FAIL ") + name + ("" if cond else "  -> " + str(extra)))
    if not cond: fails.append(name)

# --- S2: the token only ever goes to HuggingFace -----------------------------
os.environ["HF_TOKEN"] = "not-a-real-token"
ck("token -> huggingface.co resolve", "Authorization" in hf_token.auth_headers(
    "https://huggingface.co/org/repo/resolve/main/m.safetensors"))
ck("token -> cdn-lfs.huggingface.co", "Authorization" in hf_token.auth_headers(
    "https://cdn-lfs.huggingface.co/repo/abc"))
ck("token -> hf.co", "Authorization" in hf_token.auth_headers("https://hf.co/x/y"))
ck("NO token -> civitai", hf_token.auth_headers(
    "https://civitai.com/api/download/models/123") == {})
ck("NO token -> github", hf_token.auth_headers(
    "https://github.com/o/r/releases/download/v1/m.bin") == {})
ck("NO token -> attacker host", hf_token.auth_headers("http://attacker.example/x") == {})
ck("NO token -> lookalike huggingface.co.evil.com",
   hf_token.auth_headers("http://huggingface.co.evil.com/x") == {})
ck("NO token -> misleading suffix evilhuggingface.co",
   hf_token.auth_headers("http://evilhuggingface.co/x") == {})
del os.environ["HF_TOKEN"]
ck("no token in the env -> no header even on HF",
   hf_token.auth_headers("https://huggingface.co/x") == {})

# --- S1: an unknown category stays confined under models/ --------------------
def target(cat): return scanner.resolve_category(cat).target_dir
mroot = os.path.realpath("/comfy/models")
def under(cat):
    t = os.path.realpath(target(cat))
    return t == mroot or t.startswith(mroot + os.sep)
ck("a normal category lands under models/", target("mycat") == os.path.normpath("/comfy/models/mycat"))
ck("a legitimate subfolder is kept", target("cat/sub") == os.path.normpath("/comfy/models/cat/sub"))
ck("../../custom_nodes traversal blocked", under("../../custom_nodes/evil"), target("../../custom_nodes/evil"))
ck("absolute /etc traversal blocked", under("/etc/cron.d"), target("/etc/cron.d"))
ck("Windows ..\\.. traversal blocked", under("..\\..\\Windows"), target("..\\..\\Windows"))
ck("mixed cat/../.. blocked", under("checkpoints/../../../root"), target("checkpoints/../../../root"))

# --- S3: only allow-listed hosts are ever contacted ---------------------------
for url in ("https://huggingface.co/org/repo/resolve/main/m.safetensors",
            "https://us.aws.cdn.hf.co/xet-bridge-us/abc?Expires=1",
            "https://cdn-lfs-us-1.hf.co/repo/abc",
            "https://civitai.com/api/download/models/123",
            "https://x.r2.cloudflarestorage.com/53515/model/a.safetensors?X-Amz-Signature=1",
            "https://github.com/o/r/releases/download/v1/m.bin",
            "https://release-assets.githubusercontent.com/github-production-release-asset/1",
            "HTTPS://HUGGINGFACE.CO./x"):
    ck("allowed: " + url[:60], urlpolicy.check_url(url) is None, urlpolicy.check_url(url))
for url, reason in (("http://huggingface.co.evil.com/x", "host not allowed: huggingface.co.evil.com"),
                    ("http://evilhuggingface.co/x", "host not allowed: evilhuggingface.co"),
                    ("https://attacker.example/m.safetensors", "host not allowed: attacker.example"),
                    ("http://127.0.0.1:8188/system_stats", "host not allowed: 127.0.0.1"),
                    ("http://169.254.169.254/latest/meta-data/", "host not allowed: 169.254.169.254"),
                    ("http://[::1]/x", "host not allowed: ::1"),
                    ("http://user:pw@huggingface.co/x", "URL must not carry credentials"),
                    ("http://huggingface.co@evil.com/x", "URL must not carry credentials"),
                    ("ftp://huggingface.co/x", "URL is not http(s)"),
                    ("file:///etc/passwd", "URL is not http(s)"),
                    ("huggingface.co/x", "URL is not http(s)"),
                    ("http://huggingface.co:abc/x", "invalid URL"),
                    ("http:///x", "invalid URL"),
                    ("", "URL is not http(s)")):
    ck("refused: " + (url or "<empty>"), urlpolicy.check_url(url) == reason, urlpolicy.check_url(url))

os.environ["CF_MF_ALLOWED_HOSTS"] = " my-mirror.example , https://Other.Example:8443/path "
os.environ["HF_ENDPOINT"] = "https://hf.corp.local"
ck("CF_MF_ALLOWED_HOSTS entries are accepted (and their subdomains)",
   urlpolicy.check_url("https://my-mirror.example/x") is None
   and urlpolicy.check_url("https://cdn.my-mirror.example/x") is None)
ck("an entry given as a URL is reduced to its host",
   urlpolicy.check_url("https://other.example/x") is None)
ck("the HF_ENDPOINT host is accepted", urlpolicy.check_url("https://hf.corp.local/x") is None)
ck("the env never widens the token leash", hf_token.auth_headers("https://hf.corp.local/x") == {}
   and hf_token.auth_headers("https://my-mirror.example/x") == {})
del os.environ["CF_MF_ALLOWED_HOSTS"], os.environ["HF_ENDPOINT"]
ck("the env is read live: unset -> refused again",
   urlpolicy.check_url("https://my-mirror.example/x") == "host not allowed: my-mirror.example")
os.environ["CF_MF_ALLOWED_HOSTS"] = "127.0.0.1:8080, mirror.example:8443"
ck("a host:port entry means the host, whatever the port",
   urlpolicy.check_url("http://127.0.0.1:8080/x") is None
   and urlpolicy.check_url("http://127.0.0.1:9/x") is None
   and urlpolicy.check_url("https://mirror.example/x") is None, urlpolicy.allowed_hosts())
ck("an IPv6 literal entry is kept whole", urlpolicy._clean_host("[::1]:8080") == "::1")
del os.environ["CF_MF_ALLOWED_HOSTS"]
ck("only the host reason maps to host_not_allowed",
   urlpolicy.refusal_code("host not allowed: x") == "host_not_allowed"
   and urlpolicy.refusal_code("URL is not http(s)") == "url_refused"
   and urlpolicy.refusal_code("URL must not carry credentials") == "url_refused")
from fetcher import remote
ck("remote_size: off-list host -> host_not_allowed, no probe",
   remote.remote_size("http://attacker.example/m.safetensors") == (None, "host_not_allowed"))
ck("remote_size: credentials in the URL -> url_refused, not the allow-list's fault",
   remote.remote_size("https://huggingface.co@evil.example/m") == (None, "url_refused"))

# --- S4: the downloader only writes under the registered model folders -------
fp.folder_names_and_paths = {
    "checkpoints": (["/comfy/models/checkpoints", "/extra/models/checkpoints",
                     "/comfy/output/checkpoints"], set()),
}
for path in ("/comfy/models/checkpoints/a.safetensors",
             "/comfy/models/checkpoints/Flux/a.safetensors",
             "/comfy/models/newcategory/a.safetensors",
             "/extra/models/checkpoints/a.safetensors",
             "/comfy/models/checkpoints/../loras/a.safetensors"):
    ck("dest allowed: " + path, scanner.is_allowed_dest(path))
for path in ("/comfy/output/checkpoints/a.safetensors",
             "/comfy/models/../custom_nodes/evil.py",
             "/comfy/models2/a.safetensors",
             "/extra/models/a.safetensors",
             "/etc/cron.d/x",
             "/comfy/user/cf_mf_hf_token.txt"):
    ck("dest refused: " + path, not scanner.is_allowed_dest(path))
# A category whose only folder sits under output/ keeps it (dest_dirs' own fallback): what
# the menu offers and the route accepts, the worker must not refuse.
fp.folder_names_and_paths = {"onlyout": (["/comfy/output/onlyout"], set())}
ck("output-only category: the worker accepts what dest_dirs offers",
   scanner.dest_dirs(scanner.resolve_category("onlyout")) == ["/comfy/output/onlyout"]
   and scanner.is_allowed_dest("/comfy/output/onlyout/a.safetensors"))
ck("…without opening the rest of output/", not scanner.is_allowed_dest("/comfy/output/other/a"))
fp.folder_names_and_paths = {}
ck("nothing registered: models/ itself still allowed",
   scanner.is_allowed_dest("/comfy/models/x/a.safetensors"))
del sys.modules["folder_paths"]
ck("no folder_paths at all -> nothing is allowed",
   not scanner.is_allowed_dest("/comfy/models/x/a.safetensors"))
sys.modules["folder_paths"] = fp

# --- S5: the token file only ever holds one printable line -------------------
ck("a plausible token passes", hf_token.looks_like_token("hf_abcDEF0123456789"))
for label, tok in (("newline", "hf_abc\nrm -rf /"), ("space", "hf_abc def"), ("tab", "hf\tabc"),
                   ("control char", "hf_abc\x00"), ("empty", ""), ("over-long", "x" * 513)):
    ck(f"refused token: {label}", not hf_token.looks_like_token(tok))
try:
    hf_token.save_token("hf_abc\nrm -rf /")
    ck("save_token refuses a malformed token", False)
except ValueError:
    ck("save_token refuses a malformed token", True)
ck("…and did not touch the environment", os.environ.get("HF_TOKEN") is None)

print(f"\n{'FAILURES: ' + ', '.join(fails) if fails else 'all security cases OK'}")
sys.exit(1 if fails else 0)
