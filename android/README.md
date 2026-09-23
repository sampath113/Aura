# AURA for Android

This is **the same AURA**, not a companion app. The desktop build and this one run
the same Python program, the same retrieval pipeline and the same interface; what
differs is only what a phone forces to differ: where files may live, how the engine
is delivered, and how the user picks their documents.

```
        ┌────────────────────────── the APK ──────────────────────────┐
        │                                                             │
        │  MainActivity.java   a WebView, a setup screen, a picker    │
        │        │                                                    │
        │        │ starts, by name                                   │
        │        ▼                                                    │
        │  aura_mobile.py      ← the Android entry point              │
        │        │                                                    │
        │        ▼                                                    │
        │  aura/ …             ← the desktop code, copied verbatim    │
        │        │                                                    │
        │        │ serves 127.0.0.1:<port>                            │
        │        ▼                                                    │
        │  WebView shows it → the same UI as the desktop              │
        │                                                             │
        │  lib/arm64-v8a/libllama-server-bin.so + its .so files       │
        │        ← the local model engine, unpacked at build time      │
        └─────────────────────────────────────────────────────────────┘
```

There is one interface, one library format, one answer pipeline, and one copy of
`aura/` — kept identical by `tools/sync_aura.py` and enforced by
`tests/test_aura.py::AndroidMirrorTests`.

---

## Building it

The build server does this for you (see the build console and `src/README.md`); to
do it by hand you need JDK 17, the Android SDK (`platforms;android-34`,
`build-tools;34.0.0`) and Gradle 8.7+:

