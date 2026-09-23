#!/usr/bin/env python3
"""AURA desktop entry point.

    AURA.exe                          start the app in its own window
    AURA.exe --shell browser           ... or in your browser instead
    AURA.exe --add notes.pdf          add documents, then start
    AURA.exe --ask "what is ATP?"     answer one question and exit
    AURA.exe --lan                    also let other devices on your wifi connect

The app is a small local server plus a single-page UI, which is what lets the
same interface serve the desktop window, the Android app and any browser on the
network. On the desktop the UI is shown in a native window (`aura/desktop.py`),
falling back to a chromeless browser app window and then to a browser tab, so
there is still no GUI toolkit to install.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

if __package__ in (None, ""):  # running as a script / frozen exe
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from aura import config           # noqa: E402
from aura import desktop          # noqa: E402
from aura.llama_server import backend_for, manager  # noqa: E402
from aura.llm import detect_backend  # noqa: E402
from aura.models import AuraError  # noqa: E402
from aura.server import create_server, lan_address  # noqa: E402
from aura.store import Library     # noqa: E402


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog=config.APP_NAME,
        description="{} - {}".format(config.APP_NAME, config.APP_TAGLINE))
    parser.add_argument("--version", action="version",
                        version="{} {}".format(config.APP_NAME, config.APP_VERSION))
    parser.add_argument("--data-dir", default="", help="where the library and index live")
    parser.add_argument("--host", default="", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=0, help="port (default 8765, auto-advances if busy)")
    parser.add_argument("--lan", action="store_true",
                        help="allow other devices on your network to open this AURA")
    parser.add_argument("--shell", default="auto", choices=["auto", "webview", "app", "browser", "none"],
                        help="how to show the interface: auto (native window), webview, app "
                             "(chromeless browser window), browser, or none")
    parser.add_argument("--console", action="store_true",
                        help="keep the diagnostic console window visible")
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open any window (same as --shell none)")
    parser.add_argument("--add", action="append", default=[], metavar="PATH",
                        help="add a document to the library (repeatable)")
    parser.add_argument("--add-folder", action="append", default=[], metavar="DIR",
                        help="add every supported file in a folder (repeatable)")
    parser.add_argument("--remove", default="", metavar="DOC_ID", help="remove a document id")
    parser.add_argument("--ask", default="", metavar="QUESTION", help="answer one question and exit")
    parser.add_argument("-k", "--top-k", type=int, default=0, help="passages of evidence to use")
    parser.add_argument("--no-dense", action="store_true", help="disable semantic search for this run")
    parser.add_argument("--rebuild", action="store_true", help="rebuild the index before serving")
    parser.add_argument("--list", action="store_true", help="list the library and exit")
    parser.add_argument("--model", default="", metavar="PATH",
                        help="use this .gguf model (see --model-status)")
    parser.add_argument("--model-status", action="store_true",
                        help="show the local model and engine state, then exit")
    parser.add_argument("--install-engine", action="store_true",
                        help="download the llama.cpp engine AURA runs models with, then exit")
    return parser.parse_args(argv)


def engine_line(library: Library) -> str:
    """One honest sentence about what will write the answers."""
    state = manager(library.root).status()
    if state["state"] == "ready":
        return "{} [started on port {}]".format(state["backend"] or state["model_name"], state["port"])
    if state["model_name"] and state["state"] in ("starting", "error"):
        return "{} [{}]".format(state["model_name"], state["detail"])
    backend = detect_backend(library.settings)
    return backend.describe() if backend else "extractive answerer (no local model configured)"


def banner(library: Library, host: str, port: int, allow_lan: bool) -> None:
    stats = library.stats()
    line = "-" * 66
    print(line)
    print("  {} {}  -  {}".format(config.APP_NAME, config.APP_VERSION, config.APP_TAGLINE))
    print(line)
    print("  documents     : {}  ({} passages, {} of text)".format(
        stats["documents"], stats["chunks"], _human(stats["chars"])))
    print("  semantic      : {}".format(stats["dense"]))
    print("  answer engine : {}".format(engine_line(library)))
    print("  library folder: {}".format(stats["root"]))
    print()
    print("  address       : http://{}:{}".format(
        "127.0.0.1" if host in ("0.0.0.0", "") else host, port))
    if allow_lan:
        print("  phone (same wifi) : http://{}:{}".format(lan_address(), port))
        print("                              open that in a browser on your phone")
    else:
        print("  phone access  : off (start with --lan to allow it)")
    print("  stop AURA     : the Quit button in Settings, or Ctrl+C")
    print(line)


def aura_is_serving(port: int, timeout: float = 0.7) -> bool:
    """True when another AURA is already answering on this machine's port.

    AURA keeps serving when its window is closed, so launching the app again
    should show that window rather than start a second copy (or fall over
    because the port is taken).
    """
    try:
        with urllib.request.urlopen("http://127.0.0.1:{}/health".format(int(port)),
                                    timeout=timeout) as reply:
            payload = json.loads(reply.read().decode("utf-8", "replace"))
        return payload.get("app") == config.APP_NAME
    except Exception:  # noqa: BLE001 - anything else means "not ours"
        return False


def stop_engine(library: Library) -> None:
    """Take the local model server down with us instead of leaving it running."""
    try:
        manager(library.root).stop()
    except Exception:  # noqa: BLE001 - shutting down must never raise
        pass


def _human(count: int) -> str:
    value = float(count or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return "{:.0f} {}".format(value, unit) if unit == "B" else "{:.1f} {}".format(value, unit)
        value /= 1024
    return "{} B".format(count)


def bundled_model() -> str:
    """A .gguf packaged inside the executable (see aura.spec)."""
    base = getattr(sys, "_MEIPASS", "")
    if base:
        candidate = Path(base) / "model.gguf"
        if candidate.exists():
            return str(candidate)
    beside = Path(__file__).resolve().parent / "model.gguf"
    return str(beside) if beside.exists() else ""


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        library = Library(root=args.data_dir or None)
    except AuraError as exc:
        print("could not open the library: {}".format(exc))
        return 2

    if args.model:
        chosen = Path(args.model).expanduser()
        if not chosen.exists():
            print("no model file at {}".format(args.model))
            return 2
        library.settings = config.save_settings(
            library.root, dict(library.settings, llm_model_path=str(chosen)))

    if not library.settings.get("llm_model_path"):
        found = bundled_model() or manager(library.root).default_model()
        if found:
            library.settings = config.save_settings(
                library.root, dict(library.settings, llm_model_path=found))

    if args.no_dense:
        library.settings = dict(library.settings, embedding_backend="off")
    if args.rebuild:
        print("rebuilding the index ...")
        library.rebuild()

    for path in args.add:
        try:
            document = library.add_file(path)
            print("added {} ({} passages)".format(document.name, document.pages))
        except AuraError as exc:
            print("skipped {}: {}".format(path, exc))
    for folder in args.add_folder:
        try:
            documents = library.add_folder(folder)
            print("added {} document(s) from {}".format(len(documents), folder))
        except AuraError as exc:
            print("skipped {}: {}".format(folder, exc))

    if args.remove:
        print("removed" if library.remove(args.remove) else "no such document")

    if args.list:
        for item in library.document_list():
            print("{:<36} {:<10} {:>4} pages  {:>4} passages  {}".format(
                item["doc_id"], item["kind"], item["pages"], item["chunks"], item["name"]))
        print("{} document(s), {} passages".format(len(library.documents), len(library.chunks)))
        return 0

    if args.model_status:
        state = manager(library.root).status()
        engine = state["engine"]
        print("model      : {} [{}]".format(state["model_name"] or "none selected", state["state"]))
        print("detail     : {}".format(state["detail"]))
        print("engine     : {}".format(
            "installed ({}) - {}".format(engine.get("tag") or "llama.cpp", engine.get("binary") or "")
            if state["engine_installed"] else
            "not installed - run: AURA --install-engine"))
        print("models dir : {}".format(state["models_dir"]))
        if engine.get("wanted_asset"):
            print("engine for : {} {} ({})".format(state["platform"], state["arch"], engine["wanted_asset"]))
        print("data dir   : {}".format(state["root"]))
        return 0

    if args.install_engine:
        print("downloading the local model engine (about 18 MB) ...")
        try:
            info = manager(library.root).install_runtime()
        except AuraError as exc:
            print("could not install the engine: {}".format(exc))
            return 4
        print("installed {} into {}".format(info.get("tag") or "llama.cpp", info.get("folder")))
        return 0

    if args.ask:
        engine = manager(library.root)
        backend = backend_for(library.root, library.settings)
        if backend is None and engine.model_path and engine.detail:
            print("(the local model was not used: {})".format(engine.detail))
        answer = library.ask(args.ask, k=args.top_k or None, backend=backend)
        print(answer.text)
        print()
        print("mode: {} | citations: {}".format(answer.mode, ", ".join(answer.citations) or "none"))
        for hit in answer.hits:
            print("  [{}] {} p. {} (score {:.2f})".format(
                hit.label, hit.chunk.doc_name, hit.chunk.page, hit.bm25))
        return 0

    host = args.host or ("0.0.0.0" if (args.lan or library.settings.get("allow_lan")) else
                         str(library.settings.get("host") or "127.0.0.1"))
    allow_lan = bool(args.lan or host == "0.0.0.0" or library.settings.get("allow_lan"))
    port = args.port or int(library.settings.get("port") or 8765)
    shell = "none" if args.no_browser else args.shell

    if aura_is_serving(port):
        print("AURA is already running on port {} - showing that window.".format(port))
        url = "http://127.0.0.1:{}".format(port)
        if shell != "none":
            desktop.open_window(url, prefer=shell, profile_dir=library.root / "browser-window")
        print("  (quit it from Settings, or close the native window)")
        return 0

    server = None
    for attempt in range(12):
        try:
            server = create_server(library, host=host, port=port + attempt, shell=shell)
            break
        except OSError as exc:
            if attempt == 0:
                print("port {} is busy ({})".format(port, exc))
    if server is None:
        desktop.show_message("AURA could not start",
                            "No free port between {} and {} could be used.".format(port, port + 11))
        return 3
    bound_port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    banner(library, host, bound_port, allow_lan)

    url = "http://127.0.0.1:{}".format(bound_port)
    result = {"shell": shell, "blocking": False, "detail": ""}
    console_hidden = {"value": False}
    if shell != "none":
        def note_shell(chosen: str) -> None:
            server.api.shell = chosen
            # Hide the black window *before* the native window blocks, never
            # after: once open_window returns, the window has been closed.
            if chosen == "webview" and not args.console:
                console_hidden["value"] = desktop.hide_console()

        result = desktop.open_window(
            url, prefer=shell, profile_dir=library.root / "browser-window", on_shell=note_shell)
        print("  window        : {}".format(desktop.describe(result)))
        if result.get("shell") == "none":
            desktop.show_message(
                "AURA has no window",
                "The interface is up but could not be shown ({}).\n\n"
                "Open {}\n\nYou can also start AURA with --shell browser.".format(
                    result.get("detail") or "unknown reason", url))
        elif result.get("shell") == "webview":
            print("  closing the AURA window stops AURA"
                  + (" (console hidden - start with --console to see it)"
                     if console_hidden["value"] else ""))
        else:
            if console_hidden["value"]:
                desktop.show_console()  # the console is the way out of this mode
            print("  AURA keeps running if you close that window - quit it from Settings")

    try:
        while thread.is_alive():
            time.sleep(0.4)
    except KeyboardInterrupt:
        print("\nstopping AURA ...")
    stop_engine(library)
    server.shutdown()
    return 0


def _guarded_main(argv=None) -> int:
    try:
        return main(argv)
    except KeyboardInterrupt:
        print("\nstopping AURA ...")
        return 0
    except Exception:  # noqa: BLE001 - never die silently with a hidden console
        trace = traceback.format_exc()
        print(trace)
        desktop.show_message("AURA stopped with an error", trace.strip()[-1200:])
        return 1


if __name__ == "__main__":
    sys.exit(_guarded_main())
