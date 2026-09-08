import { api } from "../../scripts/api.js";
import { app } from "../../scripts/app.js";
import { linkState, linkedAmong, relinkModel } from "./relink.js";

// ---------------------------------------------------------------------------
// Module state
// ---------------------------------------------------------------------------
let overlayEl = null;
let state = {
  models: [],       // enriched with _subfolder, _dlCategory
  categories: {},
  knownCategories: [],
  hfToken: false,
  workflowKey: "",  // the workflow the displayed analysis belongs to
};
const rowById = new Map(); // id -> {model, el}
let lastNotes = [];        // last analysed notes (for the re-analysis after saving a token)

// ---------------------------------------------------------------------------
// Per-workflow preferences
//
// The choices made in the popup (location, subfolder, ticked boxes) survive closing it, but
// stay attached TO the workflow they were made in: with several workflows open, the tabs
// never step on each other.
// Relink, on the other hand, is NOT remembered here: its state is read back from the graph on
// every render (the source of truth, including after a Ctrl+Z or a workflow reload).
// ---------------------------------------------------------------------------
const MAX_TRACKED_WORKFLOWS = 20;
const prefsByWorkflow = new Map(); // workflow key -> { models: {id: {...}}, sawRelink }
let graphEpoch = 0;
let analyzeSeq = 0;  // seq of the latest analysis fired (late answers are dropped)

// Called by main.js on every (re)configuration of the graph (load, tab switch).
export function onGraphConfigured() { graphEpoch++; }

function currentWorkflowKey() {
  const wf = app.extensionManager?.workflow?.activeWorkflow || app.workflowManager?.activeWorkflow;
  const key = wf?.key || wf?.path || wf?.filename;
  // When the frontend exposes no workflow identity, fall back on a counter of graph
  // (re)configurations: preferences survive the popup but not a workflow switch — a safe
  // degradation, never a state shared between tabs.
  return key ? "wf:" + key : "epoch:" + graphEpoch;
}

function prefsFor(key) {
  let prefs = prefsByWorkflow.get(key);
  if (!prefs) {
    prefs = { models: {}, sawRelink: false };
    prefsByWorkflow.set(key, prefs);
    // Bound the memory: only the most recently visited workflows are kept.
    while (prefsByWorkflow.size > MAX_TRACKED_WORKFLOWS) {
      prefsByWorkflow.delete(prefsByWorkflow.keys().next().value);
    }
  }
  return prefs;
}

function persistRow(m, patch) {
  const prefs = prefsFor(state.workflowKey);
  prefs.models[m.id] = { ...(prefs.models[m.id] || {}), ...patch };
}

// A remembered copy is only reused while it is still one of the copies on offer. Without this
// the pick was the one row choice NOT surviving a re-analysis — a refresh, a tab switch or a
// token save — and the menu snapped back to the automatic copy with no signal, while the tick,
// the location and the subfolder beside it all held.
function validPick(m, value) {
  if (!value) return null;
  return relinkCandidates(m).find((c) => c.value === value) || null;
}

