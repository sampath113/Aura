"use strict";

const state = {
  server: localStorage.getItem("aura.server") || "",
  docs: [],
  stats: {},
  settings: {},
  model: {},
  answers: [],
  address: {},
  shell: "",
  local: true,
  host: {},
  busy: false,
};

/* ------------------------------------------------------------------ the theme
   Settings offers Auto / Light / Dark. The stored choice is applied by the tiny
   script in index.html before the first paint (so a dark screen never flashes
   white); these are the same decision, made again whenever it changes. */
function currentTheme() {
  try { return localStorage.getItem("aura.theme") || "auto"; } catch (error) { return "auto"; }
}

function applyTheme(choice) {
  const follows = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  const dark = choice === "dark" || (choice === "auto" && follows);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", dark ? "#212121" : "#ffffff");
}

function setTheme(choice) {
  try { localStorage.setItem("aura.theme", choice); } catch (error) { /* private browsing */ }
  applyTheme(choice);
  document.querySelectorAll("#themeRow button").forEach((button) => {
    button.classList.toggle("on", button.dataset.themeChoice === choice);
  });
}

const isNarrow = () => Boolean(window.matchMedia && window.matchMedia("(max-width: 900px)").matches);


const el = (id) => document.getElementById(id);

/* --------------------------------------------------------------- the shell
   A desktop build is a web page talking to its own server, and so is the
   Android app - except that the phone can do two things a web page cannot:
   choose files with the system picker, and hand the page to the phone's own
   browser. The app injects "AndroidAura" for exactly those two; everything
   else (including every API call) stays ordinary HTTP to 127.0.0.1.
   --------------------------------------------------------------------------- */
const androidBridge = () => (window.AndroidAura && typeof window.AndroidAura === "object")
  ? window.AndroidAura : null;
const onAndroid = () => Boolean(androidBridge() && androidBridge().platform);

function androidResult(raw) {
  try { return JSON.parse(raw || "{}"); } catch (error) { return { error: String(error) }; }
}

/* Ask the phone for files or a folder; resolves to {paths: [...]} or {error}. */
function pickOnAndroid(kind) {
  const bridge = androidBridge();
  if (!bridge || !bridge.pick) return { error: "this build cannot show a file picker" };
  // The bridge hands the picker's answer back in one synchronous call, which
  // means no page code at all runs while the picker is on screen - so a bubble
  // left over from the previous action would be stuck there for the whole
  // time. Take it down before handing over.
  removeToasts();
  return androidResult(bridge.pick(kind));
}

/* ---------------------------------------------------------------- transport */
async function api(method, path, body) {
  if (window.AndroidAura && window.AndroidAura.request) {
    const payload = body === undefined ? "" : JSON.stringify(body);
    const raw = window.AndroidAura.request(method, state.server + path, payload);
    const parsed = JSON.parse(raw || "{}");
    if (parsed.error) throw new Error(parsed.error);
    return parsed;
  }
  const options = { method, headers: {} };
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(state.server + path, options);
  const text = await response.text();
  let parsed = {};
  try { parsed = text ? JSON.parse(text) : {}; } catch (error) { parsed = { error: text.slice(0, 300) }; }
  if (!response.ok || parsed.error) throw new Error(parsed.error || ("HTTP " + response.status));
  return parsed;
}

async function uploadFile(file) {
  if (window.AndroidAura && window.AndroidAura.request) {
    const base64 = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(",")[1] || "");
      reader.onerror = () => reject(new Error("could not read " + file.name));
      reader.readAsDataURL(file);
    });
    return api("POST", "/api/upload?name=" + encodeURIComponent(file.name), { __base64: base64 });
  }
  const options = { method: "POST", headers: { "X-Filename": file.name }, body: file };
  const response = await fetch(state.server + "/api/upload?name=" + encodeURIComponent(file.name), options);
  const parsed = await response.json().catch(() => ({}));
  if (!response.ok || parsed.error) throw new Error(parsed.error || ("HTTP " + response.status));
  return parsed;
}

/* A name short enough for one line of a toast. */
function shortName(name) {
  const text = String(name || "");
  return text.length > 46 ? text.slice(0, 43) + "\u2026" : text;
}

/* Hand each chosen file to AURA, saying which one is being read.

   The "Reading ..." bubble is deliberately sticky: a forty-megabyte PDF takes
   far longer than a toast's five seconds, and a message that disappears while
   the work is still running is worse than none. It is taken down in the
   `finally`, so it cannot outlive the attempt whether that attempt succeeds,
   fails, or is abandoned half way. */
async function uploadEach(files) {
  for (const file of files) {
    const progress = toast("Reading " + shortName(file.name) + "\u2026", false, { sticky: true });
    try {
      await uploadFile(file);
    } catch (error) {
      toast(file.name + " - " + friendlyError(error), true);
    } finally {
      progress.remove();
    }
  }
}

/* -------------------------------------------------------------------- toast
   One bubble at a time, and each bubble gets rid of itself.

   This used to be a single shared timer: the second toast to arrive cleared the
   first one's timeout, so the first bubble was never removed and stayed on
   screen for ever - which is exactly how "Reading <file>..." ended up welded to
   the bottom of the window. Every node now carries its own expiry (and the
   sweeper below catches anything a stalled thread delayed), so a toast cannot
   outlive its welcome no matter what else is going on.
   --------------------------------------------------------------------------- */
const TOAST_MS = 5200;

function toast(message, bad, options) {
  const node = document.createElement("div");
  node.className = "toast" + (bad ? " bad" : "");
  node.textContent = message;
  document.body.appendChild(node);
  // Whatever was there is stale news the moment this one appears.
  removeToasts(node);
  const lifetime = options && options.sticky ? 0 : TOAST_MS;
  if (lifetime > 0) {
    node.dataset.expires = String(Date.now() + lifetime);
    setTimeout(() => node.remove(), lifetime);
  } else {
    node.dataset.expires = "";
  }
  return node;
}

/* Take every bubble off the screen; `keep` spares one (the newest). */
function removeToasts(keep) {
  for (const node of document.querySelectorAll(".toast")) {
    if (node !== keep) node.remove();
  }
  return true;
}

/* A dead engine and a rejected request both surface as "Failed to fetch",
   which tells the user nothing useful, so say what actually happened. */
