# AURA - Agentic Multimodal Research Assistant

An offline-first study assistant. Add your PDFs, lecture notes, spreadsheets and
photos of the whiteboard; AURA indexes them **on your own machine** and answers
questions with citations back to the exact page they came from:

> **Q:** where do the light reactions happen?
> **A:** The light reactions occur in the thylakoid membrane and produce ATP and
> NADPH. [S1: bio.pdf, p. 7]

No account, no API key, no upload. The only thing that ever leaves the machine
is nothing.

---

## Why it is built this way

| Decision | Reason |
|---|---|
| Pure-Python BM25 index | always available, instant, no model download |
| Optional dense embeddings | semantic search when `fastembed` is installed, silently skipped otherwise |
| A **local model you download from inside the app** | written answers instead of quoted sentences - AURA fetches the model file *and* the llama.cpp engine it runs on, so nothing has to be prepared beforehand |
| **Its own window**, not a browser tab | `aura/desktop.py` drives the webview the OS already has (WebView2 / WKWebView / WebKitGTK); if that is unavailable it falls back to a chromeless browser app window, then to a browser tab |
| Local web UI instead of Tkinter/Qt | no GUI toolkit to install, identical UI on desktop and phone, and it is testable headlessly |
| Every answer validated against its evidence | a research assistant that invents references is worse than useless |

The pipeline is `ingest -> chunk (page-aware) -> BM25 (+dense) -> RRF + MMR -> answer -> verify citations`.

## Layout

```
aura_main.py            desktop entry point (the build server looks for this name)
aura.spec               PyInstaller recipe; ships aura/webui inside the executable
aura.ico                the Windows icon (16-256px), embedded by aura.spec
requirements.txt        the only hard dependencies (all prebuilt wheels)
requirements-llm.txt    optional: local .gguf models
requirements-embeddings.txt
aura/
  config.py             defaults, settings file, data directory
  models.py             Page / Document / Chunk / Hit / Answer dataclasses
  text.py               tokenising, sentence splitting, stemming, keyphrases
  chunk.py              page-aware chunking with sentence overlap
  index.py              BM25 + reciprocal rank fusion
  dense.py              optional semantic embeddings (never imported eagerly)
  retrieve.py           hybrid search + MMR, sentence ranking, "closest" fallback
  citations.py          [S1: bio.pdf, p. 7] formatting, parsing, validation
  answer.py             grounded prompt, extractive/outline/closest answers, verification
  llm.py                llama.cpp / OpenAI-compatible / none
  llama_server.py       runs the local model AURA downloaded (llama-server child process)
  catalog.py            the models AURA can fetch + the llama.cpp release it needs
  downloads.py          resumable, checksum-verified downloads; archive extraction
  jobs.py               background jobs with progress, for the UI to poll
  desktop.py            how the interface is shown: native window -> app window -> browser
  ingest.py             PDF, DOCX, text, Markdown, CSV/TSV, images
  store.py              the Library: persistence + orchestration
  server.py             local HTTP API + static UI host
  webui/                the single-page interface (served by server.py):
                        index.html, style.css, app.js, icon.png
android/                the Android app - the same AURA, as an APK (Gradle project)
  README.md             how the phone build works, and what a device can do to it
  app/src/main/python/  a byte-identical copy of aura/, kept in step by tools/sync_aura.py
  app/src/main/java/    the WebView, the system file picker, the permission
tests/test_aura.py      dependency-free test suite
tests/test_server.py    the HTTP layer, through a fake socket
```

`aura/` has **no `__init__.py` on purpose**: Python 3 namespace packages do not
need one, and the editor this project is maintained in cannot store files whose
name starts with an underscore. Do not "fix" it by adding one; `import aura`
already works, as does PyInstaller.

There is also no `.gitignore` in this folder for the same reason. If you want
one, create it in the repo root with:

```
__pycache__/
*.pyc
aura_data/
dist/
build/
*.spec.bak
```

## Run it