// A remembered location is only reused while it still belongs to the category's locations
// (extra_model_paths may have changed meanwhile) — otherwise the server would refuse it.
function validBaseDir(m, dir) {
  if (!dir) return "";
  const locations = state.categories[m.category]?.locations || [];
  return locations.some((l) => l.dir === dir && !l.is_default) ? dir : "";
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function fmtSize(bytes) {
  if (bytes == null || bytes < 0) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0, n = bytes;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(n < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
}
function fmtSpeed(bps) {
  if (!bps || bps < 0) return "";
  return fmtSize(bps) + "/s";
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
// HF page URL of a model, derived from its resolve/ download URL.
function hfPageUrl(url) {
  try {
    const u = new URL(url);
    if (u.hostname !== "huggingface.co") return null;
    const parts = u.pathname.split("/").filter(Boolean);
    const ri = parts.indexOf("resolve");
    const repo = ri >= 0 ? parts.slice(0, ri) : parts.slice(0, 2);
    if (repo.length < 2) return null;
    return "https://huggingface.co/" + repo.join("/");
  } catch (e) { return null; }
}

const STATUS_META = {
  installed:            { cls: "ok",   icon: "✓", label: "Installed" },
  duplicate_same_size:  { cls: "warn", icon: "≈", label: "Likely duplicate" },
  duplicate_diff_size:  { cls: "diff", icon: "⚠", label: "Same name, different size" },
  missing:              { cls: "miss", icon: "⬇", label: "Missing" },
  unknown_dest:         { cls: "unk",  icon: "?", label: "Unknown destination" },
};

// ---------------------------------------------------------------------------
// Open / analyze
// ---------------------------------------------------------------------------
export async function openPopup(notes) {
  ensureOverlay();
  if (notes) lastNotes = notes;
  // An analysis is slow (network HEADs): several can overlap (click, ↻, tab switch). Only
  // the most recently fired one is allowed to paint — otherwise a late answer would put the
  // previous workflow back on screen.
  const seq = ++analyzeSeq;
  // The workflow key AT REQUEST TIME: the results belong to that workflow, not to whichever
  // one is active when the answer comes back.
  const key = currentWorkflowKey();
  showLoading();
  try {
    const resp = await api.fetchApi("/cf_mf/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ notes: lastNotes }),
    });
    const data = await resp.json();
    if (seq !== analyzeSeq) return;
    // A 403 from the local-only gate carries a message saying how to opt in: show that.
    if (!data.ok) throw new Error(data.message || data.error || "analysis failed");
    state.categories = data.categories || {};
    state.knownCategories = data.known_categories || [];
    state.workflowKey = key;
    const saved = prefsFor(state.workflowKey).models;
    state.models = (data.models || []).map((m) => {
      const pref = saved[m.id] || {};
      return {
        ...m,
        category_known: m.category_known || !!pref.dlCategory,
        _subfolder: pref.subfolder ?? "",
        _baseDir: validBaseDir(m, pref.baseDir),  // "" = the default location
        // Remembered by value, not by object: every analysis rebuilds these, and a copy gone
        // from disk must not stay selected.
        _relinkPick: validPick(m, pref.relinkValue),
        // Same persistence as the pick: a re-analysis rebuilds these objects, and a job posted
        // before it must still be judged on ITS subfolder when its done event lands after.
        _queuedSubfolder: pref.queuedSubfolder,
        _dlCategory: pref.dlCategory ?? (m.category || null),
        _checked: pref.checked,                   // undefined = the default tick
      };
    });
    state.hfToken = !!data.hf_token;
    render();
    resyncStatus(seq);
  } catch (e) {
    if (seq !== analyzeSeq) return;
    showError(e.message || String(e));
  }
}

async function resyncStatus(seq) {
  try {
    const r = await api.fetchApi("/cf_mf/status");
    const s = await r.json();
    if (seq !== analyzeSeq) return; // the rows no longer belong to this analysis
    const running = new Set([...(s.pending || []), s.active].filter(Boolean));
    for (const id of running) {
      const row = rowById.get(id);
      // Snapshot taken before a "done"/"error" that arrived meanwhile: replaying it would
      // put the row back into downloading, forever (no further event would ever come).
      if (row && row.state !== "done" && row.state !== "error") {
        setRowDownloading(row, { downloaded: 0, total: 0 });
      }
    }
  } catch (e) { /* noop */ }
}

// ---------------------------------------------------------------------------
// Rendering
// ---------------------------------------------------------------------------
function ensureOverlay() {
  if (overlayEl) { overlayEl.style.display = "flex"; return; }
  overlayEl = document.createElement("div");
  overlayEl.className = "cf-mf-overlay";
  overlayEl.innerHTML = `
    <div class="cf-mf-card" role="dialog" aria-label="Model Fetcher">
      <div class="cf-mf-head">
        <div class="cf-mf-title">
          <span class="cf-mf-title-icon">⬇</span>
          <span>Model Fetcher</span>
        </div>
        <div class="cf-mf-summary"></div>
        <div class="cf-mf-head-actions">
          <button class="cf-mf-key" title="HuggingFace access token">🔑</button>
          <button class="cf-mf-refresh" title="Refresh">↻</button>
          <button class="cf-mf-close" title="Close">✕</button>
        </div>
      </div>
      <div class="cf-mf-body"></div>
      <div class="cf-mf-foot">
        <div class="cf-mf-foot-info"></div>
        <div class="cf-mf-save-hint" hidden>🔗 Loader paths updated — save the workflow to keep them.</div>
        <button class="cf-mf-dl-all" disabled>Download all</button>
      </div>
    </div>`;
  document.body.appendChild(overlayEl);

  overlayEl.addEventListener("mousedown", (e) => {
    if (e.target === overlayEl) closePopup();
  });
  overlayEl.querySelector(".cf-mf-close").addEventListener("click", closePopup);
  overlayEl.querySelector(".cf-mf-key").addEventListener("click", toggleTokenPanel);
  overlayEl.querySelector(".cf-mf-refresh").addEventListener("click", async () => {
    const mod = await import("./main.js");
    openPopup(mod.gatherWorkflowNotes());
  });
  overlayEl.querySelector(".cf-mf-dl-all").addEventListener("click", downloadSelected);
  document.addEventListener("keydown", onKeydown);
}

function onKeydown(e) {
  if (e.key === "Escape" && overlayEl && overlayEl.style.display !== "none") closePopup();
}

export function closePopup() {
  if (overlayEl) overlayEl.style.display = "none";
}

export function isPopupOpen() {
  return !!overlayEl && overlayEl.style.display !== "none";
}

function bodyEl() { return overlayEl.querySelector(".cf-mf-body"); }

function showLoading() {
  bodyEl().innerHTML = `
    <div class="cf-mf-state">
      <div class="cf-mf-spinner"></div>
      <p>Reading notes and scanning your local models…</p>
    </div>`;
  overlayEl.querySelector(".cf-mf-summary").textContent = "";
  overlayEl.querySelector(".cf-mf-dl-all").disabled = true;
  overlayEl.querySelector(".cf-mf-foot-info").textContent = "";
  overlayEl.querySelector(".cf-mf-save-hint").hidden = true;
}

function showError(msg) {
  bodyEl().innerHTML = `<div class="cf-mf-state cf-mf-state-err">
    <div class="cf-mf-state-icon">⚠</div>
    <p>${esc(msg)}</p></div>`;
}

function render() {
  rowById.clear();
  const models = state.models;
  const body = bodyEl();
  body.innerHTML = "";

  if (!models.length) {
    body.innerHTML = `<div class="cf-mf-state">
      <div class="cf-mf-state-icon">📄</div>
      <p><strong>No models to download.</strong></p>
      <p class="cf-mf-muted">No "Model Link" note (MarkdownNote) with recognized download links
      was found in this workflow.</p></div>`;
    updateSummary();
    return;
  }

  // Token panel (beginner guidance) when gated models are seen without a valid token.
  const gated = models.filter((m) => m.remote_size_error === "401_gated");
  if (!state.hfToken && gated.length) {
    body.appendChild(buildTokenPanel(gated));
  }

  // Grouping by display category.
  const groups = new Map();
  for (const m of models) {
    const key = m.category || "Unknown destination";
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(m);
  }

  for (const [cat, list] of groups) {
    const section = document.createElement("div");
    section.className = "cf-mf-group";
    const catMeta = state.categories[cat];
    const target = catMeta?.target_dir ? ` · ${esc(shortPath(catMeta.target_dir))}` : "";
    section.innerHTML = `<div class="cf-mf-group-head">
      <span class="cf-mf-group-name">${esc(cat)}</span>
      <span class="cf-mf-group-meta">${list.length} model(s)${target}</span></div>`;
    const rowsWrap = document.createElement("div");
    rowsWrap.className = "cf-mf-rows";
    for (const m of list) rowsWrap.appendChild(buildRow(m));
    section.appendChild(rowsWrap);
    body.appendChild(section);
  }

  // The save reminder belongs to the displayed workflow, not to the popup.
  overlayEl.querySelector(".cf-mf-save-hint").hidden = !prefsFor(state.workflowKey).sawRelink;
  updateSummary();
}

function shortPath(p) {
  const parts = p.split(/[/\\]/);
  return parts.slice(-3).join("/");
}

// One label per location, short but UNAMBIGUOUS.
//
// Two roots declared in extra_model_paths.yaml very often end the same way ("models/clip" in
// two distinct installs): showing only the tail of the path produced two identical entries in
// the menu, impossible to tell apart without hovering each one. So we keep the smallest number
// of trailing segments that makes every entry unique, and not one more — a full path would
// overflow the select.
// Beyond that, the MIDDLE is elided: both ends are what matters (which install, which folder),
// whereas a CSS truncation would eat the end and leave only "models/text…".
const LABEL_MAX_PARTS = 3;
const LABEL_MAX_CHARS = 28;

export function locationLabels(dirs) {
  const parts = dirs.map((d) => String(d).split(/[/\\]/).filter(Boolean));
  const unique = (list) => new Set(list).size === list.length;
  const deepest = Math.max(1, ...parts.map((p) => p.length));

  for (let depth = 2; depth <= deepest; depth++) {
    const sliced = parts.map((p) => p.slice(-depth));
    const full = sliced.map((p) => p.join("/"));
    if (!unique(full)) continue;

    // At the minimal depth, the LEADING segment is the one carrying the difference, so the
    // middle can be elided without losing information — checked all the same.
    const elided = sliced.map((p) => {
      const plain = p.join("/");
      const tooLong = p.length > LABEL_MAX_PARTS || plain.length > LABEL_MAX_CHARS;
      return (tooLong && p.length >= 3)
        ? [p[0], "…", p[p.length - 1]].join("/")
        : plain;
    });
    return unique(elided) ? elided : full;
  }
  // Identical paths: the server deduplicates by realpath, so we should never get here. A
  // safety net so that two entries stay distinct regardless.
  return parts.map((p, i) => `${p.join("/")} (${i + 1})`);
}

function buildRow(m) {
  const meta = STATUS_META[m.status] || STATUS_META.missing;
  const row = document.createElement("div");
  row.className = "cf-mf-row";
  row.dataset.id = m.id;

  const preChecked = m.status === "missing" && m.category_known && !hostBlocked(m);
  const catMeta = state.categories[m.category];
  const locations = catMeta?.locations || [];
  const defaultLoc = locations.find((l) => l.is_default) || locations[0] || null;
  // Location restored from a previous popup session, otherwise the default.
  const savedLoc = m._baseDir ? locations.find((l) => l.dir === m._baseDir) : null;
  const initialSubfolders = (savedLoc || defaultLoc)?.subfolders || catMeta?.subfolders || [""];
  const needDest = m.status === "unknown_dest" || !m.category;
  // Link state read back from the graph (not remembered) — see "Per-workflow preferences".
  const pick = relinkPick(m);
  const link = pick ? linkState(m.filename, pick.value) : null;

  // column 1: checkbox
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.className = "cf-mf-cb";
  cb.checked = m._checked ?? preChecked;
  cb.addEventListener("change", () => {
    persistRow(m, { checked: cb.checked });
    updateSummary();
  });

  // column 2: information
  const info = document.createElement("div");
  info.className = "cf-mf-info";
  info.innerHTML = `
    <div class="cf-mf-fname" title="${esc(m.url)}">${esc(m.filename)}</div>
    <div class="cf-mf-sub">
      <span class="cf-mf-size">${fmtSize(m.remote_size)}</span>
      <span class="cf-mf-badgewrap">${buildBadge(m, meta, isLinked(link), pick)}</span>
    </div>`;

  // column 3: one line per action, each ending in ITS OWN button — "link to that copy" and
  // "save to that folder" are two independent decisions, and a button sitting far from the
  // field that drives it reads as if it obeyed the other one.
  const ops = document.createElement("div");
  ops.className = "cf-mf-ops";

  // line 1 — relink: which copy already on disk the loader nodes should point at.
  const linkLine = opLine("Link to");
  const actionLink = document.createElement("div");
  actionLink.className = "cf-mf-action-link";
  if (pick) {
    linkLine.ctl.appendChild(buildCopyControl(m, pick));
    actionLink.appendChild(buildLinkAction(m, link, pick));
  } else {
    linkLine.el.hidden = true;
  }
  linkLine.el.appendChild(actionLink);
  // Hairline closing the relink line, reading "OR": the two lines are alternatives — point the
  // nodes at a copy you already have, or fetch another one. Carried BY the relink line so it
  // disappears with it: a row with nothing to relink must not show a rule hanging over its
  // download line, still less offer a choice between one option and nothing.
  const sep = document.createElement("div");
  sep.className = "cf-mf-opsep";
  sep.textContent = "OR";
  linkLine.el.appendChild(sep);

  // line 2 — download: where a fetched file is written.
  const dlLine = opLine("Save to");
  const dest = dlLine.ctl;
  if (m.status !== "installed") {
    if (needDest) {
      const catSel = document.createElement("select");
      catSel.className = "cf-mf-catsel";
      catSel.innerHTML = `<option value="">— choose a folder —</option>` +
        state.knownCategories.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join("");
      catSel.value = m._dlCategory || "";
      catSel.addEventListener("change", () => {
        m._dlCategory = catSel.value || null;
        m.category_known = !!catSel.value;
        persistRow(m, { dlCategory: m._dlCategory });
        updateSummary();
      });
      dest.appendChild(catSel);
    }
    const subWrap = buildSubfolderInput(m, initialSubfolders);
    const destCol = document.createElement("div");
    destCol.className = "cf-mf-destcol";
    if (locations.length > 1) {
      const locSel = document.createElement("select");
      locSel.className = "cf-mf-locsel";
      locSel.title = "Where to save this model. \"main\" is the ComfyUI models folder; " +
        "other entries come from extra_model_paths.yaml.";
      const labels = locationLabels(locations.map((l) => l.dir));
      locations.forEach((loc, i) => {
        const label = labels[i]
          + (loc.is_default ? " (main)" : "")
          + (!loc.exists ? " (not found)" : "");
        const opt = new Option(label, loc.dir);
        opt.title = loc.dir;
        // A location that does not exist on the server's machine is not offered (except the
        // default, which is created when needed).
        if (!loc.exists && !loc.is_default) opt.disabled = true;
        if (savedLoc ? loc.dir === savedLoc.dir : loc.is_default) opt.selected = true;
        locSel.appendChild(opt);
      });
      locSel.addEventListener("change", () => {
        const loc = locations.find((l) => l.dir === locSel.value) || defaultLoc;
        m._baseDir = loc && !loc.is_default ? loc.dir : "";
        persistRow(m, { baseDir: m._baseDir });
        subWrap._setSuggestions(loc?.subfolders || [""]);
      });
      destCol.appendChild(locSel);
    }
    destCol.appendChild(subWrap);
    dest.appendChild(destCol);
  } else {
    dlLine.label.textContent = "";   // nothing to choose: only a disabled Download remains
  }

  // The download slot keeps its name and its sole ownership of the row state machine:
  // progress bar, cancel, error and "✓ Installed" all still land here and nowhere else.
  const action = document.createElement("div");
  action.className = "cf-mf-action";
  action.appendChild(buildIdleButton(m));
  dlLine.el.appendChild(action);

  ops.append(linkLine.el, dlLine.el);
  row.append(cb, info, ops);
  rowById.set(m.id, { model: m, el: row, cb, action, actionLink, linkLine: linkLine.el,
                      info, state: "idle" });
  return row;
}

// One line of the operations grid: label, controls, button. `display: contents` keeps the
// three cells in the PARENT grid, so both lines share their column widths and the buttons
// line up under one another.
function opLine(labelText) {
  const el = document.createElement("div");
  el.className = "cf-mf-opline";
  const label = document.createElement("span");
  label.className = "cf-mf-oplabel";
  label.textContent = labelText;
  const ctl = document.createElement("div");
  ctl.className = "cf-mf-opctl";
  el.append(label, ctl);
  return { el, label, ctl };
}

// Copies of the file a loader node may be pointed at.
//
// Two filters, both about not offering a choice that is not one:
// - a copy whose size contradicts the source is another version of the model, the very reason
//   a size mismatch never gets a Relink button either;
// - copies sharing a relative path under two roots collapse into one entry. ComfyUI resolves
//   a widget value against every registered root in order, so both would write the SAME string
//   and load the SAME file — `get_filename_list` deduplicates them for exactly that reason.
function relinkCandidates(m) {
  const all = (m.local_matches || []).filter((c) => c.value);
  const usable = m.remote_size == null ? all : all.filter((c) => c.same_size);
  const byValue = new Map();
  for (const c of usable) if (!byValue.has(c.value)) byValue.set(c.value, c);
  return [...byValue.values()];
}

// The copy Relink points at: the user's pick, otherwise the one the graph already uses,
// otherwise the server's automatic choice.
//
// Consulting the graph before falling back matters: reopening the popup on a workflow already
// relinked to the SECOND copy would otherwise show the automatic pick and claim the row is
// not linked. Same rule as the link state itself — the graph is the source of truth.
function relinkPick(m) {
  if (!m.relink) return null;
  if (m._relinkPick) return m._relinkPick;
  const cands = relinkCandidates(m);
  if (cands.length < 2) return m.relink;
  const linked = linkedAmong(m.filename, cands.map((c) => c.value));
  return cands.find((c) => c.value === linked) || m.relink;
}

// The relink line's button, or the pill acknowledging it is already done. Always rebuilt
// through these functions: restoring saved innerHTML would lose the listeners (a dead button).
function buildLinkAction(m, st, pick) {
  return isLinked(st) ? buildLinkedBadge(m, st.linked, false, pick)
                      : buildRelinkButton(m, st, pick);
}

// Linked = every node using this file points at the local copy.
function isLinked(st) {
  return !!st && st.found > 0 && st.linked === st.found;
}

function buildIdleButton(m) {
  const dlBtn = document.createElement("button");
  dlBtn.className = "cf-mf-dl-one";
  dlBtn.textContent = "Download";
  dlBtn.disabled = (m.status === "installed") || hostBlocked(m);
  if (hostBlocked(m)) dlBtn.title = HOST_BLOCKED_MSG;
  dlBtn.addEventListener("click", () => downloadOne(m));
  return dlBtn;
}

// "Relink" button: the file is already on disk, inside a subfolder of the category. The
// workflow's loader nodes get their value fixed instead of downloading a second copy.
function buildRelinkButton(m, st, pick) {
  const btn = document.createElement("button");
  btn.className = "cf-mf-relink";
  btn.textContent = "Relink";
  if (!st.found) {
    // No node of the open workflow uses this file: nothing to fix here.
    btn.disabled = true;
    btn.title = "No node of this workflow uses this file.";
    return btn;
  }
  btn.title = `Point the loader node(s) of this workflow to the copy you already have:\n`
    + `${pick.value}\n(${pick.path})`
    + (st.linked ? `\n${st.linked} of ${st.found} node(s) already point there.` : "");
  btn.addEventListener("click", () => relinkOne(m, btn));
  return btn;
}

// Acknowledgement: the button becomes this pill once the link is done. Rebuilt identically on
// later openings of the popup, since it is read back from the graph.
function buildLinkedBadge(m, count, unverified, pick) {
  const el = document.createElement("span");
  el.className = "cf-mf-relinked-badge";
  el.textContent = `✓ Linked${count > 1 ? " ×" + count : ""}`;
  el.title = `${count} node(s) point to ${pick.value}.`
    + `\nSave the workflow to keep the change.`
    + (unverified
      ? `\nThis path wasn't in the node's list yet — refresh node definitions (R) if ComfyUI complains.`
      : "");
  return el;
}

// One label per root, keyed by root — or null when there is only one root in sight (nothing
// to tell apart) or a copy carries no root (defensive: every entry must stay labellable).
//
// The naming set is the UNION of the category's locations and the copies' own roots, not
// just the latter: two subfolders of one install differ early ("models/unet" against
// "models/diffusion_models"), which used to stop locationLabels' depth escalation before
// the segment naming the install — on a machine with two installs, the menu could not say
// which one a copy lived in. Running over the category's full set drags that segment in
// exactly when "Save to" shows it. The union (rather than the locations alone) covers a
// copy under output/<category>: scanned, hence a valid root here, never a download location.
// Deduplicated, so two copies of one root cannot trip locationLabels' "(1)"/"(2)" net.
function rootLabels(locationDirs, roots) {
  if (roots.some((r) => !r)) return null;
  const naming = [...new Set([...(locationDirs || []), ...roots])];
  if (naming.length < 2) return null;
  const labels = locationLabels(naming);
  return new Map(naming.map((r, i) => [r, labels[i]]));
}

// Menu of the copies already on disk. Shown only when there is a real choice to make.
// Without it Relink silently took the first same-size copy, while the destination menu right
// next to it — which only ever drove downloads — read as though it were the one deciding.
// What to write on each entry of the menu: WHERE the copy lives, not its full path.
//
// Every entry repeats the same filename — it is the row's title, two centimetres to the left —
// so spelling it out again buries the only part that differs. Seen on a real install: two
// entries 60 characters long, identical but for the case of one folder ("Minimax" vs
// "MiniMax"), and no way to tell which disk each one sat on.
//
// The root is named as soon as the category registers more than one location — exactly when
// the "Save to" menu exists — even if every copy sits on the same root: the whole point is
// to say WHICH install a copy lives in, and that question exists as soon as there are two.
// Labelling runs over the same set as the "Save to" menu (see rootLabels), so the same
// folder reads the same in both menus.
export function copyLabels(cands, locationDirs = null) {
  const named = rootLabels(locationDirs, cands.map((c) => c.root));
  const distinct = (arr) => new Set(arr).size === arr.length;
  const compose = (folderOf) => cands.map((c) => {
    const cut = c.value.lastIndexOf("/");
    // A copy at the root of a folder would resolve by its bare name, so it never reaches this
    // menu; the guard is there so a malformed value degrades to something readable.
    const folder = folderOf(cut > 0 ? c.value.slice(0, cut) : c.value);
    return named ? `${named.get(c.root)} · ${folder}` : folder;
  });
  // Escalate until every RENDERED label is unique — a menu with two identical entries is the
  // regression this whole function exists to kill, so no step is allowed to reintroduce one:
  // 1. elided folders (the normal case);
  // 2. elision collided (two deep folders sharing both ends) → full folders, over budget but
  //    distinct — a truncated-but-tellable label beats a tidy ambiguous one;
  // 3. identical folders (filenames differing by case) → append the filename;
  // 4. absolute net, same shape as locationLabels' own.
  let labels = compose(elideFolder);
  if (!distinct(labels)) labels = compose((f) => f);
  if (!distinct(labels)) {
    labels = cands.map((c, i) => `${labels[i]} · ${c.value.slice(c.value.lastIndexOf("/") + 1)}`);
  }
  if (!distinct(labels)) labels = labels.map((l, i) => `${l} (${i + 1})`);
  return labels;
}

// Keep both ends, drop the middle. The select truncates with CSS, which eats the TAIL — the
// half that tells two sibling folders apart ("…/Portrait/v1" against "…/Portrait/v2"). Staying
// under the budget means the browser never has to truncate at all.
function elideFolder(folder) {
  if (folder.length <= LABEL_MAX_CHARS) return folder;
  const parts = folder.split("/");
  if (parts.length >= 3) {
    const short = [parts[0], "…", parts[parts.length - 1]].join("/");
    if (short.length <= LABEL_MAX_CHARS) return short;
  }
  const keep = Math.floor((LABEL_MAX_CHARS - 1) / 2);
  return folder.slice(0, keep) + "…" + folder.slice(-keep);
}

function buildCopyControl(m, pick) {
  const cands = relinkCandidates(m);
  const locationDirs = state.categories[m.category]?.locations?.map((l) => l.dir) || [];
  // A single copy is not a choice: showing the path plainly says more than a one-entry menu,
  // and it was until now only reachable through the button's tooltip. The root is still
  // named under the same rule as the menu's entries: one copy is no less ambiguous about
  // WHICH install it sits in than two.
  if (cands.length < 2) {
    const fixed = document.createElement("span");
    fixed.className = "cf-mf-copyfixed";
    const named = rootLabels(locationDirs, [pick.root]);
    fixed.textContent = named ? `${named.get(pick.root)} · ${pick.value}` : pick.value;
    fixed.title = pick.path;
    return fixed;
  }

  const sel = document.createElement("select");
  sel.className = "cf-mf-copysel";
  sel.title = "Which copy already on disk the loader node(s) should point at.";
  const labels = copyLabels(cands, locationDirs);
  // Keyed by index rather than by path: shorter, and nothing here depends on the value.
  cands.forEach((c, i) => {
    const opt = new Option(labels[i], String(i));
    opt.title = `${c.path}\n${fmtSize(c.size)}`;
    if (c.path === pick.path) opt.selected = true;
    sel.appendChild(opt);
  });
  sel.addEventListener("change", () => {
    m._relinkPick = cands[Number(sel.value)] || null;
    persistRow(m, { relinkValue: m._relinkPick?.value || "" });
    refreshLinkParts(m);
  });
  return sel;
}

// Re-render what depends on WHICH copy is selected: the action column, and the badge that
// names the copy. The picker itself is left in place — rebuilding it would close the menu the
// user has just used.
function refreshLinkParts(m) {
  const row = rowById.get(m.id);
  if (!row || row.linkLine.hidden) return;
  // Guarded on the LINE, not on row.state: that one describes the download slot, and a running
  // download says nothing about whether this row can still be relinked.
  //
  // The workflow check is the one relinkOne() makes: after a tab switch the popup keeps showing
  // the previous workflow's rows until the re-analysis lands (main.js re-opens on a timer), and
  // reading the visible graph meanwhile would report another workflow's links as this row's.
  if (state.workflowKey !== currentWorkflowKey()) return;
  const pick = relinkPick(m);
  const st = linkState(m.filename, pick.value);
  row.actionLink.innerHTML = "";
  row.actionLink.appendChild(buildLinkAction(m, st, pick));
  const wrap = row.info.querySelector(".cf-mf-badgewrap");
  if (wrap) {
    wrap.innerHTML = buildBadge(m, STATUS_META[m.status] || STATUS_META.missing,
                                isLinked(st), pick);
  }
}

function relinkOne(m, btn) {
  const row = rowById.get(m.id);
  if (!row) return;
  // The visible graph is no longer the analysed one (tab switched, not yet refreshed):
  // writing into it would fix the nodes of the wrong workflow.
  if (state.workflowKey !== currentWorkflowKey()) {
    flashRow(m.id, "Workflow changed — refresh first.", "link");
    return;
  }
  btn.disabled = true;
  const pick = relinkPick(m);
  let res;
  try {
    res = relinkModel(m.filename, pick.value);
  } catch (e) {
    flashRow(m.id, "Relink failed: " + (e.message || String(e)), "link");
    return;
  }
  if (!res.found) {
    // The workflow changed between the render and the click.
    flashRow(m.id, "No node uses this file.", "link");
    return;
  }

  // The button gives way to its acknowledgement, the row turns green.
  const badge = buildLinkedBadge(m, res.changed + res.already, res.unverified, pick);
  btn.replaceWith(badge);
  markRowLinked(row, m, pick);
  if (res.wrote) {
    flashLinked(row, badge);
    showSaveHint();
  }
}

// Row status: "≈ Likely duplicate" becomes "🔗 Linked".
function markRowLinked(row, m, pick) {
  const badge = row.info.querySelector(".cf-mf-badge");
  if (!badge) return;
  badge.className = "cf-mf-badge cf-mf-badge-ok";
  badge.textContent = "🔗 Linked";
  badge.title = `This workflow's loader node(s) point to ${pick.path}`;
}

// A brief spotlight at click time (the pill alone goes unnoticed).
function flashLinked(row, badge) {
  row.el.classList.add("cf-mf-row-flash");
  badge.classList.add("cf-mf-pop");
  setTimeout(() => {
    row.el.classList.remove("cf-mf-row-flash");
    badge.classList.remove("cf-mf-pop");
  }, 1000);
}

// Save reminder: relink changes the in-memory graph, not the workflow file.
function showSaveHint() {
  prefsFor(state.workflowKey).sawRelink = true;
  const hint = overlayEl?.querySelector(".cf-mf-save-hint");
  if (hint) hint.hidden = false;
}

// Editable subfolder field: suggestions = existing subfolders (of the selected location),
// free text = a new subfolder (created at download time). Empty = the category root (default).
// `wrap._setSuggestions(list)` reloads the suggestions when the user changes root location.
function buildSubfolderInput(m, subfolders) {
  const wrap = document.createElement("div");
  wrap.className = "cf-mf-subwrap";

  const input = document.createElement("input");
  input.type = "text";
  input.className = "cf-mf-subinput";
  input.value = m._subfolder || "";
  input.placeholder = "root — or type a new subfolder";
  input.title = "Leave empty to save in the category root, or type a name to create a new " +
    "subfolder before downloading. Existing subfolders are suggested as you type.";

  const dlId = "cf-mf-dl-" + (++datalistSeq); // unique id (deriving it from m.id can collide)
  const dl = document.createElement("datalist");
  dl.id = dlId;
  input.setAttribute("list", dlId);
  wrap.appendChild(dl);

  let existing = [];
  const markNew = () => {
    input.classList.toggle("cf-mf-subinput-new",
      !!m._subfolder && !existing.includes(m._subfolder));
  };
  wrap._setSuggestions = (list) => {
    existing = (list || []).filter((s) => s);
    dl.innerHTML = "";
    for (const s of existing) dl.appendChild(new Option(s, s));
    markNew();
  };
  wrap._setSuggestions(subfolders);

  input.addEventListener("input", () => {
    // Normalise: backslashes → slashes, no leading/trailing slash.
    m._subfolder = input.value.replace(/\\/g, "/").replace(/^\/+|\/+$/g, "").trim();
    persistRow(m, { subfolder: m._subfolder });
    markNew();
  });

  wrap.appendChild(input);
  return wrap;
}

let datalistSeq = 0;

function buildBadge(m, meta, linked, pick) {
  if (linked && pick) {
    return `<span class="cf-mf-badge cf-mf-badge-ok" title="${esc(
      "This workflow's loader node(s) point to " + pick.path)}">🔗 Linked</span>`;
  }
  let tip = "";
  if (m.status === "duplicate_same_size" || m.status === "duplicate_diff_size") {
    const paths = (m.local_matches || []).map((x) =>
      `${shortPath(x.path)} (${fmtSize(x.size)})`).join(" · ");
    tip = m.status === "duplicate_same_size"
      ? `Already present, identical size: ${paths}`
      : `Same filename, different size: ${paths}`;
  } else if (m.status === "installed") {
    const p = (m.local_matches || [])[0];
    tip = p ? `Installed: ${shortPath(p.path)}` : "Installed";
  } else if (m.status === "unknown_dest") {
    tip = "Destination folder couldn't be inferred from the note.";
  }
  const other = (m.other_category_matches || []).length
    ? ` <span class="cf-mf-other" title="Also exists elsewhere: ${esc((m.other_category_matches||[]).map(x=>shortPath(x.path)).join(' · '))}">•other folder</span>`
    : "";
  return `<span class="cf-mf-badge cf-mf-badge-${meta.cls}" title="${esc(tip)}">${meta.icon} ${meta.label}</span>${other}`;
}

function updateSummary() {
  const m = state.models;
  const n = (s) => m.filter((x) => x.status === s).length;
  const missing = n("missing") + n("unknown_dest");
  const dup = n("duplicate_same_size") + n("duplicate_diff_size");
  const inst = n("installed");
  const parts = [];
  if (missing) parts.push(`<b class="cf-mf-c-miss">${missing}</b> missing`);
  if (dup) parts.push(`<b class="cf-mf-c-warn">${dup}</b> duplicate(s)`);
  if (inst) parts.push(`<b class="cf-mf-c-ok">${inst}</b> installed`);
  overlayEl.querySelector(".cf-mf-summary").innerHTML = parts.join(" · ");

  // Ticked selection + total size.
  let sel = 0, totalBytes = 0, ready = 0;
  for (const [, row] of rowById) {
    if (row.cb.checked) {
      sel++;
      if (row.model.remote_size > 0) totalBytes += row.model.remote_size;
      if (canDownload(row.model)) ready++;
    }
  }
  const foot = overlayEl.querySelector(".cf-mf-foot-info");
  foot.innerHTML = sel ? `${sel} selected · ${fmtSize(totalBytes)}` : "";
  const btn = overlayEl.querySelector(".cf-mf-dl-all");
  btn.disabled = ready === 0;
  btn.textContent = ready ? `Download selected (${ready})` : "Download all";

  // badge topbar
  import("./main.js").then((mod) => mod.setBadge(missing)).catch(() => {});
}

function canDownload(m) {
  return !!(m._dlCategory || m.category) && m.status !== "installed" && !hostBlocked(m);
}

// The server refused to even probe this URL: its host is not on the download allow-list.
// Nothing the popup offers can change that, so the row says so instead of a dead button.
function hostBlocked(m) {
  return m.remote_size_error === "host_not_allowed";
}

const HOST_BLOCKED_MSG =
  "This host is not on the download allow-list (CF_MF_ALLOWED_HOSTS — see the README).";

// ---------------------------------------------------------------------------
// HuggingFace token panel (beginner guidance + paste)
// ---------------------------------------------------------------------------
function buildTokenPanel(gatedModels) {
  const panel = document.createElement("div");
  panel.className = "cf-mf-token-panel";

  const repos = [];
  const seen = new Set();
  for (const m of gatedModels || []) {
    const page = hfPageUrl(m.url);
    if (page && !seen.has(page)) { seen.add(page); repos.push(page); }
  }
  const repoList = repos.length
    ? `<ul class="cf-mf-token-repos">${repos.map((r) =>
        `<li><a href="${esc(r)}" target="_blank" rel="noopener">${esc(r.replace("https://huggingface.co/", ""))}</a></li>`).join("")}</ul>`
    : "";

  panel.innerHTML = `
    <div class="cf-mf-token-title">🔒 Some of these models need a free HuggingFace account</div>
    <p class="cf-mf-token-intro">These are "gated" models: the author asks you to accept their
      license before downloading. It's free, and you only need to do this once.</p>
    <ol class="cf-mf-token-steps">
      <li>Create a free account at
        <a href="https://huggingface.co/join" target="_blank" rel="noopener">huggingface.co/join</a>.</li>
      <li>Open each locked model page below and click
        <b>"Agree and access repository"</b>:${repoList}</li>
      <li>Create an access token (type <b>Read</b>) at
        <a href="https://huggingface.co/settings/tokens" target="_blank" rel="noopener">huggingface.co/settings/tokens</a>.</li>
      <li>Paste it below and click <b>Save</b>.</li>
    </ol>
    <div class="cf-mf-token-form">
      <input type="password" class="cf-mf-token-input" placeholder="hf_xxxxxxxxxxxxxxxx" autocomplete="off" spellcheck="false" />
      <button class="cf-mf-token-show" type="button" title="Show / hide">👁</button>
      <button class="cf-mf-token-save" type="button">Save token</button>
      <button class="cf-mf-token-forget" type="button" hidden
              title="Delete the token saved on this machine">Remove</button>
    </div>
    <div class="cf-mf-token-status"></div>`;

  wireTokenForm(panel);
  return panel;
}

function wireTokenForm(panel) {
  const input = panel.querySelector(".cf-mf-token-input");
  const showBtn = panel.querySelector(".cf-mf-token-show");
  const saveBtn = panel.querySelector(".cf-mf-token-save");
  const status = panel.querySelector(".cf-mf-token-status");

  showBtn.addEventListener("click", () => {
    input.type = input.type === "password" ? "text" : "password";
  });

  const doSave = async () => {
    const token = input.value.trim();
    if (!token) { setStatus(status, "err", "Please paste your token first."); return; }
    setStatus(status, "wait", "Checking your token…");
    saveBtn.disabled = true;
    try {
      const r = await api.fetchApi("/cf_mf/token", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ token }),
      });
      const d = await r.json();
      if (d.ok && d.valid) {
        setStatus(status, "ok",
          `✓ Saved — signed in as ${esc(d.username || "your account")}. Re-checking models…`);
        setTimeout(() => openPopup(lastNotes), 700);
      } else if (d.message || d.error) {
        // Refused before validation (malformed token, or a caller the server does not
        // serve): say what the server said rather than blaming the token's value.
        setStatus(status, "err", "✗ " + esc(d.message || d.error));
      } else {
        setStatus(status, "err",
          "✗ That token doesn't look valid — please check it and try again.");
      }
    } catch (e) {
      setStatus(status, "err", "Network error while checking the token.");
    } finally {
      saveBtn.disabled = false;
    }
  };

  saveBtn.addEventListener("click", doSave);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") doSave(); });

  // Removal of the saved token: a token put on the machine must be able to leave it.
  panel.querySelector(".cf-mf-token-forget").addEventListener("click", async () => {
    setStatus(status, "wait", "Removing…");
    try {
      await api.fetchApi("/cf_mf/token/clear", { method: "POST" });
      input.value = "";
      panel.querySelector(".cf-mf-token-forget").hidden = true;
      setStatus(status, "ok", "✓ Token removed. Re-checking models…");
      setTimeout(() => openPopup(lastNotes), 700);
    } catch (e) {
      setStatus(status, "err", "Network error while removing the token.");
    }
  });
}

