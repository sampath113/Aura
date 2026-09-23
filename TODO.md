# AURA - backlog

Requested by the user on 2026-09-23, verbatim:

> add that later we have to choose a model than give it to aura an then use it to search the documents locally.. big work

Follow-up, same day: *"i dont have any model yet..."* - which is exactly the
point: nothing has to be prepared, AURA fetches what it needs.

---

## 1. Local model, end to end - **BUILT** (2026-09-23)

**What the user wanted:** pick a model, hand it to AURA, and have AURA use it to
work through their own documents, locally. No cloud, no API key, no homework.

**Where AURA stood, and still stands,** on the two things that are easy to
confuse:

| Layer | How it works now | Needs a model? |
|---|---|---|
| **Finding** the passages (`aura/index.py`, `aura/retrieve.py`) | BM25 over the student's own text, pure Python, instant. Optional meaning-based search via `aura/dense.py` when `fastembed` is installed | no / a small *embedding* model for the semantic part |
| **Writing** the answer (`aura/answer.py`, `aura/llama_server.py`) | quotes the best sentences; with a model running it hands the same passages over and writes prose | yes - this is the `.gguf` part |

### What was built (Phase A)

- [x] **`aura/catalog.py`** - the models AURA can fetch (exact byte size + sha256
      pinned per file, RAM guide, licence, a note on who it suits), plus the
      per-platform pattern for the official llama.cpp release. Verified against
      the Hugging Face API and the GitHub releases API on 2026-09-23.
- [x] **`aura/downloads.py`** - resumable (`.part` + `Range`), progress-reporting,
      cancellable, checksum-verifying downloads, and safe archive extraction.
- [x] **`aura/llama_server.py`** - downloads and runs llama.cpp's prebuilt
      `llama-server` as a child process on a free localhost port, waits for
      `/health`, reads its log back on failure, and stops it at exit. One
      `Manager` per data folder, so the process is shared.
- [x] **`aura/jobs.py`** - background jobs with progress for the UI to poll.
- [x] **Settings -> Local model** in `webui/` - status, engine install, model
      download with a progress bar, "Use this", Remove, Start/Stop, "Test the
      model", and a friendly error card with the log tail when it will not run.
- [x] **The API**: `GET /api/models`, `GET /api/jobs[/{id}]`,
      `POST /api/model/{download,select,delete,start,stop,test}`,
      `POST /api/engine/install`, `POST /api/jobs/{id}/cancel`.
- [x] **The CLI**: `--model`, `--model-status`, `--install-engine`; `--ask` now
      uses the local model when there is one.
- [x] **Tests**: 107 passing (39 in `test_aura.py`, 13+ in `test_server.py`),
      including resume/cancel/checksum/truncation, archive traversal refusal,
      job outcomes, argv building, engine discovery/install, a dead child
      process, deleting the selected model, and a whole download-as-a-job cycle.

**Design decision worth keeping:** the default engine is the *prebuilt
`llama-server` binary*, not `llama-cpp-python`. Nothing is compiled, nothing is
bundled into the executable, the model stays a file the user can see and delete,
and the model speaks the OpenAI-compatible API AURA already supported for Ollama
and friends. `llama-cpp-python` is still supported as the alternate `llamacpp`
backend.

### Phase B - ship a model with the installer (still open)

- [ ] Build console (`index.html`): add the model fields it already sends in its
      contract (model URL, sha256, filename) with an obvious "no model" default.
- [ ] Build server (Colab notebook): when `bundle_model` is set, download the
      model on the build machine, verify `model_sha256`, and place it **beside**
      the artifact (a zip folder next to the exe, never inside it).
- [ ] App side: already done - `bundled_model()` in `aura_main.py` finds
      `model.gguf` next to the executable and uses it.

**Packaging reality:** a one-file exe that swallows a 2-4 GB `.gguf` unpacks to a
temp dir on every launch, so a bundled model means "AURA.exe + model.gguf + the
engine folder in one zip", which is also what `bundled_model()` expects. With the
managed engine in place this is now a convenience, not a requirement - a fresh
install can fetch everything itself.

### Phase C - make the model answers better (still open)

- [ ] Stream the answer: `LlamaCppBackend.stream()` exists and is unused; the
      managed server also streams. Add `POST /api/ask?stream=1` (chunked
      transfer) and append into the bubble as it arrives.
- [ ] Context budgeting: `top_k` passages can exceed `llm_n_ctx`. Truncate the
      evidence to a token budget (a chars-per-token estimate is fine) and say so
      in the meta line instead of letting llama.cpp silently drop the tail.