function friendlyError(error) {
  const message = String((error && error.message) || error || "something went wrong");
  if (/failed to fetch|networkerror|load failed|network request failed/i.test(message)) {
    return "Lost the connection to the AURA engine. Check that the AURA window is still open.";
  }
  return message;
}

async function engineAlive() {
  try {
    const response = await fetch(state.server + "/health", { cache: "no-store" });
    return response.ok;
  } catch (error) {
    return false;
  }
}

/* -------------------------------------------------------------------- utils */
function escapeHtml(text) {
  return (text || "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderRich(text) {
  const lines = escapeHtml(text || "").split("\n");
  let html = "";
  let inList = false;
  for (const line of lines) {
    const bullet = /^\s*[-*\u2022]\s+/.test(line);
    if (bullet) {
      if (!inList) { html += "<ul>"; inList = true; }
      html += "<li>" + inline(line.replace(/^\s*[-*\u2022]\s+/, "")) + "</li>";
    } else {
      if (inList) { html += "</ul>"; inList = false; }
      html += inline(line) + (line.trim() ? "<br>" : "");
    }
  }
  if (inList) html += "</ul>";
  return html.replace(/(<br>\s*)+$/, "");

  function inline(value) {
    return value
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[\s(])_([^_\n]{2,90})_(?=[\s).,;:!?]|$)/g, "$1<em>$2</em>")
      .replace(/\[(S\d+)(?:\s*:\s*([^\]]+?))?\]/g, (match, label, rest) =>
        '<span class="cite" data-label="' + label + '" title="' + (rest || label) + '">' +
        label + "</span>");
  }
}

function humanBytes(n) {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return n.toFixed(n < 10 && i ? 1 : 0) + " " + units[i];
}

/* ------------------------------------------------------------------ library */
function renderDocuments() {
  const box = el("docList");
  if (!state.docs.length) {
    box.innerHTML = '<div class="stats">No documents yet. Add a PDF, notes file or an image to begin.</div>';
    return;
  }
  box.innerHTML = state.docs.map((doc) => {
    const notes = (doc.notes || []).map((note) => '<div class="docNote">' + escapeHtml(note) + "</div>").join("");
    return '<div class="doc" data-id="' + doc.doc_id + '">' +
      '<div class="docMain"><div class="docName">' + escapeHtml(doc.name) + "</div>" +
      '<div class="docMeta">' + escapeHtml(doc.kind) + " &middot; " + doc.pages + " page(s) &middot; " +
      doc.chunks + " passages &middot; " + humanBytes(doc.chars) + "</div>" + notes + "</div>" +
      '<button class="docDel" title="Remove">&times;</button></div>';
  }).join("");
  box.querySelectorAll(".docDel").forEach((button) => {
    button.onclick = async (event) => {
      const id = event.target.closest(".doc").dataset.id;
      try { await api("GET", "/api/document/" + id); toast("Removed"); loadLibrary(); }
      catch (error) { toast(friendlyError(error), true); }
    };
  });
}

function renderStats() {
  const stats = state.stats || {};
  el("indexPill").textContent = (stats.chunks || 0) + " passages";
  el("densePill").textContent = stats.dense && stats.dense !== "off" && stats.dense !== "not built"
    ? "semantic: " + stats.dense : "keyword only";
  el("densePill").className = "pill" + (stats.vectors ? " good" : "");
  el("statsBox").textContent = (stats.documents || 0) + " documents, " + (stats.chunks || 0) +
    " passages " + ((stats.chars || 0) ? "(" + humanBytes(stats.chars) + ")" : "");
}

async function loadLibrary() {
  const payload = await api("GET", "/api/library");
  state.docs = payload.documents || [];
  state.stats = payload.stats || {};
  renderDocuments();
  renderStats();
}

/* The phone build explains itself here: how to give AURA access to the user's
   files, and which folder the downloads go to. On a desktop this stays empty. */
function renderHostNotice() {
  const box = el("hostNotice");
  if (!box) return;
  const host = state.host || {};
  const bits = [];
  if (host.android) {
    if (!host.can_read_files) {
      bits.push('<div class="noticeLine">AURA cannot read your files yet. "Choose files" still works; ' +
        "granting access lets it index a whole folder." +
        ' <button class="ghost small" id="grantBtn">Allow access</button></div>');
    } else if (host.storage_root) {
      bits.push('<div class="noticeLine">Files are read from <code>' + escapeHtml(host.storage_root) +
        "</code> and its subfolders.</div>");
    }
  }
  box.innerHTML = bits.join("");
  box.hidden = !bits.length;
  renderEmptyLead(host);
  const grant = el("grantBtn");
  if (grant) grant.onclick = () => {
    const bridge = androidBridge();
    if (bridge && bridge.openStorageSettings) bridge.openStorageSettings();
  };
}

/* "Everything stays on this machine" is the wrong sentence on a phone. The
   markup keeps the desktop wording, and this swaps the one word rather than
   holding a second copy of the paragraph. */
let emptyLeadText = "";

function renderEmptyLead(host) {
  const lead = el("emptyLead");
  if (!lead) return;
  if (!emptyLeadText) emptyLeadText = lead.textContent;
  lead.textContent = host && host.android
    ? emptyLeadText.replace("this machine", "this phone")
    : emptyLeadText;
}

async function loadHealth() {
  const health = await api("GET", "/health");
  state.settings = health.settings || {};
  state.stats = (health.stats || {});
  state.model = health.model || {};
  state.address = health.address || {};
  state.shell = health.shell || "";
  state.host = health.host || {};
  state.local = !(health.client && health.client.local === false);
  const backend = health.backend || "extractive";
  el("backendPill").textContent = backend;
  el("backendPill").className = "pill" + (/extractive/.test(backend) ? "" : " good");
  el("backendPill").title = state.model.detail || backend;
  el("backendPill").onclick = openSettings;
  // "local model problem" is the wrong sentence for a build that simply has no
  // engine in it: nothing is broken, there is just no model to run, and the
  // one-line status is no place to explain that - the settings screen does it.
  const noEngine = Boolean(state.model.engine_bundled) && !state.model.engine_installed;
  const modelBit = noEngine ? " - quoted answers only"
    : (state.model.state === "ready" ? " - local model ready"
      : (state.model.state === "starting" ? " - local model loading"
        : (state.model.state === "error" ? " - local model problem" : "")));
  const where = state.host.android ? "on this phone" : (state.stats.root || "");
  // One line, shortened by CSS where there is no room for it rather than by a
  // second copy of the sentence here.
  el("statusLine").textContent =
    health.app + " " + health.version + " - " + backend + modelBit + (where ? " - " + where : "");
  renderHostNotice();
  renderStats();
}