function setStatus(el, kind, msg) {
  el.className = "cf-mf-token-status cf-mf-token-status-" + kind;
  el.innerHTML = msg;
}

// The header's 🔑 button: opens/folds a token panel on demand, from anywhere.
async function toggleTokenPanel() {
  const body = bodyEl();
  const existing = body.querySelector(".cf-mf-token-panel.cf-mf-token-ondemand");
  if (existing) { existing.remove(); return; }
  const panel = buildTokenPanel(state.models.filter((m) => m.remote_size_error === "401_gated"));
  panel.classList.add("cf-mf-token-ondemand");
  // Current token state.
  try {
    const r = await api.fetchApi("/cf_mf/token");
    const d = await r.json();
    if (d.present) {
      setStatus(panel.querySelector(".cf-mf-token-status"), "ok",
        "✓ A token is currently set. Paste a new one to replace it.");
      // Only offer removal when there is a file to delete: a token coming from the
      // environment (HF_TOKEN exported at launch) is not ours.
      panel.querySelector(".cf-mf-token-forget").hidden = !d.from_file;
    }
  } catch (e) { /* noop */ }
  body.insertBefore(panel, body.firstChild);
  panel.querySelector(".cf-mf-token-input").focus();
}

// ---------------------------------------------------------------------------
// Downloads
// ---------------------------------------------------------------------------
function jobFor(m) {
  // Snapshot of the subfolder the job is actually posted with. The input stays editable while
  // the download runs, so the live _subfolder may say something else by the time the job
  // finishes — and setRowDone must reason about where the file LANDED, not about the field.
  m._queuedSubfolder = m._subfolder || "";
  persistRow(m, { queuedSubfolder: m._queuedSubfolder });
  return {
    id: m.id,
    url: m.url,
    filename: m.filename,
    category: m._dlCategory || m.category,
    subfolder: m._queuedSubfolder,
    base_dir: m._baseDir || "",
  };
}