```bash
python aura_main.py                     # start AURA in its own window
python aura_main.py --shell browser     # ... or in your browser, the old way
python aura_main.py --shell none        # just serve (for a phone-only setup)
python aura_main.py --console           # keep the diagnostic console window
python aura_main.py --add notes.pdf     # add a document first
python aura_main.py --add-folder ./sem5 # ingest a whole folder
python aura_main.py --list              # what is in the library
python aura_main.py --ask "what is ATP?" # answer one question and exit
python aura_main.py --lan               # also let your phone connect
python aura_main.py --no-dense --rebuild
python aura_main.py --model-status      # what model and engine AURA would use
python aura_main.py --install-engine    # fetch the llama.cpp engine, then exit
python aura_main.py --model C:\models\qwen2.5-1.5b-instruct-q4_k_m.gguf
```

The library lives in `~/.aura` (override with `--data-dir` or `AURA_DATA_DIR`).

## The window

AURA serves a web UI, so *showing* it is a choice, and `aura/desktop.py` makes
that choice in this order:

1. **`webview` - a real window.** [pywebview](https://pywebview.flowrl.com)
   wraps the webview the operating system already has: WebView2 on Windows,
   WKWebView on macOS, WebKitGTK on Linux. No browser, no tabs, no address bar,
   ~1240x820, and closing it stops AURA. On Windows the app hides its own
   console window first (`desktop.hide_console` only touches a console this
   process owns, so running from a terminal keeps that terminal).
2. **`app` - an app window.** Edge/Chrome/Brave/Vivaldi started with `--app=`
   and a private profile under `<data dir>/browser-window`: a chromeless window
   that looks like an application. Used when pywebview is missing or its
   runtime is not installed, which is also the case for a build where
   `pywebview` was left out of `requirements.txt`.
3. **`browser` - a browser tab.** The old behaviour, kept as the last resort so
   the app can always be reached.

`--shell` forces one of these (`auto` is the ladder above); `--no-browser` is
still accepted and means `--shell none`. Whatever happens is printed in the
banner and shown in Settings under *This window*, which is also where **Open in
my browser** and **Quit AURA** live (both refuse to act for a client that is not
this machine). Closing a native window quits; an app window or a browser tab
leaves AURA serving, so `POST /api/quit` exists for that case and the console
window stays open as the way out.

Starting AURA again while it is already running does not start a second copy -
it looks for an AURA on the port (`aura_main.aura_is_serving`) and re-opens the
window it finds.

The window icon is `aura.ico` (16-256px, embedded by `aura.spec` on Windows) and
`aura/webui/icon.png`, which the UI links as its favicon - that is also what an
app-mode browser window shows in the taskbar.

## Giving AURA a model

You do not have to find or install anything yourself. In the app: **Settings ->
Local model**, press **Install the engine** (18 MB, llama.cpp's own prebuilt
`llama-server`), then **Download** one of the listed models and press **Test the
model**. AURA fetches both files into its data folder, starts the model, and
from then on writes the answers itself from the passages it retrieved.

```
~/.aura/models/      the .gguf files            (1 GB and up - pick by RAM)
~/.aura/runtime/     the llama.cpp engine       (18 MB, one folder per build)
~/.aura/logs/        llama-server.log           (why a model refused to start)
```

The model list is in `aura/catalog.py`, and every file in it is pinned to an
exact byte count and sha256, so a truncated download is caught instead of
quietly producing nonsense later. What the list is *for* is honest sizing:

| Model | Download | Wants | Notes |
|---|---|---|---|
| Qwen2.5 0.5B Instruct | 469 MB | ~1.5 GB RAM | the smallest sensible one |
| **Qwen2.5 1.5B Instruct** | **1.0 GB** | **~2.5 GB RAM** | **the recommended default** |
| Gemma 2 2B Instruct | 1.6 GB | ~3 GB RAM | short answers |
| Llama 3.2 3B Instruct | 1.9 GB | ~4 GB RAM | best at following the citation rules |
| Qwen2.5 3B Instruct | 2.0 GB | ~4 GB RAM | a step up from the 1.5B |
| Phi-3.5 Mini Instruct | 2.3 GB | ~5 GB RAM | strong at technical explanations |
| Qwen2.5 7B Instruct | 4.5 GB (2 files) | ~8 GB RAM | clearly the best writing, the slowest |

Three ways to run a model, in the order `llm_backend: auto` tries them:

1. **`managed`** (default) - the `llama-server` above. `AURA` starts it on a free
   localhost port when a question arrives, and stops it when the app exits.
2. **`llamacpp`** - a `.gguf` loaded in-process through the optional
   `llama-cpp-python` package (`pip install -r requirements-llm.txt`).
3. **`server`** - any OpenAI-compatible endpoint you run yourself (Ollama,
   LM Studio): put its URL in `llm_server_url`. Nothing else changes.

Set `llm_backend: extractive` to switch models off entirely.

The engine and the model are ordinary files in the data folder, so they can be
copied to another machine, replaced by hand, or deleted. The managed server is
also what makes the shipped executable small: nothing is compiled at build
time, and `AURA.exe` stays a Python program plus its own code.

**When a model will not run you are told why**, in the same place you asked for
it: the Settings box shows `could not start` with the reason, an answer falls
back to quoted sentences and says so in its notes, and the last lines of
`llama-server.log` are shown under the model card.

### Security note for the maintainer

Model and engine downloads go to Hugging Face and GitHub (the llama.cpp
releases API lists the newest build; a pinned build is the fallback when the
API cannot be reached). Nothing about your library or your questions is ever
uploaded - the model runs locally and the only traffic is the download itself.

## What an answer looks like without a model

Every answer is built from the student's own passages, and each mode is chosen
automatically - the mode is shown in the UI under the answer:

| Mode | When | What you get |
|---|---|---|
| `extractive` | the question shares content words with your material | the best-matching sentences, each cited with page and file |
| `outline` | "summarise this", "key definitions", "what is this about" | a cited outline, one line per section of your material |
| `closest` | nothing in the library matches the question | the nearest passages anyway, clearly labelled as not a direct match, plus a hint to rephrase |
| `no-evidence` | the library is empty | a prompt to add a document |

With a local model running (see *Giving AURA a model*) the same evidence is
handed to it instead and the answer comes back as prose - the mode line then
reads `written by your local model from your sources`, and every citation label
the model invents is stripped before you see it.

## The interface

`aura/webui/` is one page, served by `server.py` on 127.0.0.1 and shown by both
the desktop window and the Android WebView, so it is the whole of AURA's looks. It
is deliberately the layout people already know from ChatGPT: a library down the
left, one column of conversation down the middle, and a rounded box at the bottom
to type into.

* **The conversation is the page.** A question is a rounded block on the right; an
  answer is plain text in the column, with nothing under it but the answer itself.
  The answer mode, the number of passages and the passages themselves live in a
  **Sources** box under the answer which starts **closed** - the reader asked a
  question, not for a bibliography. Tapping a `[S1]` chip in the answer opens that
  box and scrolls to the passage, which is what a citation has to do to be worth
  showing. The mode is only spelled out there when it is *not* the ordinary "quoted
  from your pages" case.
* **The library is a column on a wide screen and a drawer on a phone**, opened by
  the button in the top bar and closed by the one in its own header or by the
  scrim. On a phone whose library is empty the drawer opens itself once, because
  the alternative first screen is empty with no explanation of how to fill it.
* **Light and dark.** The theme is decided by a few lines in `index.html`'s head
  (before the first paint, so a dark phone never flashes white), kept in
  `localStorage` under `aura.theme`, and offered in Settings as
  Automatic / Light / Dark. It belongs to the screen, not to the library, so it is
  never sent to the server.
* **One column width** (`--thread`, 768px) is what makes it read as a conversation
  rather than as a dashboard. The composer sits at the bottom of that column and
  the send button is dead until there is something to send.

**The bug this replaced is worth remembering, because this kind of layout fails
silently.** The old layout was `grid-template-columns: 320px 1fr` with a `.solo`
variant of `0 1fr` for when the library was hidden - and hiding the library also
took it out of the grid, so the conversation became the *first* grid child and was
placed in the **0px** column. On a phone that rendered the entire chat 1.6 pixels
wide (it is the sliver down the left edge of the screenshot the user sent). Nothing
about it was a JavaScript error, so no test noticed: **do not go back to hiding one
grid item to make room for another.** The drawer is `position: fixed` and the
column is not a grid at all.

## Recent fixes (do not regress these)

* `webui/style.css` needs `[hidden] { display: none !important; }`: the settings
  sheet is `display:flex`, and an author rule beats the browser's default
  `[hidden]` rule, so without it the panel is stuck open on every page load.
* `webui/app.js` `addMessage()` always renders the `.meta` element: the ask flow
  used to write into a `.meta` node that was only created when a message passed
  one, so every answer ended in "Cannot set properties of null".
* `chunk.py` carries trailing sentences into the next passage - the previous
  guard (`or not carry`) broke out of the carry loop on its first iteration, so
  passage overlap silently never happened.
* A page made of unpunctuated text is hard-split on spaces, so one giant
  passage cannot swallow a whole document.
* `Library.ask()` falls back to `Retriever.fallback()` instead of returning
  nothing when the query words are absent from the index.
* Settings has a **Re-read my documents from disk** button (`POST /api/reingest`)
  because `rebuild()` only re-scores the passages already stored - an existing
  library needs a re-ingest to pick up a changed chunker.
* The local model is a **child process AURA starts itself**, and it is never left
  running: `Manager.stop()` runs on exit (`atexit` in `aura/llama_server.py`),
  and a process whose `poll()` returns is treated as dead, not as a backend.
* A model that cannot run must **say so where the user is looking**: the engine
  goes to `state: "error"` with a sentence naming what to install, the answer's
  notes carry that sentence, and the UI shows the tail of `llama-server.log`.
  Do not turn this into a silent fall back to quoting.
* Deleting the model that is currently selected clears `llm_model_path` and stops
  the server - otherwise AURA points at a file that is no longer there.
* `aura/downloads.py` refuses a file whose byte count or sha256 does not match
  the catalogue, and deletes the `.part`, so a truncated model can never be
  loaded as if it were fine.
* **Nothing the interface needs may be fatal to the window.** `desktop.open_window`
  tries webview -> app window -> browser and returns which one worked; a missing
  pywebview, a missing WebView2 runtime or a browser that will not start is a
  line in `problems`, never an exception. Do not make the window a hard
  requirement, and do not raise from `open_window`.
* `desktop.py` imports `ctypes`, `winreg` and `shutil.which` **inside** the
  functions that need them, and takes `platform_name` / `environ` / `which` /
  `exists` / `spawn` as arguments: that is what lets the whole ladder be tested
  headlessly (and under a wasm interpreter, which has no `ctypes`).
* `desktop.hide_console()` must only hide a console this process owns
  (`GetConsoleProcessList`), otherwise running `AURA.exe` from a terminal makes
  the user's terminal disappear.
* Quitting over HTTP (`POST /api/quit`) and opening the real browser
  (`POST /api/open-browser`) are **loopback-only**; a phone on the same wifi may
  ask for anything else but not those two.
* **The Android copy of `aura/` must stay identical** to this one
  (`python android/tools/sync_aura.py`); `AndroidMirrorTests` fails otherwise, and a
  phone quietly running older code is the one bug nobody would notice from here.
* **`abiFilters` and the engine go together**: the engine names in
  `android/app/build.gradle` (`engineLibs`) must match `ENGINE_BUNDLE_FILES` in
  `aura/catalog.py` (a test enforces it), and `useLegacyPackaging = true` is what
  makes the native library directory contain a file that can actually be executed.
  Turn it off and the engine silently stops existing on the device.
* On Android the engine is **bundled, not downloaded** (`host.is_android()`), so
  nothing may assume it can be installed later: `install_runtime()` refuses, the
  model card hides the button, and the "engine missing" sentence must not tell a
  phone user to look for a Settings button that cannot exist.
* **A build that lost the engine is not a build.** The engine is fetched, size- and
  sha256-checked, and unpacked *during* the build, so the failure that matters is not
  "the fetch failed" - it is "the fetch failed and an APK shipped anyway", which looks
  like a working app until a model is needed. `prepareAuraEngine` therefore **fails the
  build** unless `-PauraEngineOptional=true` was asked for, and after assembling, the
  APK is opened and `lib/arm64-v8a/libllama-server-bin.so` looked for inside it
  (`apkCarriesEngine`). `android/README.md` has the full reasoning; the pinned archive
  size and sha256 live in `build.gradle`, with a mirrored copy of the archive as a
  second source so a build machine that cannot reach GitHub still gets an engine.
* **A screen that says something is wrong must say what to do about it.** The model
  card prints `status()["detail"]` beside "could not start": a state of error with an
  empty detail is the one thing it must never publish, so `Manager.status()` fills the
  reason in (naming the missing engine file when there is one). And a build with no
  engine in it is *not* "could not start" - it gets its own state, its own sentence
  ("no engine in this build - install a build of AURA that includes the engine"), and
  **no buttons that cannot work**: no Load, no Test, no model download, just Check
  again. Saying "could not start" there sends the reader hunting for a fault in their
  own settings.
* **A message on screen must not be able to outlive its welcome.** Toasts each carry
  their own expiry (`dataset.expires`), remove every older bubble when they appear, and
  are swept by a one-second interval in `boot()`; the upload notice is *sticky* and is
  taken down in a `finally`. The version that shared one `toastTimer` left the earlier
  bubble on screen for ever - the "Reading <file>..." that was welded to the bottom of
  the window - because the next toast cleared its timeout before it fired. Nothing
  that leaves a stale message on screen is acceptable here.
* **A control that cannot be pressed must look that way.** `button:disabled` is styled
  once, at the top of the button rules, so a disabled *ghost* button (a model download
  that has to wait for the engine, say) is visibly dead. Styling only `button.primary`
  - which is what this used to do - is how a dead button ends up looking live.
* **A kill does not reach the engine.** Android kills a backgrounded app, and
  `llama-server` survives it, so the next start reaps it (`_reap_orphans`) — and
  only it: another app's copy of the same binary, or any process that merely
  mentions it, is left alone.

## AURA on Android

`android/` is a Gradle project that builds **AURA itself** for a phone - the same
program, the same interface, the same library, answering on the device with no
desktop anywhere. It is not a remote control for the desktop app; it is the app.

* **Chaquopy** puts CPython inside the APK and runs `aura/` from
  `android/app/src/main/python/` (a byte-identical copy of `aura/` — `sync_aura.py`
  keeps it that way and a test refuses to let them drift apart).
* AURA's own server then serves the same interface on `127.0.0.1`, and the app's
  WebView shows it. So there is still only one interface to maintain.
* The **local model works on the phone**: llama.cpp's Android engine is unpacked
  into the APK's native libraries at build time (Android 10+ will not execute a file
  an app downloaded into its own storage), and models are downloaded from inside the
  app into a folder the user picks — the same model list, the same Settings card.