/* ---------------------------------------------------------------------- ask */
const MODE_LABELS = {
  extractive: "quoted from your sources (no model)",
  outline: "outline of your library (no model)",
  closest: "closest passages (no direct match)",
  "no-evidence": "nothing matched",
  local: "written by your local model from your sources",
  llamacpp: "local model",
  server: "model server",
};

function modeLabel(mode) {
  const key = String(mode || "extractive").split(" ")[0];
  return MODE_LABELS[key] || mode;
}

/* A question is a rounded block on the right, an answer is plain text in the
   column - the one difference that makes a conversation readable at a glance.
   The empty state goes as soon as there is something to read. */
function addMessage(role, html, meta) {
  const empty = el("emptyState");
  if (empty) empty.remove();
  const node = document.createElement("div");
  node.className = "msg " + role;
  node.innerHTML = '<div class="bubble">' + html + '</div><div class="meta">' + (meta || "") + "</div>";
  el("thread").appendChild(node);
  scrollToEnd();
  return node;
}

function scrollToEnd() {
  const box = el("chatScroll");
  if (box) box.scrollTop = box.scrollHeight;
}

function setBubble(node, html) {
  const bubble = node && node.querySelector(".bubble");
  if (bubble) bubble.innerHTML = html;
}

function setMeta(node, html) {
  const meta = node && node.querySelector(".meta");
  if (meta) meta.innerHTML = html || "";
}

/* The passages an answer was drawn from. Closed by default: the answer is the
   answer, and the sources are one tap away - the citation chips in the text
   open this box rather than pointing at it. */
function sourcesHtml(hits, mode) {
  if (!hits || !hits.length) return "";
  const items = hits.map((hit) => {
    const chunk = hit.chunk || {};
    const score = (hit.bm25 || 0).toFixed(2);
    const dense = hit.dense ? " &middot; semantic " + hit.dense.toFixed(2) : "";
    return '<div class="src" data-label="' + hit.label + '">' +
      '<div class="srcHead"><span><span class="srcTag">' + hit.label + "</span> " +
      escapeHtml(chunk.doc_name) + ", page " + chunk.page +
      (chunk.heading ? " &middot; " + escapeHtml(chunk.heading) : "") + "</span>" +
      "<span>score " + score + dense + "</span></div>" +
      '<div class="srcText">' + escapeHtml(chunk.text || "") + "</div></div>";
  }).join("");
  const count = hits.length === 1 ? "1 source" : hits.length + " sources";
  // A quoted answer is the normal case and needs no explanation; anything else
  // is worth saying out loud - a written answer is the model's words rather
  // than the page's, and the reader should know which one they are reading.
  const tail = mode && mode !== "extractive" ? " &middot; " + escapeHtml(modeLabel(mode)) : "";
  return '<details class="sources"><summary>' + count + tail + "</summary>" +
    '<div class="srcList">' + items + "</div></details>";
}

async function ask(question) {
  if (state.busy) return;
  question = (question || "").trim();
  if (!question) { toast("Type a question first", true); return; }
  if (!state.docs.length) { toast("Add a document to your library first", true); return; }
  state.busy = true;
  updateSend();
  addMessage("user", escapeHtml(question));
  const pending = addMessage("aura",
    '<span class="thinking"><span class="dot"></span><span class="dot"></span>' +
    '<span class="dot"></span><span class="thinkingText"> searching ' +
    (state.stats.chunks || 0) + " passages</span></span>");
  let seconds = 0;
  const ticker = setInterval(() => {
    seconds += 1;
    const label = pending.querySelector(".thinkingText");
    if (label) label.textContent = " searching and writing (" + seconds + "s)";
  }, 1000);
  try {
    const payload = await api("POST", "/api/ask", { question });
    clearInterval(ticker);
    const checks = payload.checks || {};
    const mode = payload.mode || "extractive";
    // The mode and the passage count live in the sources box; underneath the
    // answer only a real warning is shown, so what is left is the answer.
    const warnings = (checks.notes || [])
      .filter((note) => !(mode === "no-evidence" && /nothing matched/i.test(note)))
      .map((note) => '<span class="warn">' + escapeHtml(note) + "</span>");
    if (checks.fallback) warnings.unshift('<span class="warn">nearest passages, not a direct match</span>');
    setBubble(pending, renderRich(payload.text || ""));
    setMeta(pending, warnings.join(" &middot; "));
    const sources = sourcesHtml(payload.hits, mode);
    if (sources) pending.insertAdjacentHTML("beforeend", sources);
    wireCitations(pending);
    addAnswerActions(pending, payload);
    state.answers.push(payload);
    state.answers.push(payload);
  } catch (error) {
    clearInterval(ticker);
    setBubble(pending, '<span class="warn">' + escapeHtml(friendlyError(error)) + "</span>");
    setMeta(pending, "");
  } finally {
    state.busy = false;
    scrollToEnd();
    updateSend();
  }
}

/* Tapping a [S1] chip in the answer opens the sources box and brings that
   passage into view - the alternative is a label that does nothing. */
function wireCitations(scope) {
  scope.querySelectorAll(".cite").forEach((chip) => {
    chip.onclick = () => {
      const target = scope.querySelector('.src[data-label="' + chip.dataset.label + '"]');
      if (!target) return;
      const box = target.closest("details");
      if (box) box.open = true;
      target.classList.add("highlight");
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      setTimeout(() => target.classList.remove("highlight"), 2400);
    };
  });
}

/* An answer is something a student wants in their notes, so it can be copied
   whole - the citations come with it, which is the point of them. */