async function downloadOne(m) {
  if (!canDownload(m)) {
    flashRow(m.id, hostBlocked(m) ? HOST_BLOCKED_MSG : "Choose a destination folder first.");
    return;
  }
  await postDownload([jobFor(m)]);
}

async function downloadSelected() {
  const jobs = [];
  for (const [, row] of rowById) {
    if (row.cb.checked && canDownload(row.model)) jobs.push(jobFor(row.model));
  }
  if (jobs.length) await postDownload(jobs);
}

async function postDownload(jobs) {
  for (const j of jobs) {
    const row = rowById.get(j.id);
    if (row) setRowDownloading(row, { downloaded: 0, total: 0 });
  }
  try {
    const r = await api.fetchApi("/cf_mf/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ jobs }),
    });
    const data = await r.json();
    if (!data.ok) {
      // Refused as a whole (the local-only gate answers 403 with no per-job list): every
      // row posted must leave the downloading state, or it would spin forever.
      for (const j of jobs) setRowError(j.id, data.message || data.error || "refused");
      return;
    }
    for (const rej of data.rejected || []) {
      setRowError(rej.id, rej.reason || "refused");
    }
  } catch (e) {
    for (const j of jobs) setRowError(j.id, e.message || String(e));
  }
}

async function cancelJob(id) {
  try {
    await api.fetchApi("/cf_mf/cancel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id }),
    });
  } catch (e) { /* noop */ }
}

