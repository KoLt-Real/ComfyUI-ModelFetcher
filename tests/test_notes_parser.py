"""Note parsing: uniqueness of the model identifiers, HuggingFace page links, and the category
read from the URL when the note gives none."""
import os, sys, types
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
fp = types.ModuleType("folder_paths")
fp.models_dir="/m"
fp.folder_names_and_paths={}
fp.get_user_directory=lambda:"/u"
fp.map_legacy=lambda n:n
sys.modules["folder_paths"] = fp

fails=[]
def ck(n,c,e=""):
    print(("OK   " if c else "FAIL ")+n+("" if c else "  -> "+str(e)))
    if not c: fails.append(n)

from fetcher.notes_parser import parse_notes
# ---------- C5: two URLs, same name + category -> distinct ids ---------------
note = {"node_id": 1, "title": "t", "text": """
**checkpoints**
- [model.safetensors](https://huggingface.co/repoA/resolve/main/model.safetensors)
- [model.safetensors](https://huggingface.co/repoB/resolve/main/model.safetensors)
- [autre.safetensors](https://huggingface.co/repoC/resolve/main/autre.safetensors)
"""}
refs = parse_notes([note])
ids = [r.id for r in refs]
ck("3 models parsed", len(refs) == 3, ids)
ck("all ids unique", len(set(ids)) == 3, ids)
ck("the unambiguous model keeps a readable id",
   "checkpoints/autre.safetensors" in ids, ids)
ck("same-named models get a suffix", all("#" in i for i in ids if "model.safetensors" in i), ids)

# stability: re-analysing the same notes gives the same ids back (prefs are keyed on them)
ck("ids stable across analyses", [r.id for r in parse_notes([note])] == ids)
# reversed order in the note -> same ids (the suffix derives from the URL, not the rank)
note2 = dict(note, text=note["text"].replace("repoA", "TMP").replace("repoB", "repoA").replace("TMP", "repoB"))
ck("ids independent of the order", set(r.id for r in parse_notes([note2])) == set(ids))

# ---------- HuggingFace /blob/ = the page that displays the file ------------
# Real templates link it (flux_dev: comfyanonymous/flux_text_encoders/blob/main/clip_l...);
# downloaded as-is, the HTML page lands on disk under the model's name.
HF = "https://huggingface.co/org/repo"
blob = parse_notes([{"node_id": 1, "title": "t", "text": f"""
**text_encoders**
- [clip_l.safetensors]({HF}/blob/main/clip_l.safetensors)
- [README]({HF}/blob/main/README.md)
- [ds.safetensors](https://huggingface.co/datasets/org/ds/blob/v1/a/ds.safetensors)
- [gh.pth](https://github.com/org/repo/blob/main/gh.pth)
"""}])
urls = {r.filename: r.url for r in blob}
ck("blob link rewritten to resolve", urls.get("clip_l.safetensors") == f"{HF}/resolve/main/clip_l.safetensors", urls)
ck("dataset blob link rewritten too",
   urls.get("ds.safetensors") == "https://huggingface.co/datasets/org/ds/resolve/v1/a/ds.safetensors", urls)
ck("a blob link to a README stays a page link (not listed)", "README.md" not in urls, urls)
ck("only HuggingFace hosts are rewritten", urls.get("gh.pth") == "https://github.com/org/repo/blob/main/gh.pth", urls)
both = parse_notes([{"node_id": 1, "title": "t", "text": f"""
**vae**
- [ae.safetensors]({HF}/blob/main/ae.safetensors)
- [ae.safetensors]({HF}/resolve/main/ae.safetensors)
"""}])
ck("blob and resolve links to one file make one row", len(both) == 1 and not both[0].disambiguator,
   [r.id for r in both])

# ---------- category read from the URL, as a last resort --------------------
REG = {"diffusion_models", "loras", "vae", "text_encoders"}
is_cat = lambda n: {"unet": "diffusion_models"}.get(n, n) in REG
url_note = {"node_id": 1, "title": "t", "text": f"""
> tip: [big.safetensors]({HF}/resolve/main/diffusion_models/big.safetensors)

- [lora.safetensors]({HF}/blob/main/loras/lora.safetensors)
- [split.safetensors]({HF}/resolve/main/split_files/vae/split.safetensors)
- [deep.safetensors]({HF}/resolve/main/loras/flux/deep.safetensors)
- [old.safetensors]({HF}/resolve/main/unet/old.safetensors)
- [root.safetensors]({HF}/resolve/main/root.safetensors)
- [odd.safetensors]({HF}/resolve/main/transformer/odd.safetensors)
- [repo.safetensors](https://huggingface.co/org/loras/resolve/main/repo.safetensors)

**text_encoders**
- [te.safetensors]({HF}/resolve/main/vae/te.safetensors)
"""}
cats = {r.filename: r.category for r in parse_notes([url_note], is_cat)}
ck("no bold heading: the URL folder is the category", cats.get("big.safetensors") == "diffusion_models", cats)
ck("works on a rewritten blob link", cats.get("lora.safetensors") == "loras", cats)
ck("Comfy-Org split_files layout", cats.get("split.safetensors") == "vae", cats)
ck("nearest REGISTERED folder wins", cats.get("deep.safetensors") == "loras", cats)
ck("legacy folder names are accepted as-is (resolve_category maps them)", cats.get("old.safetensors") == "unet", cats)
ck("a file at the repository root has no category", cats.get("root.safetensors") is None, cats)
ck("an unregistered folder is never a category", cats.get("odd.safetensors") is None, cats)
ck("the repository name is never read as a folder", cats.get("repo.safetensors") is None, cats)
ck("a bold heading beats the URL", cats.get("te.safetensors") == "text_encoders", cats)
tree_note = {"node_id": 1, "title": "t", "text": f"""
- [big.safetensors]({HF}/resolve/main/diffusion_models/big.safetensors)

```
📂 ComfyUI/
├── 📂 models/
│   └── 📂 checkpoints/
│       └── big.safetensors
```
"""}
ck("the storage tree beats the URL",
   parse_notes([tree_note], is_cat)[0].category == "checkpoints", [r.category for r in parse_notes([tree_note], is_cat)])
# ---------- a Markdown heading ends the bold list ---------------------------
# image_qwen_image_layered: a "## FP8" tip after the **vae** list links a diffusion model.
sect = {r.filename: r.category for r in parse_notes([{"node_id": 1, "title": "t", "text": f"""
**vae**
- [v.safetensors]({HF}/resolve/main/vae/v.safetensors)

## FP8
For fp8, use [fp8.safetensors]({HF}/blob/main/split_files/diffusion_models/fp8.safetensors)

### Key Features
- [any.safetensors]({HF}/resolve/main/any.safetensors)

**loras**
- [l.safetensors]({HF}/resolve/main/l.safetensors)
"""}], is_cat)}
ck("the bold list still applies under its heading", sect.get("v.safetensors") == "vae", sect)
ck("after a new section, the old bold heading no longer applies",
   sect.get("fp8.safetensors") == "diffusion_models", sect)
ck("a later section with nothing to go on stays unknown", sect.get("any.safetensors") is None, sect)
ck("a new bold heading after a section applies again", sect.get("l.safetensors") == "loras", sect)

ck("without a registry, the URL is never read",
   all(r.category is None for r in parse_notes([url_note]) if r.filename != "te.safetensors"),
   {r.filename: r.category for r in parse_notes([url_note])})


print(f"\n{'FAILURES: '+', '.join(fails) if fails else 'parser OK'}")
sys.exit(1 if fails else 0)