async function copyText(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch (error) { /* not allowed here; fall through to the old way */ }
  try {
    const box = document.createElement("textarea");
    box.value = text;
    box.setAttribute("readonly", "");
    box.style.cssText = "position:fixed;top:0;left:0;opacity:0";
    document.body.appendChild(box);
    box.select();
    const copied = document.execCommand("copy");
    box.remove();
    return copied;
  } catch (error) {
    return false;
  }
}

function addAnswerActions(node, payload) {
  const text = String(payload.text || "").trim();
  if (!text) return;
  const row = document.createElement("div");
  row.className = "msgActions";
  const button = document.createElement("button");
  button.className = "actBtn";
  button.textContent = "Copy";
  button.onclick = async () => {
    const copied = await copyText(text);
    button.textContent = copied ? "Copied" : "Could not copy";
    setTimeout(() => { button.textContent = "Copy"; }, 1600);
  };
  row.appendChild(button);
  node.appendChild(row);
}

/* The send button is dead until there is something to send, the way the round
   button in a chat window behaves. */
function updateSend() {
  const box = el("questionInput");
  const button = el("askBtn");
  if (!box || !button) return;
  button.disabled = state.busy || !box.value.trim();
}

/* ------------------------------------------------------------------- models
   The local model lives entirely on this machine: AURA downloads the model
   file and llama.cpp's llama-server, then starts it and asks it to write the
   answers. Nothing here needs an account, a key, or the internet afterwards.
   --------------------------------------------------------------------------- */
const modelState = { data: null, timer: null, error: "", test: null };

async function refreshModels() {
  try {
    modelState.data = await api("GET", "/api/models");
    modelState.error = "";
  } catch (error) {
    modelState.error = friendlyError(error);
  }
  renderModels();
}

function modelBusy() {
  const data = modelState.data || {};
  return Boolean((data.job && !data.job.done) || (data.engine && data.engine.state === "starting"));
}

function scheduleModelPoll() {
  stopModelPoll();
  const delay = modelBusy() ? 1200 : 6000;
  modelState.timer = setTimeout(async () => {
    modelState.timer = null;
    if (!el("settingsSheet") || el("settingsSheet").hidden) return;
    await refreshModels();
    scheduleModelPoll();
  }, delay);
}

function stopModelPoll() {
  if (modelState.timer) {
    clearTimeout(modelState.timer);
    modelState.timer = null;
  }
}

function jobCardHtml(job) {
  if (!job) return "";
  const tone = job.state === "error" ? " bad" : (job.state === "done" ? " good" : " warn");
  const meta = job.state === "error" ? (job.error || "failed")
    : (job.detail || job.state);
  const bar = (job.state === "running" || job.state === "queued")
    ? '<div class="bar"><i style="width:' + (job.percent || 0) + '%"></i></div>' : "";
  const cancel = job.done ? ""
    : '<button class="ghost small" data-model-action="cancel" data-job="' + job.id + '">Cancel</button>';
  return '<div class="modelCard">' +
    '<div class="modelRow"><div class="modelName">' + escapeHtml(job.label) + '</div>' +
    '<div class="modelState' + tone + '">' + escapeHtml(job.percent + "% - " + job.state) + "</div></div>" +
    bar + '<div class="modelMeta">' + escapeHtml(meta) + "</div>" +
    (cancel ? '<div class="actions">' + cancel + "</div>" : "") + "</div>";
}

/* Where downloaded models are kept. On a desktop this is <data>/models; on a
   phone it starts on the user's own storage and can be moved to a folder of
   their choosing (or an SD card), which is the only way a 4 GB file makes
   sense there. */
function folderCardHtml(data) {
  const folder = (data && data.folder) || {};
  const path = folder.path || "";
  const byDefault = folder.default || "";
  const android = onAndroid();
  const tone = folder.writable === false ? "bad" : "good";
  return '<div class="modelCard">' +
    '<div class="modelRow"><div class="modelName">Models folder</div>' +
    '<div class="modelState ' + tone + '">' +
    (folder.writable === false ? "not writable" : "writable") + "</div></div>" +
    '<div class="modelMeta">Downloaded models are saved here.' +
    (android ? " A model is a gigabyte or more, so pick somewhere with room." : "") + "</div>" +
    '<div class="folderRow">' +
    '<input id="modelsDirInput" type="text" value="' + escapeHtml(path) + '" spellcheck="false">' +
    (android ? '<button class="ghost small" data-model-action="folder-browse">Browse...</button>' : "") +
    '<button class="ghost small" data-model-action="folder">Save</button>' +
    "</div>" +
    (folder.chosen && byDefault && byDefault !== path
      ? '<div class="modelMeta">AURA started with <code>' + escapeHtml(byDefault) +
        "</code>. Clear the box and press Save to go back to it.</div>"
      : "") +
    "</div>";
}