// ---------------------------------------------------------------------------
// Per-row download states
// ---------------------------------------------------------------------------
function setRowDownloading(row, { downloaded, total, speed }) {
  // The progress UI (bar + ✕ button) is built ONCE: later progress events only update the bar
  // and the text. Rebuilding the DOM on every event (~4/s) destroyed the ✕ button between
  // mousedown and mouseup → the click was lost.
  if (row.state !== "downloading" && row.state !== "cancelling") {
    row.state = "downloading";
    row.el.classList.add("is-downloading");
    row.el.classList.remove("is-cancelling");
    row.action.innerHTML = "";
    const wrap = document.createElement("div");
    wrap.className = "cf-mf-progress-wrap";
    wrap.innerHTML = `
      <div class="cf-mf-progress"><div class="cf-mf-progress-bar" style="width:0%"></div></div>
      <div class="cf-mf-progress-txt"></div>`;
    const cancel = document.createElement("button");
    cancel.className = "cf-mf-cancel";
    cancel.textContent = "✕";
    cancel.title = "Cancel";
    cancel.addEventListener("click", () => {
      if (row.state !== "downloading") return;
      row.state = "cancelling";
      row.el.classList.add("is-cancelling");
      cancel.disabled = true;
      row._progressTxt.textContent = "Cancelling…";
      cancelJob(row.model.id);
    });
    row._progressBar = wrap.querySelector(".cf-mf-progress-bar");
    row._progressTxt = wrap.querySelector(".cf-mf-progress-txt");
    row.action.append(wrap, cancel);
  }
  if (row.state === "cancelling") return; // display frozen on "Cancelling…"
  const pct = total ? Math.min(100, Math.round((downloaded / total) * 100)) : 0;
  row._progressBar.style.width = pct + "%";
  row._progressTxt.textContent =
    `${fmtSize(downloaded)}${total ? " / " + fmtSize(total) : ""}${speed ? " · " + fmtSpeed(speed) : ""}`;
}