- [ ] Model tunables in the UI: context size, threads, max tokens, temperature -
      they are settings already, but they are buried in the advanced section.
- [ ] A "quoted / written" toggle on each answer so the difference is obvious.

### Honest limits (tell the user, never hide)

- A 7B Q4 model wants ~8 GB of RAM; the 1.5B default wants ~2.5 GB.
- The first question after a cold start waits for the model to load (a few
  seconds; the answer's notes say when AURA started it).
- The engine and the model are downloads, so the very first setup needs internet.
  After that AURA never needs it again.
- None of this improves *retrieval*: if the answer is not in the library, a
  bigger model only makes the wrong answer sound better.

---

## 2. Smaller items

## 2. The desktop window - **BUILT** (2026-09-23)

**Requested, verbatim:**

> instead of opening in the default brower i want the ai to open in the app
> itself with professional gui... is that possible?

Yes - and it did not need a GUI toolkit, because AURA already owns the UI. The
window is now `aura/desktop.py`:

- [x] **`webview`** - pywebview driving the OS webview (WebView2 / WKWebView /
      WebKitGTK): a real window, 1240x820, text selection on, closing it quits
      AURA, and the console window is hidden first (only when this process owns
      it - `desktop.hide_console`).
- [x] **`app`** - Edge/Chrome/Brave/Vivaldi started with `--app=` and a private
      profile under `<data dir>/browser-window`: chromeless, no tabs, no address
      bar. The fallback when pywebview is not installed (the shipped exe relies
      on it until the build bundles pywebview) or WebView2 is absent.
- [x] **`browser`** - the old default-browser behaviour, kept as the last resort.
- [x] `--shell auto|webview|app|browser|none`, `--console`; `--no-browser` kept.
- [x] `POST /api/quit` and `POST /api/open-browser` (loopback-only), surfaced as
      **Quit AURA** and **Open in my browser** in Settings -> *This window*,
      which also shows the address and the AURA Pocket URL.
- [x] Single instance: launching AURA again opens the window of the AURA that is
      already serving (`aura_is_serving`).
- [x] An icon: `aura.ico` (16-256px, embedded by the spec) and
      `aura/webui/icon.png` as the favicon / window icon.

**Open:**

- [ ] Remember the window size and position between runs (`webui/` cannot
      persist it; pywebview would need to report the geometry back to the app).
- [ ] A real installer: Start-menu shortcut, uninstaller, and a file
      association so double-clicking an `.aura` library file opens AURA. Out of
      scope for the build console as it stands.

## 3. Queued from the user (2026-09-23, not built yet)

- [ ] **Sources as a dropdown.** `sourcesHtml()` already renders a `<details>`,
      but with the `open` attribute, so every passage is expanded. Make it start
      closed and put the count in the summary ("Sources used by this answer (6)").
- [ ] **Longer answers.** Two causes: (a) the model the user picked is Qwen2.5
      **0.5B**, the weakest in the catalogue - the UI should nudge towards 1.5B+
      when there is enough RAM; (b) `GROUNDING_SYSTEM` rule 5 tells the model to
      answer "in 2-6 sentences". Offer a "short / full" answer-length setting
      rather than just loosening the prompt.
- [ ] **A misleading engine warning.** The user saw "qwen2.5-...gguf is
      installed, but the local model engine is not. Install it from Settings."
      as a red toast while the model was demonstrably running (the header showed
      the model and its port, and the status line said "local model ready"), and
      Settings now says the engine *is* installed. `Manager.ensure()` writes
      that exact sentence, and the toast comes from a failing Settings action
      (`handleModelAction`) whose message is
      `Manager.test()`'s `AuraError(self.detail)` - i.e. **a stored `detail`
      can be shown as a live error**. Two things to fix: (a) `ensure()` should
      re-derive the reason it is returning None instead of leaving an old
      `detail` in place, and (b) `test()`/`_prepare_model()` should never
      surface a `detail` that the engine check does not confirm right now.
      Worth asking the user which button produced it (Test the model is the
      likeliest) and whether the engine had just been installed when it
      appeared.

## 4. Smaller items

- [ ] Definition bias in `rank_sentences` (`aura/retrieve.py`): for "what is X"
      questions, prefer sentences where a query term is followed by
      *is / are / means / refers to*. Today a question quoted in the student's
      notes ranks the same as a definition. Offered, not built.
- [ ] The document list's passage count and the header pill come from two
      different endpoints; a snapshot taken mid-ingest can show them disagreeing
      for one refresh. Re-read once after an ingest settles.
- [ ] Android stays a client of the desktop engine. An on-device model is a
      separate project (and the managed-engine design would not port to it
      unchanged).