function modelSectionHtml() {
  if (!modelState.data) {
    return '<div class="field hint">' + escapeHtml(modelState.error || "Loading the model list...") + "</div>";
  }
  const data = modelState.data;
  const engine = data.engine || {};
  const eng = engine.engine || {};
  const installed = data.installed || [];
  const catalog = data.catalog || [];
  const selected = data.selected || "";
  const running = engine.state === "ready";
  const downloading = modelBusy();
  let html = "";

  // The engine that runs models ships inside the Android app, so a build that
  // lost it on the way through the build machine cannot load a model at all -
  // no setting, no button and no amount of waiting will change that. That is a
  // different animal from "you have not chosen a model yet", and saying
  // "could not start" about it sends the reader looking for a fault in their
  // own setup. So it gets its own words, its own state, and no buttons that
  // cannot possibly work.
  const bundled = Boolean(engine.engine_bundled);
  const engineAbsent = bundled && !engine.engine_installed;
  const engineMissing = (eng && eng.missing) || [];
  const engineWhy = engineMissing.length
    ? "The model engine inside this build is incomplete - " + engineMissing.join(", ") +
      (engineMissing.length === 1 ? " is" : " are") + " missing from the app."
    : "This copy of AURA was installed without the model engine inside it.";

  const tone = engineAbsent ? "bad" : (running ? "good" : (engine.state === "error" ? "bad" : "warn"));
  const headline = engineAbsent ? "no engine in this build"
    : (running ? "ready"
      : (engine.state === "starting" ? "loading..."
        : (engine.state === "error" ? "could not start"
          : (selected ? "not running" : "no model chosen yet"))));
  const detail = engineAbsent
    ? engineWhy + " Nothing you change on this screen can fix that: the engine has to be in the app " +
      "when it is built. Search, indexing and quoted answers all still work - only written answers " +
      "need a model, so install a build of AURA that includes the engine." +
      (engine.detail && engine.detail.indexOf("without the model engine") < 0
        ? " (" + engine.detail + ")" : "")
    : (engine.detail || "");
  html += '<div class="modelCard">' +
    '<div class="modelRow"><div class="modelName">Local model</div>' +
    '<div class="modelState ' + tone + '">' + escapeHtml(headline) + "</div></div>" +
    '<div class="modelMeta">' + escapeHtml(detail) + "</div>" +
    (running ? '<div class="modelMeta">AURA writes its answers with this model, using the ' +
      "passages it found in your own documents.</div>" : "") +
    (running && engine.model_name
      ? '<div class="modelMeta">' + escapeHtml(engine.model_name + " on " + engine.url) + "</div>" : "") +
    (engine.log_tail ? '<pre class="modelLog">' + escapeHtml(engine.log_tail) + "</pre>" : "") +
    '<div class="actions">' +
    (engineAbsent
      // Nothing to load and nothing to test, so offer the one thing that can
      // help: look again, in case a newer build has been installed since.
      ? '<button class="ghost small" data-model-action="refresh">Check again</button>'
      : (running ? '<button class="ghost small" data-model-action="stop">Stop the model</button>' : "") +
        (selected && !running ? '<button class="ghost small" data-model-action="start">Load the model</button>' : "") +
        (selected ? '<button class="ghost small" data-model-action="test"' + (downloading ? " disabled" : "") +
          ">Test the model</button>" : "")) +
    "</div></div>";

  const engineLine = bundled
    ? (engine.engine_installed
      ? "llama.cpp " + (eng.tag || "engine") + " for Android, shipped inside this app. Nothing to download."
      : engineWhy + " The app still reads your documents, searches them, and answers by quoting the " +
        "passages it matched - it just cannot write new sentences. This is a fault in the copy that " +
        "was installed, not in your settings.")
    : (engine.engine_installed
      ? "llama.cpp " + (eng.tag || "build") + " for " + (engine.platform || "") + " " +
        (engine.arch || "") + " (" + (eng.size || "") + " in " + (eng.folder || "") + ")"
      : "AURA needs llama.cpp's llama-server to run a model. It is a " +
        (eng.download_mb || 18) + " MB download - no compiler, no extra software.");
  html += '<div class="modelCard">' +
    '<div class="modelRow"><div class="modelName">Model engine</div>' +
    '<div class="modelState ' + (engine.engine_installed ? "good" : (bundled ? "bad" : "warn")) + '">' +
    (engine.engine_installed ? (bundled ? "built in" : "installed")
                             : (bundled ? "not in this build" : "not installed")) + "</div></div>" +
    '<div class="modelMeta">' + escapeHtml(engineLine) + "</div>" +
    (engineAbsent
      // Which build is on the phone decides whether the engine is there, so say
      // it plainly: "did the new one actually install?" should be answerable
      // from this screen.
      ? '<div class="modelMeta">This app reports itself as build <code>' +
        escapeHtml((engine.host || {}).app_version || "unknown") +
        "</code>. A build made with the engine says <code>built in</code> above instead of " +
        "<code>not in this build</code>.</div>"
      : "") +
    (bundled ? "" :
      '<div class="actions"><button class="ghost small" data-model-action="engine"' +
      (downloading ? " disabled" : "") + ">" +
      (engine.engine_installed ? "Reinstall the engine" : "Install the engine") + "</button></div>") +
    "</div>";

  html += folderCardHtml(data);

  if (installed.length) {
    html += '<div class="sectionTitle">Models on ' + (onAndroid() ? "this phone" : "this machine") +
      "</div>";
    html += installed.map((item) => {
      const isSelected = Boolean(selected) && item.path === selected;
      const label = isSelected ? (running ? "in use" : "chosen") : "";
      return '<div class="modelCard' + (isSelected ? " chosen" : "") + '">' +
        '<div class="modelRow"><div class="modelName">' + escapeHtml(item.title) + "</div>" +
        '<div class="modelState ' + (isSelected ? "good" : "") + '">' + escapeHtml(label) + "</div></div>" +
        '<div class="modelMeta">' + escapeHtml(item.name) + " &middot; " + escapeHtml(item.size) +
        (item.part ? " &middot; unfinished - download again to resume"
                   : (item.known ? "" : " &middot; not from AURA's list")) + "</div>" +
        '<div class="actions">' +
        (item.part ? "" : '<button class="ghost small" data-model-action="use" data-path="' +
          escapeHtml(item.path) + '">Use this</button>') +
        '<button class="ghost small" data-model-action="remove" data-path="' +
        escapeHtml(item.path) + '">Remove</button></div></div>';
    }).join("");
  }

  html += '<div class="sectionTitle">Models AURA can download</div>';
  if (engineAbsent) {
    html += '<div class="modelCard"><div class="modelMeta bad">Downloading one of these is wasted for ' +
      "now: it is hundreds of megabytes, and this copy of AURA has nothing able to run it. Install a " +
      "build that includes the model engine first.</div></div>";
  }
  html += catalog.map((model) => {
    const ready = model.state === "ready";
    const partial = !ready && model.on_disk > 0;
    const status = ready ? (selected === model.path ? "chosen" : "downloaded") : (partial ? "unfinished" : "");
    const meta = escapeHtml(model.params + " " + model.quant) + " &middot; " +
      escapeHtml(model.size) + " &middot; about " + escapeHtml(String(model.ram_gb)) +
      " GB of RAM &middot; " + escapeHtml(model.license);
    return '<div class="modelCard' + (model.recommended ? " pick" : "") + '">' +
      '<div class="modelRow"><div class="modelName">' + escapeHtml(model.name) +
      (model.recommended ? ' <span class="tag">good first choice</span>' : "") + "</div>" +
      '<div class="modelState ' + (ready ? "good" : "") + '">' + escapeHtml(status) + "</div></div>" +
      '<div class="modelMeta">' + meta + "</div>" +
      '<div class="modelMeta">' + escapeHtml(model.note) + "</div>" +
      '<div class="actions">' +
      (ready
        ? '<button class="ghost small" data-model-action="use" data-path="' + escapeHtml(model.path) +
          '">Use this</button>'
        : (engineAbsent
          // A model is hundreds of megabytes and there would be nothing on the
          // phone able to run it, so the honest thing is to refuse.
          ? '<button class="ghost small" disabled>Needs the model engine</button>'
          : '<button class="primary small" data-model-action="download" data-id="' + escapeHtml(model.id) +
            '"' + (downloading ? " disabled" : "") + ">" +
            (partial ? "Resume the download" : "Download " + escapeHtml(model.size)) + "</button>")) +
      "</div></div>";
  }).join("");

  if (data.job) html += jobCardHtml(data.job);

  if (modelState.test) {
    const ok = modelState.test.ok;
    html += '<div class="modelCard">' +
      '<div class="modelRow"><div class="modelName">Test result</div>' +
      '<div class="modelState ' + (ok ? "good" : "bad") + '">' +
      escapeHtml(modelState.test.seconds + "s") + "</div></div>" +
      '<div class="modelMeta">' + escapeHtml(modelState.test.text || "the model said nothing back") + "</div>" +
      '<div class="modelMeta">' + escapeHtml(modelState.test.backend || "") + "</div></div>";
  }
  if (modelState.error) {
    html += '<div class="modelCard"><div class="modelMeta bad">' + escapeHtml(modelState.error) + "</div></div>";
  }
  return html;
}

