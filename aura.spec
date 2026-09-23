# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller build recipe for AURA.

The Windows/Linux build server picks this file up automatically (it looks for
aura.spec / AURA.spec / pyinstaller.spec), which is the only way to guarantee
that the web UI ships inside the executable - PyInstaller does not bundle
non-Python data files on its own.

    onefile   -> a single AURA.exe (default; easiest to hand to a classmate)
    directory -> dist/AURA/ with the binary plus its _internal folder

Set ONEFILE = False when you want to bundle a large .gguf model, since one-file
mode unpacks everything to a temp folder on every launch. Drop the model in the
repo root as model.gguf and it is picked up automatically either way.

pywebview (the native window) is optional at build time: when it is installed
it is collected with its WebView2 bindings and AURA opens a real window, and
when it is not the app falls back to a chromeless browser window. Both paths
are in aura/desktop.py, and nothing here fails the build if it is missing.
"""
from pathlib import Path

try:  # PyInstaller injects its own names, but is_win is not one of them
    from PyInstaller.compat import is_win
except Exception:  # noqa: BLE001
    is_win = False

ROOT = Path(SPECPATH)  # noqa: F821  (provided by PyInstaller)
NAME = "AURA"
ONEFILE = True
CONSOLE = True  # kept for the diagnostic log; AURA hides this window itself
                # when it opens a native window (desktop.hide_console)

datas = [(str(ROOT / "aura" / "webui"), "aura/webui")]

model = ROOT / "model.gguf"
if model.exists():
    datas.append((str(model), "."))

binaries = []
hiddenimports = []
hooksconfig = {}

# ---- the native window (optional) -------------------------------------------
# pywebview imports its platform backends by name at runtime, so pointing
# PyInstaller at them explicitly is what makes the WebView2 path work; its own
# hook (webview/__pyinstaller) brings the DLLs and js/ with it.
try:
    import webview  # noqa: F401
except Exception:
    webview = None

if webview is not None:
    try:
        from PyInstaller.utils.hooks import collect_all
        web_datas, web_binaries, web_hidden = collect_all("webview")
        datas += web_datas
        binaries += web_binaries
        hiddenimports += web_hidden
    except Exception:
        pass
    for extra in ("webview.platforms.winforms", "webview.platforms.edgechromium",
                  "webview.platforms.mshtml", "webview.platforms.cef",
                  "webview.platforms.gtk", "webview.platforms.cocoa",
                  "clr", "clr_loader", "pythonnet", "bottle", "proxy_tools"):
        if extra not in hiddenimports:
            hiddenimports.append(extra)

excludes = [
    "tkinter", "matplotlib", "PyQt5", "PyQt6", "PySide2", "PySide6", "cefpython3",
    "IPython", "jupyter", "notebook", "pytest", "sphinx",
]

icon = ROOT / "aura.ico"
if icon.exists() and is_win:
    icon_arg = str(icon)
else:
    icon_arg = None

a = Analysis(
    [str(ROOT / "aura_main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig=hooksconfig,
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
)

pyz = PYZ(a.pure)

if ONEFILE:
    exe = EXE(
        pyz, a.scripts, a.binaries, a.datas, [],
        name=NAME, debug=False, bootloader_ignore_signals=False,
        strip=False, upx=False, runtime_tmpdir=None,
        console=CONSOLE, disable_windowed_traceback=False,
        icon=icon_arg,
    )
else:
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True, name=NAME, debug=False,
        bootloader_ignore_signals=False, strip=False, upx=False,
        console=CONSOLE, disable_windowed_traceback=False,
        icon=icon_arg,
    )
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=NAME)
