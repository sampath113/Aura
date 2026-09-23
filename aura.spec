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
"""
from pathlib import Path

ROOT = Path(SPECPATH)  # noqa: F821  (provided by PyInstaller)
NAME = "AURA"
ONEFILE = True
CONSOLE = True  # keep the console window: it prints the phone-access URL and errors

datas = [(str(ROOT / "aura" / "webui"), "aura/webui")]

model = ROOT / "model.gguf"
if model.exists():
    datas.append((str(model), "."))

excludes = [
    "tkinter", "matplotlib", "PyQt5", "PyQt6", "PySide2", "PySide6",
    "IPython", "jupyter", "notebook", "pytest", "sphinx",
]

a = Analysis(
    [str(ROOT / "aura_main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
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
    )
else:
    exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True, name=NAME, debug=False,
        bootloader_ignore_signals=False, strip=False, upx=False,
        console=CONSOLE, disable_windowed_traceback=False,
    )
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=NAME)