function renderModels() {
  const box = el("modelBox");
  if (!box) return;
  try {
    box.innerHTML = modelSectionHtml();
  } catch (error) {
    box.innerHTML = '<div class="field hint bad">Could not show the model list (' +
      escapeHtml(error.message) + ").</div>";
  }
}

async function handleModelAction(button) {
  const action = button.dataset.modelAction;
  const restore = button.textContent;
  button.disabled = true;
  const wait = (label) => { button.textContent = label; };
  try {
    if (action === "refresh") {
      wait("Checking...");
      await loadHealth();
      await refreshModels();
    } else if (action === "download") {
      wait("Starting...");
      await api("POST", "/api/model/download", { id: button.dataset.id });
      toast("Downloading - you can close Settings, it keeps going");
    } else if (action === "folder") {
      const input = el("modelsDirInput");
      wait("Saving...");
      const result = await api("POST", "/api/settings", { models_dir: input ? input.value.trim() : "" });
      const saved = (result.settings || {}).models_dir || "";
      toast(saved ? "Models will be saved in " + saved : "Models go back to the default folder");
      await refreshModels();
    } else if (action === "folder-browse") {
      const picked = pickOnAndroid("folder");
      if (picked.error) throw new Error(picked.error);
      const chosen = (picked.paths || [])[0];
      if (!chosen) { toast("No folder chosen"); }
      else {
        const input = el("modelsDirInput");
        if (input) input.value = chosen;
        await api("POST", "/api/settings", { models_dir: chosen });
        toast("Models will be saved in " + chosen);
        await refreshModels();
      }
    } else if (action === "engine") {
      wait("Starting...");
      await api("POST", "/api/engine/install", {});
      toast("Installing the model engine...");
    } else if (action === "use") {
      wait("Switching...");
      await api("POST", "/api/model/select", { path: button.dataset.path });
      toast("AURA will use " + button.dataset.path.split(/[\\/]/).pop());
      await loadHealth();
    } else if (action === "remove") {
      await api("POST", "/api/model/delete", { path: button.dataset.path });
      toast("Removed " + button.dataset.path.split(/[\\/]/).pop());
      await loadHealth();
    } else if (action === "start") {
      wait("Loading...");
      await api("POST", "/api/model/start", {});
      toast("Loading the model - this can take a few seconds");
    } else if (action === "stop") {
      await api("POST", "/api/model/stop", {});
      toast("Local model stopped");
      await loadHealth();
    } else if (action === "cancel") {
      await api("POST", "/api/jobs/" + button.dataset.job + "/cancel", {});
      toast("Cancelling...");
    } else if (action === "test") {
      wait("Asking the model...");
      modelState.test = await api("POST", "/api/model/test", {});
      toast("The model answered in " + modelState.test.seconds + "s");
      await loadHealth();
    }
    await refreshModels();
  } catch (error) {
    modelState.error = friendlyError(error);
    toast(modelState.error, true);
    await refreshModels();
  } finally {
    button.disabled = false;
    button.textContent = restore;
  }
  scheduleModelPoll();
}

/* ----------------------------------------------------------------- settings */
const SETTING_FIELDS = [
  ["top_k", "Passages given to the model", "number"],
  ["chunk_chars", "Passage size (characters)", "number"],
  ["chunk_overlap_chars", "Passage overlap", "number"],
  ["llm_backend", "How answers are written", "select",
   ["auto", "managed", "llamacpp", "server", "extractive"]],
  ["llm_model_path", "Model file AURA is using", "text"],
  ["llm_n_ctx", "Model context size (tokens)", "number"],
  ["llm_threads", "CPU threads for the model (0 = automatic)", "number"],
  ["llm_server_url", "Or an OpenAI-compatible server URL", "text"],
  ["llm_max_tokens", "Max answer tokens", "number"],
  ["llm_temperature", "Temperature", "number"],
  ["embedding_backend", "Semantic search backend", "select", ["auto", "off", "fastembed", "sentence-transformers"]],
  ["embedding_model", "Embedding model", "text"],
];

/* The shell AURA is being used in, plus the two things only the machine
   running it can do: hand the interface to a real browser, or stop AURA. On a
   phone both go through the app's bridge, because a WebView cannot open the
   system browser by itself. */
const SHELL_NAMES = { webview: "native window", app: "app window", browser: "browser tab" };

