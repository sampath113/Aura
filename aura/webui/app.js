"use strict";

const state = {
  server: localStorage.getItem("aura.server") || "",
  docs: [],
  stats: {},
  settings: {},
  model: {},
  answers: [],
  busy: false,
  libraryHidden: false,
};

const el = (id) => document.getElementById(id);

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

/* -------------------------------------------------------------------- toast */
let toastTimer = null;
function toast(message, bad) {
  const node = document.createElement("div");
  node.className = "toast" + (bad ? " bad" : "");
  node.textContent = message;
  document.body.appendChild(node);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => node.remove(), 5200);
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

async function loadHealth() {
  const health = await api("GET", "/health");
  state.settings = health.settings || {};
  state.stats = (health.stats || {});
  state.model = health.model || {};
  const backend = health.backend || "extractive";
  el("backendPill").textContent = backend;
  el("backendPill").className = "pill" + (/extractive/.test(backend) ? "" : " good");
  el("backendPill").title = state.model.detail || backend;
  el("backendPill").onclick = openSettings;
  const modelBit = state.model.state === "ready" ? " - local model ready"
    : (state.model.state === "starting" ? " - local model loading"
      : (state.model.state === "error" ? " - local model problem" : ""));
  el("statusLine").textContent = health.app + " " + health.version + " - " + backend + modelBit +
    " - " + (state.stats.root || "");
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

function addMessage(role, html, meta) {
  const empty = el("emptyState");
  if (empty) empty.remove();
  const node = document.createElement("div");
  node.className = "msg " + role;
  node.innerHTML = '<div class="who">' + (role === "user" ? "You" : "AURA") + "</div>" +
    '<div class="bubble">' + html + '</div><div class="meta">' + (meta || "") + "</div>";
  el("chatScroll").appendChild(node);
  el("chatScroll").scrollTop = el("chatScroll").scrollHeight;
  return node;
}

function setBubble(node, html) {
  const bubble = node && node.querySelector(".bubble");
  if (bubble) bubble.innerHTML = html;
}

function setMeta(node, html) {
  const meta = node && node.querySelector(".meta");
  if (meta) meta.innerHTML = html || "";
}

function sourcesHtml(hits) {
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
  return '<details class="sources" open><summary>Sources used by this answer</summary>' + items + "</details>";
}

async function ask(question) {
  if (state.busy) return;
  question = (question || "").trim();
  if (!question) { toast("Type a question first", true); return; }
  if (!state.docs.length) { toast("Add a document to your library first", true); return; }
  state.busy = true;
  el("askBtn").disabled = true;
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
    const metaBits = ["mode: " + modeLabel(mode),
                      "citations: " + ((payload.citations || []).length)];
    if (checks.coverage !== undefined && mode !== "extractive" && mode !== "closest"
        && mode !== "outline") {
      metaBits.push("citation coverage: " + Math.round(checks.coverage * 100) + "%");
    }
    const warnings = (checks.notes || [])
      .filter((note) => !(mode === "no-evidence" && /nothing matched/i.test(note)))
      .map((note) => '<span class="warn">' + escapeHtml(note) + "</span>");
    if (checks.fallback) warnings.unshift('<span class="warn">nearest passages, not a direct match</span>');
    setBubble(pending, renderRich(payload.text || ""));
    setMeta(pending, metaBits.map(escapeHtml).join(" &middot; ") +
      (warnings.length ? " &middot; " + warnings.join(" &middot; ") : ""));
    const sources = sourcesHtml(payload.hits);
    if (sources) {
      const node = document.createElement("div");
      node.className = "msg aura";
      node.innerHTML = sources;
      pending.appendChild(node);
    }
    wireCitations(pending);
    state.answers.push(payload);
  } catch (error) {
    clearInterval(ticker);
    setBubble(pending, '<span class="warn">' + escapeHtml(friendlyError(error)) + "</span>");
    setMeta(pending, "");
  } finally {
    state.busy = false;
    el("askBtn").disabled = false;
    el("chatScroll").scrollTop = el("chatScroll").scrollHeight;
  }
}

