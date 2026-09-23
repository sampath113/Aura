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
      which also shows the address and the same-wifi URL.
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

## 4. AURA on Android - **BUILT** (2026-09-23)

**Requested, verbatim:**

> why is the built app only 13.7kb.. port the windows app which we currently build
> to android exactly as it is but made for mobile aspect ratios and the same mobile
> selection but should as for permission to access files and a path to store the
> downloaded models

The 13.7 KB was the old *Android debug APK*: a one-screen connection dialog that
pointed a WebView at a desktop AURA on the same wifi. It is gone. The
phone app is now **AURA itself** - the same Python, the same retrieval, the same
interface, the same model list - running on the device.

- [x] **`aura/host.py`** - the one place that asks what machine this is (the Android
      flag, the app's paths, the "all files" grant, the native library directory).
      Every answer can be overridden from the environment, which is what makes a
      phone testable with no phone present. Nothing else in `aura/` tests for Android.
- [x] **`aura_mobile.py`** - the Android entry point: takes the app's paths as one
      JSON blob, writes the first-run settings (models folder, thread count), serves
      the interface on `127.0.0.1`, unloads the model 25 s after the app leaves the
      screen, and answers the app's `foreground`/`background`/`shutdown` calls.
- [x] **Chaquopy inside the APK** - `android/app/build.gradle` runs CPython in the
      app, from `android/app/src/main/python/aura` (a byte-identical copy of `aura/`,
      kept equal by `tools/sync_aura.py` and enforced by `AndroidMirrorTests`), and
      installs the same three optional packages `requirements.txt` lists. Each has a
      built-in fallback in `aura/ingest.py`, so a build machine that cannot supply
      them loses nothing but the better readers.
- [x] **The local model, on the phone** - the llama.cpp Android engine is unpacked
      into the APK's native libraries at build time (`prepareAuraEngine`), renamed to
      `libllama-server-bin.so` because Android only extracts `lib*.so` from there,
      and stripped (230 MB -> ~20 MB). It is bundled rather than downloaded because
      Android 10+ will not execute a file the app wrote itself. If the build machine
      cannot reach GitHub, the APK still builds and says on screen that it has no
      engine.
- [x] **Files** - `MANAGE_EXTERNAL_STORAGE` (plus `READ_EXTERNAL_STORAGE` for older
      Android), a first-run screen and a Settings nudge that offer the tap that grants
      it, the system picker for "add files"/"add a folder", and a copy-into-the-app
      fallback for a picked file with no real path.
- [x] **A path for downloaded models** - `models_dir` is a real setting, defaulting to
      `<shared storage>/models`, validated server-side (`POST /api/settings` refuses a
      folder it cannot write to) and changeable from the model card with a native
      folder picker. The model list, the download job and the progress bar are the
      desktop ones.
- [x] **Mobile layout** - a phone block in `webui/style.css` (bigger touch targets,
      full-width sheets, stacked rows), the interface says "on this phone" instead of
      "this machine", the paste-a-path row and the drop zone are hidden where there is
      no filesystem to drop onto, and Quit/Open-in-browser behave as they can.
- [x] **Lifecycle** - the model is unloaded when the app goes to the background, the
      server stops when it is closed, and a leftover engine from a run Android killed
      is reaped at the next start (`_reap_orphans`, which kills only this app's own
      engine).
- [x] **Tests** - 164 in the suite, including the Android paths, the bundled-engine
      report (present / half-copied / absent), the reap rules, the first-run defaults,
      `/api/host`, the models-folder validation, the build script's engine list, the
      `@Field` rule below, and the mirror. See `android/README.md` for how to build it.

**The first build, and what it taught us (2026-09-23):** the user pushed the port to
GitHub and ran the Android build. It failed after 47 s, before compiling anything:

```
A problem occurred evaluating project ':app'.
> Could not get unknown property 'supportedPython' for project ':app'
```

Cause: a **method** in a Groovy build script cannot see a plain `def` declared at
script level - it is a local of the script's `run()`, not a property. So
`detectBuildPython()` could not read `supportedPython` (and `stripEngine()` could not
read `engineBinary`) - `engineBinary` had not failed *yet* only because that method is
not called until the task runs. Fixed by making both `@Field`s
(`import groovy.transform.Field`), widening the Python probe (3.9–3.13, trying
`python3.13` down to `python3.9`, resolving an absolute path for Chaquopy), and adding
`AURA_NO_PYTHON_PACKAGES=1` as a documented escape hatch for the pip step.
`test_the_values_a_groovy_method_reads_are_fields` guards the rule now. Everything
else in the build had already checked out: AGP 8.6.1 + Chaquopy 16.1.0 resolved from
Maven Central, and the wheel indexes list arm64 `lxml`, `Pillow` and `pypdf` for
3.9–3.13. The second build had not been run yet at the time of writing.

**Still open, and honest about it:**

- [ ] **No device has run this yet.** The layout, the permission flow and the engine
      launch are all reasoned and tested as far as they can be without hardware; a
      first build on a real phone is the next step, and `android/README.md` lists
      exactly what to check.
- [ ] 64-bit only (`arm64-v8a`). A 32-bit phone would need the armeabi-v7a engine and
      a second ABI in `abiFilters`.
- [ ] Executing the engine from the native library directory is the standard trick for
      this, but it is the one thing a strict device or SELinux policy can refuse; the
      log tail is shown in the model card when it does.
- [ ] An in-process backend (llama.cpp through JNI) would remove the child process and
      the exec-from-lib question entirely. Much bigger job; not needed unless a device
      turns out to refuse the current one.

## 5. Smaller items

- [ ] Definition bias in `rank_sentences` (`aura/retrieve.py`): for "what is X"
      questions, prefer sentences where a query term is followed by
      *is / are / means / refers to*. Today a question quoted in the student's
      notes ranks the same as a definition. Offered, not built.
- [ ] The document list's passage count and the header pill come from two
      different endpoints; a snapshot taken mid-ingest can show them disagreeing
      for one refresh. Re-read once after an ingest settles.
- [x] Android was a client of the desktop engine; it is now the same app with the
      engine bundled into the APK (see section 4).
