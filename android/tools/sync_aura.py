#!/usr/bin/env python3
"""Copy AURA's Python into the Android app, byte for byte.

Why a copy exists at all: the Android build has to package the Python code
*inside the APK* (Chaquopy's `srcDirs`), and the natural source directory for
that is `android/app/src/main/python`. Pointing Chaquopy at the repo root
instead would drag the whole Gradle project - including its own `build/`
output - into the app, so the honest options were a copy or a build step that
has to run in exactly the right order. A copy cannot be ordered wrongly.

So `aura/` is the source, and `android/app/src/main/python/aura/` is what the
APK actually runs. This script is what keeps them equal, and
`tests/test_aura.py::AndroidMirrorTests` fails the moment they differ - so the
worst case is a red test, never a phone running last week's code.

    python android/tools/sync_aura.py           # copy (shows what changed)
    python android/tools/sync_aura.py --check   # report differences, exit 1
"""
from __future__ import annotations

import filecmp
import shutil
import sys
from pathlib import Path
from typing import List, Tuple

HERE = Path(__file__).resolve()
ANDROID = HERE.parent.parent                 # .../android
REPO = ANDROID.parent                        # .../aura-app
SOURCE = REPO / "aura"
TARGET = ANDROID / "app" / "src" / "main" / "python" / "aura"

SKIP_DIRS = {"__pycache__"}


def relative_files(root: Path) -> List[str]:
    """Every file under `root`, as a relative posix path, sorted."""
    found = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if relative.suffix == ".pyc":
            continue
        found.append(relative.as_posix())
    return found


def differences() -> List[Tuple[str, str]]:
    """(relative path, what is wrong) for every file that is not identical."""
    problems: List[Tuple[str, str]] = []
    source = {name for name in relative_files(SOURCE)}
    target = {name for name in relative_files(TARGET)} if TARGET.exists() else set()
    for name in sorted(source | target):
        if name not in target:
            problems.append((name, "missing from the Android copy"))
        elif name not in source:
            problems.append((name, "only in the Android copy"))
        elif not filecmp.cmp(SOURCE / name, TARGET / name, shallow=False):
            problems.append((name, "different"))
    return problems


def sync() -> List[str]:
    """Make the Android copy identical, and report what changed."""
    changed: List[str] = []
    source = {name for name in relative_files(SOURCE)}
    target = {name for name in relative_files(TARGET)} if TARGET.exists() else set()
    for name in sorted(target - source):
        (TARGET / name).unlink()
        changed.append("removed " + name)
    for name in sorted(source):
        destination = TARGET / name
        if name in target and filecmp.cmp(SOURCE / name, destination, shallow=False):
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(SOURCE / name, destination)
        changed.append(("updated " if name in target else "added ") + name)
    return changed


def main(argv: List[str]) -> int:
    if "--check" in argv:
        problems = differences()
        for name, problem in problems:
            print("{}: {}".format(name, problem))
        if problems:
            print("\n{} file(s) differ - run: python android/tools/sync_aura.py".format(len(problems)))
            return 1
        print("the Android copy matches aura/ exactly")
        return 0
    changed = sync()
    for line in changed:
        print(line)
    if changed:
        print("{} file(s) changed".format(len(changed)))
    else:
        print("already identical")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