function windowCardHtml() {
  const address = state.address || {};
  const shell = state.shell || "";
  const android = onAndroid();
  const label = android ? "AURA on this phone" : (SHELL_NAMES[shell] || "in use");
  const phone = !android && address.lan_allowed && address.lan_url ? address.lan_url : "";
  let html = '<div class="modelCard">' +
    '<div class="modelRow"><div class="modelName">' + (android ? "This app" : "This window") + "</div>" +
    '<div class="modelState good">' + escapeHtml(label) + "</div></div>";
  if (address.url && !android) {
    html += '<div class="modelMeta">Running on ' + escapeHtml(address.url) + "</div>";
  }
  if (android) {
    html += '<div class="modelMeta">AURA runs inside this app. Nothing leaves the phone, ' +
      "and it works with no network at all.</div>";
  }
  if (phone) {
    html += '<div class="modelMeta">Phone (same wifi): <code>' + escapeHtml(phone) +
      "</code> - open that in a browser on your phone.</div>";
  }
  if (state.local) {
    html += '<div class="actions">' +
      (android ? "" : '<button class="ghost small" id="openBrowserBtn">Open in my browser</button>') +
      '<button class="ghost small" id="quitBtn">Quit AURA</button></div>';
  } else {
    html += '<div class="modelMeta">Only the machine running AURA can quit it.</div>';
  }
  return html + "</div>";
}

function showStopped() {
  const app = el("app");
  if (app) {
    app.innerHTML = '<div class="stopped"><h1>AURA has stopped</h1>' +
      "<p>Your library and index are safe on disk. Run AURA again to pick up where you left off.</p>" +
      "<p>You can close this window.</p></div>";
  }
}

/* Light, dark, or whatever the machine's own setting is. Kept in the browser
   only - it is a property of this screen, not of the library. */
function appearanceCardHtml() {
  const choice = currentTheme();
  const options = [["auto", "Automatic"], ["light", "Light"], ["dark", "Dark"]];
  return '<div class="modelCard">' +
    '<div class="modelRow"><div class="modelName">Appearance</div></div>' +
    '<div class="segRow" id="themeRow">' +
    options.map(([value, label]) => '<button data-theme-choice="' + value + '"' +
      (choice === value ? ' class="on"' : "") + ">" + label + "</button>").join("") +
    "</div></div>";
}

function settingsFormHtml() {
  const settings = state.settings || {};
  const fields = SETTING_FIELDS.map((field) => {
    const [key, label, type, options] = field;
    const value = settings[key];
    if (type === "select") {
      return '<div class="field"><label>' + label + "</label><select data-key=\"" + key + "\">" +
        options.map((option) => '<option value="' + option + '"' +
          (String(value) === option ? " selected" : "") + ">" + option + "</option>").join("") +
        "</select></div>";
    }
    return '<div class="field"><label>' + label + '</label><input data-key="' + key +
      '" type="' + type + '" value="' + escapeHtml(String(value === undefined ? "" : value)) + '"></div>';
  }).join("");
  if (!fields) return '<div class="field hint">No settings could be built.</div>';
  return appearanceCardHtml() + windowCardHtml() +
    '<div id="modelBox"><div class="field hint">Loading the model list...</div></div>' +
    '<details class="adv"><summary>Retrieval, model size and advanced settings</summary>' +
    '<div class="advBody">' + fields + "</div></details>" +
    '<button class="primary" id="saveSettingsBtn">Save settings</button>' +
    '<div class="field hint">Saved settings apply from your next question - the model box ' +
    "above saves itself as you press its buttons.</div>" +
    '<div class="field"><button class="ghost" id="reingestBtn">Re-read my documents from disk</button>' +
    '<div class="hint">Re-indexes every file in the library with the current version - ' +
    "use it after updating AURA, or if a passage split looks wrong.</div></div>";
}

function openSettings() {
  const body = el("settingsBody");
  if (!body) return;
  try {
    body.innerHTML = settingsFormHtml();
  } catch (error) {
    body.innerHTML = '<div class="field hint">Could not render the settings form (' +
      escapeHtml(error.message) + ").</div>";
  }

  const saveBtn = el("saveSettingsBtn");
  if (saveBtn) saveBtn.onclick = async () => {
    const payload = {};
    body.querySelectorAll("[data-key]").forEach((input) => {
      const key = input.dataset.key;
      payload[key] = input.type === "number" ? Number(input.value) : input.value;
    });
    try {
      const result = await api("POST", "/api/settings", payload);
      state.settings = result.settings || state.settings;
      toast("Settings saved");
      closeSettings();
      await loadHealth();
    } catch (error) { toast(friendlyError(error), true); }
  };
  const browseBtn = el("openBrowserBtn");
  if (browseBtn) browseBtn.onclick = async () => {
    try {
      const result = await api("POST", "/api/open-browser", {});
      toast(result.opened ? "Opened in your browser" : "No default browser was found", !result.opened);
    } catch (error) { toast(friendlyError(error), true); }
  };
  const quitBtn = el("quitBtn");
  if (quitBtn) quitBtn.onclick = async () => {
    quitBtn.disabled = true;
    quitBtn.textContent = "Stopping...";
    try {
      await api("POST", "/api/quit", {});
      showStopped();
    } catch (error) {
      quitBtn.disabled = false;
      quitBtn.textContent = "Quit AURA";
      toast(friendlyError(error), true);
    }
  };
  const reingest = el("reingestBtn");  if (reingest) reingest.onclick = async () => {
    const label = reingest.textContent;
    reingest.disabled = true;
    reingest.textContent = "Re-reading...";
    try {
      const result = await api("POST", "/api/reingest", {});
      const skipped = (result.skipped || []).length;
      toast("Re-read " + (result.reingested || 0) + " document(s)" +
        (skipped ? " - " + skipped + " file(s) could not be found" : ""));
      await loadLibrary();
      await loadHealth();
    } catch (error) {
      toast(friendlyError(error), true);
    } finally {
      reingest.disabled = false;
      reingest.textContent = label;
    }
  };
  const themeRow = el("themeRow");
  if (themeRow) themeRow.onclick = (event) => {
    const button = event.target.closest("[data-theme-choice]");
    if (button) setTheme(button.dataset.themeChoice);
  };
  el("settingsSheet").hidden = false;
  el("scrim").hidden = false;
  refreshModels().then(() => scheduleModelPoll());
}

function closeSettings() {
  stopModelPoll();
  el("settingsSheet").hidden = true;
  if (!document.body.classList.contains("drawerOpen")) el("scrim").hidden = true;
}

/* The library is a drawer over the conversation on a phone, and a column that
   can be put away on a wide screen. One button in the top bar brings it back. */