* **Files:** the app asks for the "all files" access a file manager asks for, so
  AURA can be asked about the user's own PDFs and folders; a file picked from a
  provider with no real path is copied into the app so it stays readable. Settings
  shows where models go and offers a native folder picker to change it.

Details, including the parts a real device can still surprise you with, are in
`android/README.md`. The desktop app can also serve a phone on the same wifi with
`--lan`, which is useful for a quick look but is not what the APK is for.

## Building the installers

The repo is laid out for the AURA build console (`perchance.org/aura-build-console`)
and its Colab build server:

* the desktop entry point is `aura_main.py` (one of the names the server probes for);
* `requirements.txt` at the repo root is installed into the interpreter that freezes the app
  (that is how `pywebview`, and so the native window, reaches the packager);
* any directory holding `settings.gradle` is treated as the Android project - here `android/`;
* `aura.spec` is picked up automatically, which is what bundles `aura/webui` into `AURA.exe`,
  collects pywebview and its WebView2 bindings when pywebview is installed, and embeds `aura.ico`.

The Android target builds `android/` with Gradle 8.7 + AGP 8.6.1 + Chaquopy 16.1.0
(all resolvable from Maven Central), a Python 3.8–3.13 on the build machine for
Chaquopy's pip step, and llama.cpp's Android engine downloaded into the APK. It is
the one target with a **release variant worth asking for** — a debug APK is slower
and larger — and the one whose failure modes are worth reading about in
`android/README.md` before believing a green build.