function setRowError(id, msg) {
  const row = rowById.get(id);
  if (!row) return;
  row.state = "error";
  row.errMsg = msg;
  row.el.classList.remove("is-downloading", "is-cancelling");
  row.action.innerHTML = "";
  row.action.append(...buildErrorActions(row));
}

// Content of the action column of a row in error (message + Retry). Extracted so flashRow can
// rebuild it: a flash message must not make Retry disappear.
function buildErrorActions(row) {
  const err = document.createElement("div");
  err.className = "cf-mf-row-err";
  err.textContent = row.errMsg || "error";
  err.title = row.errMsg || "";
  const retry = document.createElement("button");
  retry.className = "cf-mf-dl-one";
  retry.textContent = "Retry";
  retry.addEventListener("click", () => downloadOne(row.model));
  return [err, retry];
}

function setRowDone(id) {
  const row = rowById.get(id);
  if (!row) return;
  row.state = "done";
  row.el.classList.remove("is-downloading", "is-cancelling");
  row.el.classList.add("is-done");
  row.model.status = "installed";
  row.cb.checked = false;
  // Without this, the tick remembered before the download would leave the now-installed model
  // ticked again (and counted in the total) the next time the popup opens.
  row.model._checked = false;
  persistRow(row.model, { checked: false });
  row.action.innerHTML = `<span class="cf-mf-done-badge">✓ Installed</span>`;
  // Only a file landing at the ROOT of its folder resolves under its bare name. Downloaded
  // into a subfolder, the loader nodes stay just as broken as before — hiding the relink line
  // there would remove the only control that fixes them. Judged on the subfolder the job was
  // POSTED with (snapshotted by jobFor), not on the live input, which the user may have edited
  // while the download ran. Never un-hides: a row that had nothing to relink still has nothing.
  // A done with no snapshot means the job predates this popup session entirely (resync after
  // a reload): the live field is then the only information there is.
  const sub = row.model._queuedSubfolder ?? row.model._subfolder;
  if (!sub) row.linkLine.hidden = true;
  const badge = row.info.querySelector(".cf-mf-badge");
  if (badge) {
    badge.className = "cf-mf-badge cf-mf-badge-ok";
    badge.textContent = "✓ Installed";
  }
  updateSummary();
}

