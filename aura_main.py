#!/usr/bin/env python3
"""AURA desktop entry point.

    AURA.exe                          start the app and open the browser
    AURA.exe --add notes.pdf          add documents, then start
    AURA.exe --ask "what is ATP?"     answer one question and exit
    AURA.exe --lan                    also let your phone connect (AURA Pocket)

The app is a small local server plus a single-page UI, so there is no GUI
toolkit to install and the same interface serves the desktop browser and the
Android companion app.
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
import webbrowser
from pathlib import Path

if __package__ in (None, ""):  # running as a script / frozen exe
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from aura import config           # noqa: E402
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
                        help="allow other devices on your network (the AURA Pocket app) to connect")
    parser.add_argument("--no-browser", action="store_true", help="do not open a browser window")
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
    print("  open this in your browser : http://{}:{}".format(
        "127.0.0.1" if host in ("0.0.0.0", "") else host, port))
    if allow_lan:
        print("  phone (same wifi)         : http://{}:{}".format(lan_address(), port))
        print("                              paste that into AURA Pocket")
    else:
        print("  phone access              : off (start with --lan to allow it)")
    print()
    print("  press Ctrl+C to stop")
    print(line)


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

    server = None
    for attempt in range(12):
        try:
            server = create_server(library, host=host, port=port + attempt)
            break
        except OSError as exc:
            if attempt == 0:
                print("port {} is busy ({})".format(port, exc))
    if server is None:
        print("could not find a free port")
        return 3
    bound_port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    banner(library, host, bound_port, allow_lan)

    url = "http://127.0.0.1:{}".format(bound_port)
    if not args.no_browser:
        threading.Thread(target=lambda: (time.sleep(0.6), webbrowser.open(url)), daemon=True).start()

    try:
        while thread.is_alive():
            time.sleep(0.4)
    except KeyboardInterrupt:
        print("\nstopping AURA ...")
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