function wireCitations(scope) {
  scope.querySelectorAll(".cite").forEach((chip) => {
    chip.onclick = () => {
      const target = scope.querySelector('.src[data-label="' + chip.dataset.label + '"]');
      if (!target) return;
      target.classList.add("highlight");
      target.scrollIntoView({ behavior: "smooth", block: "center" });
      setTimeout(() => target.classList.remove("highlight"), 2200);
    };
  });
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

  const tone = running ? "good" : (engine.state === "error" ? "bad" : "warn");
  const headline = running ? "ready"
    : (engine.state === "starting" ? "loading..."
      : (engine.state === "error" ? "could not start"
        : (selected ? "not running" : "no model chosen yet")));
  html += '<div class="modelCard">' +
    '<div class="modelRow"><div class="modelName">Local model</div>' +
    '<div class="modelState ' + tone + '">' + escapeHtml(headline) + "</div></div>" +
    '<div class="modelMeta">' + escapeHtml(engine.detail || "") + "</div>" +
    (running ? '<div class="modelMeta">AURA writes its answers with this model, using the ' +
      "passages it found in your own documents.</div>" : "") +
    (running && engine.model_name
      ? '<div class="modelMeta">' + escapeHtml(engine.model_name + " on " + engine.url) + "</div>" : "") +
    (engine.log_tail ? '<pre class="modelLog">' + escapeHtml(engine.log_tail) + "</pre>" : "") +
    '<div class="actions">' +
    (running ? '<button class="ghost small" data-model-action="stop">Stop the model</button>' : "") +
    (selected && !running ? '<button class="ghost small" data-model-action="start">Load the model</button>' : "") +
    (selected ? '<button class="ghost small" data-model-action="test"' + (downloading ? " disabled" : "") +
      ">Test the model</button>" : "") +
    "</div></div>";

  html += '<div class="modelCard">' +
    '<div class="modelRow"><div class="modelName">Model engine</div>' +
    '<div class="modelState ' + (engine.engine_installed ? "good" : "warn") + '">' +
    (engine.engine_installed ? "installed" : "not installed") + "</div></div>" +
    '<div class="modelMeta">' + (engine.engine_installed
      ? escapeHtml("llama.cpp " + (eng.tag || "build") + " for " + (engine.platform || "") + " " +
                   (engine.arch || "") + " (" + (eng.size || "") + " in " + (eng.folder || "") + ")")
      : escapeHtml("AURA needs llama.cpp's llama-server to run a model. It is a " +
                   (eng.download_mb || 18) + " MB download - no compiler, no extra software.")) + "</div>" +
    '<div class="actions"><button class="ghost small" data-model-action="engine"' +
    (downloading ? " disabled" : "") + ">" +
    (engine.engine_installed ? "Reinstall the engine" : "Install the engine") + "</button></div></div>";

  if (installed.length) {
    html += '<div class="sectionTitle">Models on this machine</div>';
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
        : '<button class="primary small" data-model-action="download" data-id="' + escapeHtml(model.id) +
          '"' + (downloading ? " disabled" : "") + ">" +
          (partial ? "Resume the download" : "Download " + escapeHtml(model.size)) + "</button>") +
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
    if (action === "download") {
      wait("Starting...");
      await api("POST", "/api/model/download", { id: button.dataset.id });
      toast("Downloading - you can close Settings, it keeps going");
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
  return '<div id="modelBox"><div class="field hint">Loading the model list...</div></div>' +
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
  const reingest = el("reingestBtn");
  if (reingest) reingest.onclick = async () => {
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
  el("settingsSheet").hidden = false;
  el("scrim").hidden = false;
  refreshModels().then(() => scheduleModelPoll());
}

function closeSettings() {
  stopModelPoll();
  el("settingsSheet").hidden = true;
  el("scrim").hidden = true;
}

/* -------------------------------------------------------------------- events */
function wire() {
  el("askBtn").onclick = () => { const q = el("questionInput").value; el("questionInput").value = ""; ask(q); };
  el("questionInput").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      const q = el("questionInput").value;
      el("questionInput").value = "";
      ask(q);
    }
  });
  document.querySelectorAll(".chip.example").forEach((chip) => {
    chip.onclick = () => { el("questionInput").value = chip.textContent; el("questionInput").focus(); };
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

  el("addFolderBtn").onclick = () => { el("pathInput").focus(); el("pathInput").placeholder = "paste a folder path, then press Add"; };
  el("pickBtn").onclick = () => el("filePicker").click();
  el("filePicker").onchange = async (event) => {
    const files = Array.from(event.target.files || []);
    for (const file of files) {
      try { toast("Reading " + file.name + "..."); await uploadFile(file); }
      catch (error) { toast(file.name + " - " + friendlyError(error), true); }
    }
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
    for (const file of files) {
      try { toast("Reading " + file.name + "..."); await uploadFile(file); }
      catch (error) { toast(file.name + " - " + friendlyError(error), true); }
    }
    await loadLibrary();
  });

  el("settingsBtn").onclick = openSettings;
  el("closeSettingsBtn").onclick = closeSettings;
  el("scrim").onclick = closeSettings;
  if (el("setupModelChip")) el("setupModelChip").onclick = openSettings;
  el("settingsBody").addEventListener("click", (event) => {
    const button = event.target.closest("[data-model-action]");
    if (!button) return;
    event.preventDefault();
    handleModelAction(button);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !el("settingsSheet").hidden) closeSettings();
  });
  el("collapseBtn").onclick = () => {
    state.libraryHidden = !state.libraryHidden;
    document.querySelector(".layout").classList.toggle("solo", state.libraryHidden);
    el("collapseBtn").innerHTML = state.libraryHidden ? "&#9654;" : "&#9664;";
  };
}

async function boot() {
  wire();
  try {
    await loadHealth();
    await loadLibrary();
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
