"use strict";

const state = {
  server: localStorage.getItem("aura.server") || "",
  docs: [],
  stats: {},
  settings: {},
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
      .replace(/_([^_]+)_/g, "<em>$1</em>")
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
  el("backendPill").textContent = health.backend || "extractive";
  el("backendPill").className = "pill" + (/extractive/.test(health.backend || "") ? "" : " good");
  el("statusLine").textContent = health.app + " " + health.version + " - " +
    (health.backend || "extractive") + " - " + (state.stats.root || "");
  renderStats();
}

/* ---------------------------------------------------------------------- ask */
function addMessage(role, html, meta) {
  const empty = el("emptyState");
  if (empty) empty.remove();
  const node = document.createElement("div");
  node.className = "msg " + role;
  node.innerHTML = '<div class="who">' + (role === "user" ? "You" : "AURA") + "</div>" +
    '<div class="bubble">' + html + "</div>" + (meta ? '<div class="meta">' + meta + "</div>" : "");
  el("chatScroll").appendChild(node);
  el("chatScroll").scrollTop = el("chatScroll").scrollHeight;
  return node;
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
    '<span class="dot"></span> searching ' + (state.stats.chunks || 0) + " passages</span>");
  let seconds = 0;
  const ticker = setInterval(() => {
    seconds += 1;
    const bubble = pending.querySelector(".bubble");
    if (bubble) bubble.lastChild.textContent = " searching and writing (" + seconds + "s)";
  }, 1000);
  try {
    const payload = await api("POST", "/api/ask", { question });
    clearInterval(ticker);
    const checks = payload.checks || {};
    const metaBits = ["mode: " + (payload.mode || "extractive"),
                      "citations: " + ((payload.citations || []).length)];
    if (checks.coverage !== undefined) metaBits.push("citation coverage: " + Math.round(checks.coverage * 100) + "%");
    const warnings = (checks.notes || []).map((note) => '<span class="warn">' + escapeHtml(note) + "</span>");
    pending.querySelector(".bubble").innerHTML = renderRich(payload.text || "");
    pending.querySelector(".meta").innerHTML = metaBits.map(escapeHtml).join(" &middot; ") +
      (warnings.length ? " &middot; " + warnings.join(" &middot; ") : "");
    const sources = document.createElement("div");
    sources.className = "msg aura";
    sources.innerHTML = sourcesHtml(payload.hits);
    pending.appendChild(sources);
    wireCitations(pending);
    state.answers.push(payload);
  } catch (error) {
    clearInterval(ticker);
    pending.querySelector(".bubble").innerHTML =
      '<span class="warn">' + escapeHtml(error.message) + "</span>";
    pending.querySelector(".meta").textContent = "";
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

/* ----------------------------------------------------------------- settings */
const SETTING_FIELDS = [
  ["top_k", "Passages given to the model", "number"],
  ["chunk_chars", "Passage size (characters)", "number"],
  ["chunk_overlap_chars", "Passage overlap", "number"],
  ["llm_backend", "Language model backend", "select", ["auto", "llamacpp", "server", "extractive"]],
  ["llm_model_path", "Path to a local .gguf model", "text"],
  ["llm_server_url", "OpenAI-compatible server URL", "text"],
  ["llm_max_tokens", "Max answer tokens", "number"],
  ["llm_temperature", "Temperature", "number"],
  ["embedding_backend", "Semantic search backend", "select", ["auto", "off", "fastembed", "sentence-transformers"]],
  ["embedding_model", "Embedding model", "text"],
];

function openSettings() {
  const body = el("settingsBody");
  body.innerHTML = SETTING_FIELDS.map((field) => {
    const [key, label, type, options] = field;
    const value = state.settings[key];
    if (type === "select") {
      return '<div class="field"><label>' + label + "</label><select data-key=\"" + key + "\">" +
        options.map((option) => '<option value="' + option + '"' +
          (String(value) === option ? " selected" : "") + ">" + option + "</option>").join("") +
        "</select></div>";
    }
    return '<div class="field"><label>' + label + '</label><input data-key="' + key +
      '" type="' + type + '" value="' + escapeHtml(String(value === undefined ? "" : value)) + '"></div>';
  }).join("") +
    '<button class="primary" id="saveSettingsBtn">Save settings</button>' +
    '<div class="field hint">A .gguf model path needs llama-cpp-python installed. ' +
    "Without a model AURA still answers by extracting sentences from your own sources.</div>";

  el("saveSettingsBtn").onclick = async () => {
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
  el("settingsSheet").hidden = false;
  el("scrim").hidden = false;
}

function closeSettings() {
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
