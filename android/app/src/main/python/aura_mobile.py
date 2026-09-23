"""Starting AURA inside the Android app.

`aura_main.py` is the desktop entry point: it chooses a window, opens it, and
serves the same interface inside it. A phone has no window to choose - the app
*is* the window - so this module is its Android twin:

* every path arrives from the Java layer, because Java is the only side that
  knows where Android actually lets an app write, and whether the user has
  granted access to their own files. `main()` is handed that as one JSON blob;
* AURA's own HTTP server still listens on 127.0.0.1 only, and the app's WebView
  shows it, so there is exactly one interface to maintain and nothing is
  exposed to the network;
* the model is unloaded when the app goes to the background - a phone will kill
  an app that keeps a gigabyte of weights resident while the user is elsewhere;
* the system file picker lives in Java and is reached from the interface through
  the WebView's JavaScript bridge, not through here.

Nothing imports this on a desktop build (`aura_main.py` never mentions it), so
the code that runs on both platforms stays free of phone-specific branches: what
differs is the paths, the engine that ships inside the app, and the picker.

The imports below are absolute (`from aura import ...`) rather than relative
(`from . import ...`) because the Java layer starts AURA by name - it asks for
the module `aura_mobile`, which makes this a top-level module with no package to
be relative to. `aura_main.py` reads the same way for the same reason.
"""
from __future__ import annotations

import json
import os
import threading
import traceback
from pathlib import Path
from typing import Dict, Optional

from aura import config, host, server
from aura.llama_server import manager
from aura.store import Library

#: A phone has four fast cores and four slow ones, and llama.cpp's default is
#: "all of them", which is measurably slower on a big.LITTLE chip. This is only
#: the starting value - it is an ordinary setting, editable in Settings.
PERFORMANCE_CORE_CAP = 4

#: How long AURA waits, after the app is backgrounded, before unloading the
#: model. Long enough that switching apps and coming back does not pay for a
#: reload; short enough that a loaded model is not resident all afternoon.
BACKGROUND_GRACE_S = 25.0

HOST_CLASS = "org.aura.app.Host"

#: Remembered so the app's own calls (`background`, `shutdown`) can find what
#: `main` started. One Android process serves one AURA at a time.
_LIVE: Dict[str, object] = {}


# ------------------------------------------------------------------ the app
def _java_host():
    """The Java `Host` class, or None when this is not an Android process."""
    try:
        from java import jclass
    except Exception:  # noqa: BLE001 - no Chaquopy means no phone
        return None
    try:
        return jclass(HOST_CLASS)
    except Exception:  # noqa: BLE001 - the class is part of the app, so this is a bug
        return None


def notify(method: str, *args) -> bool:
    """Tell the app something, without ever taking AURA down if it cannot.

    The app uses these calls to swap its loading screen for the interface, or
    to show a real error instead of a spinner that never stops.
    """
    target = _java_host()
    if target is None:
        return False
    try:
        getattr(target, method)(*args)
        return True
    except Exception:  # noqa: BLE001 - a courtesy call must never raise
        return False


def report(where: str, exc: BaseException) -> None:
    """A failure the user has to see: log it and put it on the screen."""
    traceback.print_exc()
    notify("onError", "{}: {}: {}".format(where, type(exc).__name__, exc))
    print("[aura] {} failed: {}".format(where, exc))


def apply_config(raw: str) -> dict:
    """Take the app's paths and put them where the rest of AURA looks for them.

    The Java layer knows the data directory, the folder the user chose, the app
    version and how much of the phone AURA may read; it hands all of it over as
    one JSON object. This writes it into the environment so `aura/host.py` is
    the single place anything else has to ask.
    """
    payload: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        except ValueError:
            report("reading the app's configuration", ValueError("that was not JSON: " + raw[:200]))
    for key, name in (("data_dir", host.DATA_DIR_ENV), ("models_dir", host.MODELS_DIR_ENV),
                      ("webui_dir", host.WEBUI_DIR_ENV), ("native_lib_dir", host.NATIVE_LIB_DIR_ENV)):
        value = str(payload.get(key) or "").strip()
        if value:
            os.environ[name] = value
    os.environ[host.ANDROID_FLAG] = "1"
    os.environ[host.ANDROID_INFO] = json.dumps({
        key: payload.get(key) for key in ("storage_root", "permission", "app_version", "api_level")
        if payload.get(key) is not None
    })
    return payload


# ------------------------------------------------------------------ first run
def _apply_first_run_defaults(root: Path) -> dict:
    """Settings a *phone* wants, written once, on the very first launch.

    Only the two things a desktop build cannot know: where the user's models
    should be kept (external storage, so a 4 GB model does not fill the app's
    private space) and how many cores to use. After this the settings file
    belongs to the user - AURA never rewrites it.
    """
    path = config.settings_path(root)
    if path.exists():
        return {}
    settings = dict(config.DEFAULTS)
    folder = host.default_models_dir()
    if folder:
        settings["models_dir"] = folder
    cores = _cpu_count()
    if cores:
        settings["llm_threads"] = max(1, min(PERFORMANCE_CORE_CAP, cores))
    config.save_settings(root, settings)
    return {"models_dir": settings.get("models_dir", ""), "llm_threads": settings.get("llm_threads")}