```bash
cd aura-app/android
echo "sdk.dir=/path/to/android-sdk" > local.properties
gradle assembleDebug          # a debug APK, installable straight away
gradle assembleRelease        # the one to ship (sign it before installing)
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

**Build the release variant when it matters.** A debug APK is slower, larger, and
carries the debug flag; the release APK the build console produces is signed with
the build server's generated keystore (keep `aura-release.jks` and
`aura-release.password` — updates must be signed with the same key).

What the build does, in order:

1. **Chaquopy 16.1.0** brings CPython into the APK. It is on Maven Central, so no
   extra repository is needed. It needs `minSdk >= 24` (this app uses 26) and an
   explicit `abiFilters`.
2. **A Python for the build machine.** Chaquopy's pip install and bytecode steps
   insist the build machine's Python has the same major.minor as the app's. Rather
   than hard-code one, `app/build.gradle` probes `python3`/`python` and accepts
   3.8–3.13 (`-PbuildPython=/usr/bin/python3.12` overrides it).
3. **The pip packages** — `pypdf`, `python-docx`, `Pillow`: the three optional
   packages `requirements.txt` lists for the desktop. Every one of them has a
   fallback inside `aura/ingest.py` (a built-in PDF text scan and a DOCX reader), so
   if a build machine cannot supply them, comment the line out in
   `app/build.gradle`: AURA still reads everything and says in the document's notes
   which reader it used.
4. **The engine** — `prepareAuraEngine` downloads the pinned llama.cpp Android
   release, unpacks it into the APK's native libraries, and strips it. See below.
5. **Everything else** is `MainActivity.java` and three small Java files.

---

## The model engine: why it is inside the APK

**Android 10 and later refuse to execute a file an app wrote into its own storage.**
That single rule is why the desktop design (download `llama-server` into the data
folder and run it) cannot work here: on a phone the executable has to arrive *with*
the app, in the app's **native library directory** — the one place an app is allowed
to execute from.

So `prepareAuraEngine` (in `app/build.gradle`):

* downloads `llama-b11136-bin-android-arm64.tar.gz` from the llama.cpp GitHub release
  (pinned, because a phone cannot ask GitHub for the newest one at run time);
* copies the launcher and its shared objects into `build/engine/jniLibs/arm64-v8a/`,
  renaming `llama-server` to **`libllama-server-bin.so`** — Android only puts files
  named `lib*.so` into the native library directory, so the name is the trick that
  gets an executable there;
* strips the debug information out of them. It is most of the download: the engine
  as published is about 230 MB, about 20 MB once stripped. `stripEngine` tries
  `llvm-strip`, `aarch64-linux-gnu-strip`, `strip`, and the NDK's `llvm-strip` in
  turn, works on copies, and gives up quietly — an unstripped engine still runs, the
  APK is just a lot bigger.
* sets `packaging { jniLibs { useLegacyPackaging = true } }`, which is **required**:
  it stores the native libraries uncompressed *and* extracts them on install, so
  `getApplicationInfo().nativeLibraryDir` really contains an executable file. Without
  it the directory is empty of anything you can `exec`, and the cost of having it is
  a larger APK on disk.

**Nothing about the engine is fatal.** If the build machine is offline, or GitHub is
unreachable, the APK is still produced: AURA indexes, searches and answers with
quoted passages, and the model card says *this copy of AURA was built without the
local model engine*. The names in `engineLibs` must stay in step with
`ENGINE_BUNDLE_FILES` in `aura/catalog.py` — `AndroidMirrorTests` fails if they
drift.

At run time the engine needs a little help, and `aura/llama_server.py` gives it:
`native_lib_dir` arrives from Java, `LD_LIBRARY_PATH` and `GGML_BACKEND_PATH` point
at that directory (`_child_env`), there is no working directory (`_child_cwd`), and
a leftover engine from a previous run is killed first (`_reap_orphans` — Android
kills a backgrounded app without reaching its children).

---

## Reading the user's files

AURA's whole point is that it can be asked about the documents the student already
has. Two things follow.

**The permission.** `AndroidManifest.xml` asks for `MANAGE_EXTERNAL_STORAGE` (the
"all files" access a file manager asks for) plus `READ_EXTERNAL_STORAGE` with
`maxSdkVersion="32"` for older releases. The first-run screen and Settings both offer
the one tap that grants it. The app is **sideloaded**, which is what makes that
permission available — Google Play restricts it to file managers and similar, so this
app is not a Play candidate as it stands. Without the grant AURA still works: files
picked through the system picker are copied into `<filesDir>/imports`, so they stay
readable afterwards, and the interface says plainly which case it is in.

**Where things live.** All of it comes from Java, in one JSON blob, because Java is
the only side that knows what Android actually allows (`aura/host.py` is the one
place any of it is read):

| What | Path |
|---|---|
| library, index, settings | `<filesDir>/library` |
| shared storage the user can see | `/storage/emulated/0/AURA` (else the app's external dir, else private) |
| **downloaded models (default)** | `<shared storage>/models` — a *setting*, editable, with a native folder picker |
| the engine | `getApplicationInfo().nativeLibraryDir` |
| the interface | `aura/webui/`, extracted by Chaquopy next to `server.py` |

A phone has nothing to paste a path into, so "add files" and "add a folder" go
through the system picker, which JavaScript reaches through the WebView bridge
(`window.AndroidAura.pick(kind)`, implemented by `MainActivity.pickSync`). A folder
with no real path — a cloud provider, the Downloads provider — is reported rather
than silently ignored; a *file* with no real path is copied into the app instead, so
"add this file" always works. That logic is `Pickers.java`, deliberately free of the
activity so the path arithmetic can be reasoned about on its own.

---

## The files

| File | What it is |
|---|---|
| `app/build.gradle` | Chaquopy, the ABIs, the packaging flag, and `prepareAuraEngine` |
| `app/src/main/AndroidManifest.xml` | the permissions, `PyApplication`, `MainActivity` |
| `app/src/main/java/org/aura/app/MainActivity.java` | the three screens (setup / loading / error), the WebView, the lifecycle |
| `app/src/main/java/org/aura/app/Host.java` | the two thin wires: Python → app, JavaScript → picker |
| `app/src/main/java/org/aura/app/Pickers.java` | the system pickers, URI → path arithmetic, copying a file that has no path, and `hasFileAccess` |
| `app/src/main/python/aura_mobile.py` | the Android entry point (absolute imports: Java loads it by name) |
| `app/src/main/python/aura/` | **a byte-identical copy of `../aura/`** |
| `tools/sync_aura.py` | keeps that copy identical (`--check` reports drift) |
| `app/src/main/res/` | the launcher icon and the dark theme, nothing else |
| `gradle.properties`, `settings.gradle`, `build.gradle` | the project (AGP 8.6.1 + Chaquopy 16.1.0) |

No AndroidX and no XML layouts: the screens are built in Java, so the whole app is
four Java files, one manifest and one icon, and every line can be read in one sitting.

---

## Lifecycle, and the things a phone does to you

* **Backgrounded** → `aura_mobile.background()` arms a 25 s timer that unloads the
  model; coming back cancels it, so switching apps does not pay for a reload.
* **Closed** → the Quit button (`POST /api/quit` → Python stops the engine, then the
  server returns → `Host.onStopped()` → `finishAndRemoveTask()`).
* **Killed by Android** → the next launch calls `_reap_orphans` (`/proc` scan) to stop
  the engine the kill left behind, and finds the port again if the preferred one is
  taken (`_bind` falls back to port 0).
* **No network use at all** beyond the model downloads the user asks for: the server
  binds `127.0.0.1` only, and the WebView talks to it over loopback.

### Known limits — check these on a real device

1. **Executing the engine from the native library directory** is the standard trick
   for this shape of problem, and it is what the whole design rests on. Some devices
   and some SELinux policies are stricter than others; if the engine will not start on
   a particular phone, llama-server's own error is in
   `<data>/library/logs/llama-server.log` and the model card shows its tail. That is
   the first place to look.
2. **64-bit only** (`arm64-v8a`). A 32-bit phone would need a different engine build;
   add `armeabi-v7a` to `abiFilters` only if such a phone matters.
3. The APK is big — the engine (~20 MB stripped) plus CPython and the native
   libraries, stored uncompressed so they can be executed. Hundreds of thousands of
   bytes of that is unavoidable; if it ever needs to shrink, the lever is dropping
   ABI-agnostic ggml backends or splitting per-ABI APKs.
4. **Nothing here has been run on a phone from inside this workspace.** The Python
   side is covered by the test suite and the mirror test, and the Gradle project is
   laid out to the letter of Chaquopy's documentation, but a first build on a real
   device is the moment of truth: check that the app reaches the interface, that
   "Allow access to my files" opens the right system page, that adding a folder from
   Download works, and that Settings → Local model can download and then run a model.

---

## Debugging

```bash
adb logcat -s AURA            # MainActivity's own lines, plus the page's console
adb shell run-as org.aura.app ls files/library          # the library, on a device
adb shell run-as org.aura.app cat files/library/logs/llama-server.log
```

The WebView's `console.log` is forwarded to `Log.i("AURA", ...)`, so anything the
interface logs shows up in `adb logcat -s AURA`. A failure to start is shown on
screen by design (`aura_mobile.report` → `Host.onError`), never only in the log.