// A transient message, shown IN THE LINE that produced it: a relink refusal next to Relink,
// a missing destination next to Download. Landing them all in one slot would blame the wrong
// button.
function flashRow(id, msg, which = "download") {
  const row = rowById.get(id);
  if (!row) return;
  const slot = which === "link" ? row.actionLink : row.action;
  const wasState = row.state;
  const flash = document.createElement("span");
  flash.className = "cf-mf-row-err";
  flash.textContent = msg;
  slot.innerHTML = "";
  slot.appendChild(flash);
  setTimeout(() => {
    // Whatever painted over the message is fresher than this timer: a new render after a
    // re-analysis, a copy-menu change, a "✓ Linked" badge from a successful relink. All of
    // them disconnect our flash element, and repainting from here would clobber current
    // content with data captured 2.5 s ago — this timer only ever cleans up its own message.
    if (!flash.isConnected) return;
    // The relink line has no state machine: row.state describes the DOWNLOAD slot only, and
    // guarding this restore on it stranded the message here — Relink button and all — as soon
    // as a download started within the delay. Both buttons are visible at once now, so that
    // was one click away.
    if (which === "link") {
      if (state.workflowKey === currentWorkflowKey()) { refreshLinkParts(row.model); return; }
      // Workflow mismatched: nothing may be read from the graph, but a red message with no
      // button under it is a dead end — the re-analysis that normally follows a tab switch is
      // not guaranteed to land. Restore the button with the copy the MENU displays (pure DOM,
      // no graph read): the select still shows what the last valid render resolved, whereas
      // `_relinkPick || relink` skipped the graph-derived tier and could promise a copy the
      // picker visibly does not show. The click re-checks the workflow and refuses again if
      // nothing has moved.
      const sel = row.info.querySelector(".cf-mf-copysel");
      const cands = relinkCandidates(row.model);
      const pick = (sel && cands[Number(sel.value)])
        || row.model._relinkPick || row.model.relink;
      slot.innerHTML = "";
      if (pick) slot.appendChild(buildRelinkButton(row.model, { found: 1, linked: 0 }, pick));
      return;
    }
    if (row.state !== wasState) return; // a download/success took over
    slot.innerHTML = "";
    // Restore the state we came from: a row in error must get its Retry back, otherwise the
    // flash message leaves it with no button at all.
    slot.append(...(wasState === "error"
      ? buildErrorActions(row)
      : [buildIdleButton(row.model)]));
  }, 2500);
}

// ---------------------------------------------------------------------------
// Websocket events (registered once from main.js)
// ---------------------------------------------------------------------------
export function wireEvents(apiRef) {
  apiRef.addEventListener("cf_mf.progress", (e) => {
    const d = e.detail || {};
    const row = rowById.get(d.id);
    // A throttled progress event can land AFTER the cancellation/error: it must not bring the
    // row back to life.
    if (row && row.state !== "error" && row.state !== "done") {
      setRowDownloading(row, { downloaded: d.downloaded, total: d.total, speed: d.speed_bps });
    }
  });
  apiRef.addEventListener("cf_mf.done", (e) => setRowDone((e.detail || {}).id));
  apiRef.addEventListener("cf_mf.error", (e) => {
    const d = e.detail || {};
    setRowError(d.id, d.message || d.code || "error");
  });
}
