"""Putting AURA's window on screen.

AURA is a local server plus a single-page interface, so the same interface can
be shown in three different shells:

    webview   a real native window - pywebview driving WebView2 on Windows,
              WKWebView on macOS, WebKitGTK on Linux. No browser, no tabs, no
              address bar; closing it quits AURA.
    app       a Chromium-family browser (Edge, Chrome, Brave) started with
              ``--app=``, which is a chromeless window that looks like an
              application. Used when pywebview is missing or cannot start.
    browser   the default browser's new tab - what AURA used to do.

`open_window` walks that list until one shell works, so the app never ends up
with no interface at all, and says which one it picked. Choosing the shell is
separated from running it: every decision function here takes its environment
as an argument (and every process launch goes through one injectable call), so
the tests can exercise the whole ladder without a screen, a browser or Windows.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import webbrowser
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from . import catalog, config

WINDOW_TITLE = "{} - {}".format(config.APP_NAME, config.APP_TAGLINE)
WINDOW_WIDTH = 1240
WINDOW_HEIGHT = 820
WINDOW_MIN = (960, 640)
# The window is painted this colour before the page has drawn anything. It is the
# interface's light background: the theme the page picks is light unless the
# machine says otherwise, and a dark window flashing white is worse than a light
# one flashing dark.
WINDOW_BG = "#ffffff"

SHELLS = ("webview", "app", "browser", "none")
SHELL_LABELS = {
    "webview": "a native AURA window",
    "app": "an app window (no tabs, no address bar)",
    "browser": "your browser",
    "none": "no window",
}

WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"

# where a Chromium-family browser hides on each platform, best first
WINDOWS_BROWSERS = (
    ("Microsoft/Edge/Application", "msedge.exe",
     ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432", "LOCALAPPDATA")),
    ("Google/Chrome/Application", "chrome.exe",
     ("LOCALAPPDATA", "ProgramFiles", "ProgramFiles(x86)")),
    ("BraveSoftware/Brave-Browser/Application", "brave.exe",
     ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA")),
    ("Vivaldi/Application", "vivaldi.exe", ("LOCALAPPDATA", "ProgramFiles")),
)
WINDOWS_BROWSER_NAMES = ("msedge.exe", "chrome.exe", "brave.exe", "vivaldi.exe", "chromium.exe")
MAC_BROWSER_PATHS = (
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)
POSIX_BROWSER_NAMES = (
    "microsoft-edge", "microsoft-edge-stable", "google-chrome", "google-chrome-stable",
    "chromium", "chromium-browser", "brave-browser", "vivaldi",
)


class ShellUnavailable(Exception):
    """One shell could not be used on this machine - try the next one."""


# ------------------------------------------------------------------- browsers
def browser_candidates(platform_name: Optional[str] = None, environ: Optional[dict] = None,
                       which: Optional[Callable[[str], Optional[str]]] = None) -> List[str]:
    """Ordered paths of browsers that can host an app window on this machine."""
    env = dict(os.environ) if environ is None else dict(environ)
    finder = which if which is not None else shutil.which
    name = platform_name or catalog.platform_key()
    paths: List[str] = []
    if name == "windows":
        for folder, exe, roots in WINDOWS_BROWSERS:
            for root in roots:
                base = env.get(root)
                if base:
                    paths.append(str(Path(base) / folder / exe))
        for exe in WINDOWS_BROWSER_NAMES:
            paths.append(finder(exe))
    elif name == "macos":
        paths.extend(MAC_BROWSER_PATHS)
    else:
        for exe in POSIX_BROWSER_NAMES:
            paths.append(finder(exe))
    return [path for path in paths if path]


def find_browser(platform_name: Optional[str] = None, environ: Optional[dict] = None,
                 which: Optional[Callable[[str], Optional[str]]] = None,
                 exists: Optional[Callable[[str], bool]] = None) -> str:
    """The first browser on this machine that is actually installed, or ""."""
    present = exists if exists is not None else (lambda path: Path(path).is_file())
    for candidate in browser_candidates(platform_name, environ, which):
        if present(candidate):
            return candidate
    return ""


def app_argv(exe: str, url: str, width: int = WINDOW_WIDTH, height: int = WINDOW_HEIGHT,
             profile_dir=None) -> List[str]:
    """Chromium arguments that give a chromeless, app-looking window."""
    argv = [
        str(exe),
        "--app=" + str(url),
        "--window-size={},{}".format(int(width), int(height)),
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,TranslateUI,MediaRouter,AutofillServerCommunication",
    ]
    if profile_dir:
        argv.append("--user-data-dir=" + str(profile_dir))
    return argv


# -------------------------------------------------------------------- webview
def webview_module():
    """pywebview, if it was installed (`import webview`)."""
    import importlib
    try:
        return importlib.import_module("webview")
    except Exception:  # noqa: BLE001 - any failure means "not available"
        return None


def webview_runtime_ready(platform_name: Optional[str] = None) -> bool:
    """Is the OS webview component there? Assume yes when we cannot tell.

    On Windows pywebview needs the Edge **WebView2** runtime, which ships with
    Windows 11 and with every Edge update on Windows 10. Asking the registry
    first means a machine without it goes straight to the app window instead of
    opening a blank frame.
    """
    if (platform_name or catalog.platform_key()) != "windows":
        return True
    try:
        import winreg
    except Exception:  # noqa: BLE001 - not Windows after all
        return True
    views = (getattr(winreg, "KEY_WOW64_32KEY", 0), getattr(winreg, "KEY_WOW64_64KEY", 0), 0)
    locations = (
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\\" + WEBVIEW2_GUID),
        (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\EdgeUpdate\Clients\\" + WEBVIEW2_GUID),
    )
    for hive, path in locations:
        for view in views:
            try:
                with winreg.OpenKey(hive, path, 0, winreg.KEY_READ | view) as key:
                    value, _ = winreg.QueryValueEx(key, "pv")
                if value:
                    return True
            except OSError:
                continue
    return False


def check_webview(module=None, ready: Optional[bool] = None):
    """Return the pywebview module if this machine can show a native window.

    Raises ShellUnavailable otherwise, so the caller can try the next shell
    *before* anything is opened or announced.
    """
    webview = module if module is not None else webview_module()
    if webview is None:
        raise ShellUnavailable("pywebview is not installed")
    available = webview_runtime_ready() if ready is None else ready
    if not available:
        raise ShellUnavailable("the WebView2 runtime is missing")
    return webview


def run_webview(url: str, title: str = WINDOW_TITLE, width: int = WINDOW_WIDTH,
                height: int = WINDOW_HEIGHT, module=None, ready: Optional[bool] = None) -> bool:
    """Open the native window and block until the user closes it."""
    webview = check_webview(module, ready)
    webview.create_window(title, url, width=int(width), height=int(height), min_size=WINDOW_MIN,
                          resizable=True, background_color=WINDOW_BG, text_select=True)
    webview.start(debug=False)
    return True


# --------------------------------------------------------------------- shells
def _spawn(argv: Sequence[str]) -> None:
    """Start the browser window detached, so it outlives nothing and prints nothing."""
    options: Dict[str, object] = {
        "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        options["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    else:
        options["start_new_session"] = True
    subprocess.Popen(list(argv), **options)


def open_window(url: str, title: str = WINDOW_TITLE, width: int = WINDOW_WIDTH,
                height: int = WINDOW_HEIGHT, prefer: str = "auto", profile_dir=None,
                module=None, ready: Optional[bool] = None,
                spawn: Optional[Callable[[Sequence[str]], None]] = None,
                opener: Optional[Callable[[str], object]] = None,
                on_shell: Optional[Callable[[str], None]] = None,
                platform_name: Optional[str] = None, environ: Optional[dict] = None,
                which=None, exists=None) -> dict:
    """Show AURA's interface, best shell first.

    Returns a dict describing what happened: ``shell`` (which one was used, or
    "none"), ``blocking`` (True when the call returned because the window was
    closed, i.e. the app should now stop) and ``detail`` (why anything lower in
    the list was skipped). ``on_shell`` is called with the chosen shell as soon
    as it is known - before a native window blocks - so the server can tell the
    interface which shell it is running in.
    """
    order = shell_order(prefer)
    launch = spawn if spawn is not None else _spawn
    open_url = opener if opener is not None else open_in_browser
    problems: List[str] = []

    def chosen(shell: str) -> None:
        if on_shell is not None:
            try:
                on_shell(shell)
            except Exception:  # noqa: BLE001 - a listener must not break the window
                pass

    for shell in order:
        try:
            if shell == "webview":
                check_webview(module, ready)
                chosen(shell)
                run_webview(url, title, width, height, module=module, ready=ready)
                return {"shell": "webview", "blocking": True, "detail": "", "problems": problems}
            if shell == "app":
                exe = find_browser(platform_name, environ, which, exists)
                if not exe:
                    raise ShellUnavailable("no Edge, Chrome or Brave found on this machine")
                chosen(shell)
                launch(app_argv(exe, url, width, height, profile_dir))
                return {"shell": "app", "blocking": False, "detail": "", "problems": problems}
            if shell == "browser":
                chosen(shell)
                open_url(url)
                return {"shell": "browser", "blocking": False, "detail": "", "problems": problems}
        except ShellUnavailable as exc:
            problems.append("{}: {}".format(shell, exc))
        except Exception as exc:  # noqa: BLE001 - a broken shell must not stop the app
            problems.append("{}: {}: {}".format(shell, type(exc).__name__, exc))
    return {"shell": "none", "blocking": False,
            "detail": "; ".join(problems) or "no window was asked for", "problems": problems}


def shell_order(prefer: str = "auto") -> List[str]:
    """Which shells to try, in order, for a `--shell` value."""
    want = str(prefer or "auto").strip().lower()
    if want in ("", "auto", "window", "native"):
        return ["webview", "app", "browser"]
    if want == "none":
        return []
    if want not in SHELLS:
        raise ShellUnavailable("unknown shell '{}' (try webview, app, browser or none)".format(prefer))
    return [want, "browser"] if want != "browser" else ["browser"]


def describe(result: dict) -> str:
    """One plain sentence for the startup banner."""
    shell = (result or {}).get("shell", "none")
    if shell == "none":
        return "none - no window could be opened ({})".format(
            (result or {}).get("detail") or "unknown reason")
    line = SHELL_LABELS.get(shell, shell)
    detail = (result or {}).get("detail")
    if detail:
        line += " - " + detail
    for problem in (result or {}).get("problems") or []:
        line += "\n                            (skipped {})".format(problem)
    return line


# ------------------------------------------------------------------- small bits
def open_in_browser(url: str) -> bool:
    """Hand the URL to the user's default browser."""
    try:
        webbrowser.open(str(url))
        return True
    except Exception:  # noqa: BLE001 - a missing browser is not fatal
        return False


def hide_console() -> bool:
    """Hide the console window, but only when it belongs to this process.

    A double-clicked AURA.exe owns its console, so hiding it removes the black
    window behind the app. Started from a terminal, the console is shared with
    the shell the user is typing in, so it is left alone.
    """
    if os.name != "nt":
        return False
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetConsoleWindow()
        if not handle:
            return False
        attached = (ctypes.c_uint * 4)()
        if kernel32.GetConsoleProcessList(attached, 4) > 1:
            return False
        ctypes.windll.user32.ShowWindow(handle, 0)  # SW_HIDE
        return True
    except Exception:  # noqa: BLE001
        return False


def show_console() -> bool:
    """Undo hide_console() - used when the native window fell back to a browser."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        handle = ctypes.windll.kernel32.GetConsoleWindow()
        if not handle:
            return False
        ctypes.windll.user32.ShowWindow(handle, 5)  # SW_SHOW
        return True
    except Exception:  # noqa: BLE001
        return False


def show_message(title: str, text: str, error: bool = True) -> bool:
    """Tell the user something when there is no window to tell them in."""
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, str(text), str(title),
                                             0x10 if error else 0x40)
            return True
        except Exception:  # noqa: BLE001
            pass
    print("{}: {}".format(title, text))
    return False