The spec treats pywebview as optional on purpose: `Analysis` is built from
`hiddenimports`/`datas` that are only added when `import webview` succeeds, so a
build on a machine without it still produces a working AURA - it just opens an
app window instead of a native one. If a future pywebview release ever breaks the
freeze, delete the `pywebview` line from `requirements.txt` and rebuild; nothing
else has to change.

`python -m unittest discover -s tests -t .` runs the suite.

## Optional extras

```bash
pip install -r requirements-llm.txt          # only for the alternate `llamacpp` backend
pip install -r requirements-embeddings.txt   # semantic search (downloads a small model)
```

OCR for images needs a system binary as well as the Python glue:

```bash
pip install pytesseract          # plus: apt install tesseract-ocr / the Windows installer
```

Without an OCR engine, images are still catalogued and cited as figures - they
just do not contribute text.

## Roadmap

`TODO.md` is the backlog, and its headline item - **choose a model, hand it to
AURA, and let it work through the student's own documents locally** - is built:
the model chooser, the engine fetcher, the managed `llama-server`, and the
download/verify job all exist (see *Giving AURA a model*). What is left is listed
there: shipping a model *inside* the installer, streaming the model's answer into
the UI as it is written, budgeting the context window against `llm_n_ctx`, and
the small definition-bias tweak to sentence ranking.