function openDrawer() {
  document.body.classList.add("drawerOpen");
  el("scrim").hidden = false;
}

function closeDrawer() {
  document.body.classList.remove("drawerOpen");
  if (el("settingsSheet").hidden) el("scrim").hidden = true;
}

/* Growing the box as the question grows, up to a point, is most of what makes a
   composer feel like a composer rather than a form. */
function autoGrow(box) {
  box.style.height = "auto";
  box.style.height = Math.min(box.scrollHeight, 200) + "px";
}

/* -------------------------------------------------------------------- events */
function wire() {
  if (onAndroid()) {
    // A phone has no path to paste and nothing to drag, so those two rows
    // would only be confusing: the pickers below do the same job.
    const pasteRow = el("pathInput") && el("pathInput").parentNode;
    if (pasteRow) pasteRow.hidden = true;
    if (el("dropzone")) el("dropzone").hidden = true;
  }
  el("askBtn").onclick = () => {
    const q = el("questionInput").value;
    el("questionInput").value = "";
    autoGrow(el("questionInput"));
    ask(q);
  };
  el("questionInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      const q = el("questionInput").value;
      el("questionInput").value = "";
      autoGrow(el("questionInput"));
      ask(q);
    }
  });
  el("questionInput").addEventListener("input", () => {
    autoGrow(el("questionInput"));
    updateSend();
  });
  document.querySelectorAll(".chip.example").forEach((chip) => {
    chip.onclick = () => {
      el("questionInput").value = chip.textContent;
      autoGrow(el("questionInput"));
      updateSend();
      el("questionInput").focus();
    };
  });

  el("addPathBtn").onclick = async () => {
    const path = el("pathInput").value.trim();
    if (!path) { toast("Paste a file or folder path first", true); return; }
    const looksLikeFolder = !/\.[a-z0-9]{2,5}$/i.test(path);
    try {
      const payload = await api("POST", "/api/add", looksLikeFolder ? { folder: path } : { path });
      toast("Added " + (payload.count || 0) + " document(s)");
      el("pathInput").value = "";
      await loadLibrary();
    } catch (error) { toast(friendlyError(error), true); }
  };

  el("addFolderBtn").onclick = async () => {
    if (onAndroid()) {
      // A phone has no path to paste, so the system picker does the choosing.
      const picked = pickOnAndroid("folder");
      if (picked.error) { toast(picked.error, true); return; }
      const folder = (picked.paths || [])[0];
      if (!folder) { toast("No folder chosen"); return; }
      try {
        const payload = await api("POST", "/api/add", { folder: folder });
        toast("Added " + (payload.count || 0) + " document(s)");
        await loadLibrary();
      } catch (error) { toast(friendlyError(error), true); }
      return;
    }
    el("pathInput").focus();
    el("pathInput").placeholder = "paste a folder path, then press Add";
  };
  el("pickBtn").onclick = () => el("filePicker").click();
  el("filePicker").onchange = async (event) => {
    const files = Array.from(event.target.files || []);
    await uploadEach(files);
    event.target.value = "";
    await loadLibrary();
    toast("Library updated");
  };

  const dropzone = el("dropzone");
  ["dragenter", "dragover"].forEach((name) => dropzone.addEventListener(name, (event) => {
    event.preventDefault(); dropzone.classList.add("hot");
  }));
  ["dragleave", "drop"].forEach((name) => dropzone.addEventListener(name, () => dropzone.classList.remove("hot")));
  dropzone.addEventListener("drop", async (event) => {
    event.preventDefault();
    const files = Array.from((event.dataTransfer && event.dataTransfer.files) || []);
    await uploadEach(files);
    await loadLibrary();
  });

  el("settingsBtn").onclick = openSettings;
  if (el("settingsBtn2")) el("settingsBtn2").onclick = openSettings;
  el("closeSettingsBtn").onclick = closeSettings;
  el("scrim").onclick = () => { closeSettings(); closeDrawer(); };
  if (el("setupModelChip")) el("setupModelChip").onclick = openSettings;
  el("settingsBody").addEventListener("click", (event) => {
    const button = event.target.closest("[data-model-action]");
    if (!button) return;
    event.preventDefault();
    handleModelAction(button);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    closeDrawer();
    if (!el("settingsSheet").hidden) closeSettings();
  });

  // The library: a drawer over the conversation on a phone, a column that can
  // be put away on a wide screen, and one button either way.
  el("menuBtn").onclick = () => { if (isNarrow()) openDrawer(); else document.body.classList.remove("libraryHidden"); };
  el("closeSidebarBtn").onclick = () => { if (isNarrow()) closeDrawer(); else document.body.classList.add("libraryHidden"); };
  // Choosing something out of the library is usually the end of the errand.
  if (el("pickBtn")) el("pickBtn").addEventListener("click", () => { if (isNarrow()) closeDrawer(); });
  updateSend();
}

async function boot() {
  wire();
  // Nothing on this page should be able to leave a message up for ever. A
  // toast's own timer does the ordinary work; this catches the cases where a
  // long synchronous call (the Android file picker) stops timers from running
  // on time, by which point the bubble is stale news.
  setInterval(() => {
    const now = Date.now();
    for (const node of document.querySelectorAll(".toast")) {
      const expires = Number(node.dataset.expires || 0);
      if (expires && expires <= now) node.remove();
    }
  }, 1000);
  // Automatic means what the machine's own setting says, and that can change
  // while the app is open - at sunset, on a timer.
  if (window.matchMedia) {
    const watcher = window.matchMedia("(prefers-color-scheme: dark)");
    const follow = () => { if (currentTheme() === "auto") applyTheme("auto"); };
    if (watcher.addEventListener) watcher.addEventListener("change", follow);
    else if (watcher.addListener) watcher.addListener(follow);
  }
  try {
    await loadHealth();
    await loadLibrary();
    // A phone starts with the library put away. With nothing in it that is an
    // empty screen and no explanation of how to fill it, so the drawer opens
    // itself the first time - and only while there is nothing in there.
    if (isNarrow() && !state.docs.length) openDrawer();
  } catch (error) {
    const message = friendlyError(error);
    el("statusLine").textContent = "cannot reach the AURA backend";
    toast(message, true);
  }
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", boot);
} else {
  boot();
}