def _cpu_count() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 0
    except Exception:  # noqa: BLE001
        return os.cpu_count() or 0


# ------------------------------------------------------------------ background
class BackgroundWatch:
    """Unload the model a while after the app leaves the screen.

    A phone will kill a background app rather than let it hold a gigabyte of
    weights, and being killed while llama-server is still running leaves that
    process behind. Unloading it first is both politer and safer.
    """

    def __init__(self, root: Path, grace: float = BACKGROUND_GRACE_S):
        self.root = Path(root)
        self.grace = float(grace)
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def background(self) -> None:
        with self._lock:
            self._cancel()
            try:
                self._timer = threading.Timer(self.grace, self._unload)
                self._timer.daemon = True
                self._timer.start()
            except RuntimeError:  # no thread support: unload right away
                self._unload()

    def foreground(self) -> None:
        with self._lock:
            self._cancel()

    def _cancel(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def _unload(self) -> None:
        try:
            manager(self.root).stop()
        except Exception:  # noqa: BLE001 - a watchdog must not raise
            pass


# --------------------------------------------------------------------- serving
def _bind(library: Library, preferred: int) -> "server.ThreadingHTTPServer":
    """Start the server on 127.0.0.1, taking any free port if need be."""
    try:
        return server.create_server(library, host="127.0.0.1", port=int(preferred or 0), shell="app")
    except OSError:
        return server.create_server(library, host="127.0.0.1", port=0, shell="app")


def start(preferred_port: int = 0) -> dict:
    """Bring AURA up and return what the app needs to show it."""
    root = config.data_dir()
    first_run = _apply_first_run_defaults(root)
    webui = host.webui_dir()
    if webui:
        server.set_webui_dir(webui)
    library = Library(root)
    if not preferred_port:
        preferred_port = int(library.settings.get("port") or 0)
    httpd = _bind(library, preferred_port)
    engine = manager(root)
    watch = BackgroundWatch(root)
    httpd.api.on_quit = lambda: (engine.stop(), httpd.shutdown())
    state = {
        "root": str(root),
        "webui": str(server.WEBUI_DIR),
        "webui_ready": (Path(server.WEBUI_DIR) / "index.html").is_file(),
        "port": int(httpd.server_address[1]),
        "url": "http://127.0.0.1:{}/".format(int(httpd.server_address[1])),
        "server": httpd,
        "library": library,
        "watch": watch,
        "first_run": first_run,
        "models_dir": str(engine.models_dir()),
        "engine": engine.engine_state(),
    }
    _LIVE.clear()
    _LIVE["state"] = state
    return state


def main(config_json: str = "") -> dict:
    """The entry point the app calls. Blocks while AURA serves."""
    try:
        apply_config(config_json)
        state = start(int(os.environ.get("AURA_PORT") or 0))
    except BaseException as exc:  # noqa: BLE001 - a dead app with no message is useless
        report("starting AURA", exc)
        return {"ok": False, "error": "{}: {}".format(type(exc).__name__, exc)}
    print("[aura] serving {} (data {}, webui {}, models {})".format(
        state["url"], state["root"], state["webui"], state["models_dir"]))
    if not state["webui_ready"]:
        report("finding the interface", RuntimeError(
            "the interface files are missing from this build ({} has no index.html)"
            .format(state["webui"])))
    notify("onReady", state["url"])
    httpd = state["server"]
    try:
        httpd.serve_forever()
    except Exception as exc:  # noqa: BLE001
        report("serving", exc)
    finally:
        try:
            manager(state["root"]).stop()
        except Exception:  # noqa: BLE001
            pass
        notify("onStopped")
    return {"ok": True, "url": state["url"], "root": state["root"]}


# ------------------------------------------------------- calls from the app
def background() -> None:
    """The app moved off the screen (called from Java, on the UI thread)."""
    state = _LIVE.get("state")
    if state:
        state["watch"].background()


def foreground() -> None:
    """The app is back on screen."""
    state = _LIVE.get("state")
    if state:
        state["watch"].foreground()


def shutdown() -> None:
    """Stop serving and unload the model (the app is closing).

    Done on a thread because the app calls this from `onDestroy`, and stopping
    a model can take a few seconds - the caller must not wait for it.
    """
    state = _LIVE.get("state")
    if not state:
        return
    try:
        thread = threading.Thread(target=_quiet, args=(state,), name="aura-shutdown", daemon=True)
        thread.start()
    except RuntimeError:  # no threads available: do it the slow way
        _quiet(state)


def _quiet(state: dict) -> None:
    try:
        manager(state["root"]).stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        state["server"].shutdown()
        state["server"].server_close()
    except Exception:  # noqa: BLE001
        pass
