"""What AURA is running on: a desktop machine, or a phone.

The Windows/macOS/Linux build and the Android app run the *same* code. What
differs is only where things are allowed to live:

* a phone has no home directory worth using (``~`` is not writable), so the
  Android layer hands AURA a real data directory in the app's private storage;
* a phone cannot download and run the llama.cpp engine the way the desktop
  build does - the executable has to sit in the app's native library directory -
  so the engine is *bundled inside the APK* and simply found here;
* a phone has no path the user can type, so the files they want indexed come
  from the system picker.

Rather than sprinkle ``sys.platform`` checks around (which is a trap: Android
reports ``linux``), every one of those facts is answered here, and every answer
can be overridden with an environment variable. That is also what makes this
module testable off a phone: the Android layer (``android/app/src/main/python/
aura_mobile.py``) sets the variables below before AURA starts, and a test can
set them to anything it likes.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

#: Set to "1" by the Android entry point. The JSON blob is its context.
ANDROID_FLAG = "AURA_ANDROID"
ANDROID_INFO = "AURA_ANDROID_INFO"

DATA_DIR_ENV = "AURA_DATA_DIR"
MODELS_DIR_ENV = "AURA_MODELS_DIR"
WEBUI_DIR_ENV = "AURA_WEBUI_DIR"
NATIVE_LIB_DIR_ENV = "AURA_NATIVE_LIB_DIR"

#: Where an Android app may write when the user has *not* granted file access.
ANDROID_APP_DIR_NAME = "AURA"

PLATFORM_LABELS = {
    "windows": "Windows",
    "macos": "macOS",
    "linux": "Linux",
    "android": "Android",
}


def _flag(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in ("1", "true", "yes", "on")


def info() -> Dict[str, object]:
    """The context the Android layer passed in (empty on a desktop)."""
    raw = os.environ.get(ANDROID_INFO) or ""
    if not raw:
        return {}
    try:
        payload = json.loads(raw)
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def is_android(env: Optional[Dict[str, str]] = None, uname: Optional[str] = None) -> bool:
    """Are we the Android app?

    Three independent signals, because getting this wrong breaks the paths:
    the environment variable the Android layer sets, the Android-specific
    ``sys.getandroidapilevel`` that CPython only defines on Android, and (for
    tests) whatever the caller passes in.
    """
    env = os.environ if env is None else env
    if str(env.get(ANDROID_FLAG) or "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    if uname is not None:
        return "android" in str(uname).lower()
    return hasattr(sys, "getandroidapilevel")


def api_level() -> int:
    getter = getattr(sys, "getandroidapilevel", None)
    try:
        return int(getter()) if callable(getter) else 0
    except Exception:  # noqa: BLE001 - a phone that will not say is just 0
        return 0


def storage_root() -> str:
    """The folder on the phone that documents and models are kept in."""
    return str(info().get("storage_root") or "")


def storage_permission() -> str:
    """``"all"`` | ``"app"``: how much of the phone AURA may read."""
    value = str(info().get("permission") or "")
    return value if value in ("all", "app") else "app"


def can_read_files() -> bool:
    """True when AURA may read the user's own documents by path."""
    if not is_android():
        return True
    return storage_permission() == "all"


def native_lib_dir() -> str:
    """The app's native library directory (where the bundled engine lives)."""
    explicit = str(os.environ.get(NATIVE_LIB_DIR_ENV) or "").strip()
    if explicit:
        return explicit
    return str(info().get("native_lib_dir") or "")


def data_dir_env() -> str:
    return str(os.environ.get(DATA_DIR_ENV) or "").strip()


def models_dir_env() -> str:
    return str(os.environ.get(MODELS_DIR_ENV) or "").strip()


def webui_dir() -> str:
    return str(os.environ.get(WEBUI_DIR_ENV) or "").strip()


def default_models_dir() -> str:
    """Where a downloaded model goes when the user has not chosen a folder."""
    explicit = models_dir_env()
    if explicit:
        return explicit
    root = storage_root()
    if root:
        return str(Path(root) / "models")
    return ""


def default_documents_dir() -> str:
    """Where the phone's own documents are, when AURA may read them."""
    root = storage_root()
    return str(Path(root) / "documents") if root else ""


def external_roots() -> List[str]:
    """Places worth offering in the UI (never invented - only what exists)."""
    roots: List[str] = []
    for candidate in (storage_root(), "/storage/emulated/0", "/sdcard"):
        if candidate and candidate not in roots and Path(candidate).is_dir():
            roots.append(candidate)
    for name in ("Documents", "Download", "Downloads", "Pictures"):
        candidate = Path("/storage/emulated/0") / name
        if candidate.is_dir() and str(candidate) not in roots:
            roots.append(str(candidate))
    return roots


def platform_key() -> str:
    """The key the catalogue uses for *this* machine."""
    if is_android():
        return "android"
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def platform_label() -> str:
    return PLATFORM_LABELS.get(platform_key(), platform_key().title())


def home_dir() -> str:
    """The user's home folder, or "" when the platform will not name one.

    Android does give an app a home (Chaquopy creates one), but a stripped-down
    environment may not, and `Path.home()` *raises* rather than reporting
    nothing - which would take down the endpoint that only wanted to describe
    the machine it is running on.
    """
    try:
        return str(Path.home())
    except (RuntimeError, OSError):
        return str(os.environ.get("HOME") or "")


def describe(bundled_engine: bool = False) -> Dict[str, object]:
    """Everything the interface needs to explain where it is running.

    Kept plain (strings, booleans) so it can go straight out over the API.
    """
    payload = info()
    return {
        "android": is_android(),
        "platform": platform_key(),
        "label": platform_label(),
        "api_level": api_level(),
        "app_version": str(payload.get("app_version") or ""),
        "storage_root": storage_root(),
        "permission": storage_permission(),
        "can_read_files": can_read_files(),
        "native_lib_dir": native_lib_dir(),
        "bundled_engine": bool(bundled_engine),
        "picker": bool(is_android()),
        "external_roots": external_roots() if is_android() else [],
        "home": home_dir(),
    }