## Honest limitations

* Non-paginated formats (DOCX, TXT, CSV) cite *section numbers*, not printed page
  numbers, because those formats have no pages.
* Scanned PDFs need OCR; the ingest step says so explicitly instead of silently
  indexing an empty page.
* Dense retrieval is opt-in, and so is a local model - but only in the sense
  that you press a button: AURA downloads the model (about 1 GB) and the engine
  it runs on. Downloading needs internet; answering with the model never does.
* A model still only writes from the passages retrieval found. If the answer is
  not in your library, a bigger model just makes the wrong answer sound more
  convincing, which is why the citation check stays on either way.
* With no model at all, answers are quoted sentences rather than prose, and a
  question your material does not cover gets the closest passages plus a note
  saying so - it never invents an answer, so "no direct match" is a real outcome.
* On a phone the engine that runs the model arrives **inside the APK** (Android
  will not execute a file the app downloaded itself), so the APK is tens of
  megabytes and it is built for 64-bit devices only (`arm64-v8a`). A build that
  cannot bundle the engine no longer produces an APK: it stops with the reason in
  the log, because an engine-less APK is a quoter that looks like a working app until
  somebody tries to load a model. `-PauraEngineOptional=true` builds that version on
  purpose, and then the app says plainly that it was made without the engine.
* Executing the engine from the app's native library directory is the standard
  answer to that Android rule, but it is the one thing here that a strict device
  or SELinux policy can refuse; `android/README.md` says where to look when that
  happens.
* The "all files" permission the phone app asks for is the one Google Play
  restricts to file managers, so the APK is for sideloading. AURA also works
  without the grant: files added through the picker are copied into the app.
