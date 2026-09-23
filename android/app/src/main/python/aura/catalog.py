"""What AURA can run locally: the models it knows how to fetch, and the engine.

Two pieces of data live here, both deliberately plain:

* `MODELS` - a short list of small, instruction-tuned GGUF models that actually
  run on a student laptop. Every file carries its exact byte size and sha256, so
  a download can be verified end to end, and the UI can honestly say
  "1.1 GB, about 2.5 GB of RAM" *before* anything is fetched.
* the per-platform pattern for the official prebuilt llama.cpp release that
  serves those models (see `aura/llama_server.py` for how it is used).

Nothing here downloads anything by itself - `aura/downloads.py` does that, and
`aura/llama_server.py` decides when.
"""
from __future__ import annotations

import platform
from pathlib import Path
from typing import Dict, List, Optional

from . import host

MODELS_DIR_NAME = "models"
RUNTIME_DIR_NAME = "runtime"
ENGINE_INFO_NAME = "engine.json"

# The release the fallback URLs point at. Resolved dynamically at runtime
# (latest release with a matching asset), so this only matters when the GitHub
# API cannot be reached.
PINNED_RUNTIME_TAG = "b11136"

RUNTIME_ASSETS = {
    "windows": {
        "x64": "bin-win-cpu-x64.zip",
        "arm64": "bin-win-cpu-arm64.zip",
    },
    "linux": {
        "x64": "bin-ubuntu-x64.tar.gz",
        "arm64": "bin-ubuntu-arm64.tar.gz",
    },
    "macos": {
        "x64": "bin-macos-x64.tar.gz",
        "arm64": "bin-macos-arm64.tar.gz",
    },
    # Only used to document/repair the Android bundle: the app does not fetch
    # this at runtime, because a phone cannot execute a file it downloaded into
    # its own storage (Android 10+). The build machine unpacks this archive
    # into the APK's native libraries instead - see android/README.md.
    "android": {
        "arm64": "bin-android-arm64.tar.gz",
    },
}

#: The llama.cpp Android release is built as one tiny launcher plus the real
#: code in shared objects. The launcher is the executable; it is renamed so the
#: Android packager will place it in the app's native library directory (the
#: only place an app is allowed to execute from), and every other file in this
#: list has to sit next to it.
BUNDLED_ENGINE_BINARY = "libllama-server-bin.so"
ENGINE_LIBS = (
    "libllama-server-impl.so",
    "libllama-common.so",
    "libllama.so",
    "libmtmd.so",
    "libggml.so",
    "libggml-base.so",
    "libggml-cpu-android_armv8.0_1.so",
    "libggml-cpu-android_armv8.2_1.so",
    "libggml-cpu-android_armv8.2_2.so",
    "libggml-cpu-android_armv8.6_1.so",
    "libggml-cpu-android_armv9.0_1.so",
    "libggml-cpu-android_armv9.2_1.so",
    "libggml-cpu-android_armv9.2_2.so",
)
ENGINE_BUNDLE_FILES = (BUNDLED_ENGINE_BINARY,) + ENGINE_LIBS
ENGINE_ARCHIVE = "bin-android-arm64.tar.gz"

_RELEASES_API = "https://api.github.com/repos/ggml-org/llama.cpp/releases?per_page=15"
_FALLBACK_BASE = "https://github.com/ggml-org/llama.cpp/releases/download/{}"

BINARY_NAMES = ("llama-server.exe", "llama-server", BUNDLED_ENGINE_BINARY)


def _hf(repo: str, filename: str) -> str:
    return "https://huggingface.co/{}/resolve/main/{}?download=true".format(repo, filename)


MODELS: List[dict] = [
    {
        "id": "qwen2.5-0.5b-instruct",
        "name": "Qwen2.5 0.5B Instruct",
        "params": "0.5B",
        "quant": "Q4_K_M",
        "repo": "Qwen/Qwen2.5-0.5B-Instruct-GGUF",
        "files": [{"name": "qwen2.5-0.5b-instruct-q4_k_m.gguf", "bytes": 491400032,
                   "sha256": "74a4da8c9fdbcd15bd1f6d01d621410d31c6fc00986f5eb687824e7b93d7a9db"}],
        "context": 8192,
        "ram_gb": 1.5,
        "license": "Apache-2.0",
        "note": "The smallest sensible one - fastest to load, weakest writing. Good on a 4 GB machine.",
        "recommended": False,
    },
    {
        "id": "qwen2.5-1.5b-instruct",
        "name": "Qwen2.5 1.5B Instruct",
        "params": "1.5B",
        "quant": "Q4_K_M",
        "repo": "Qwen/Qwen2.5-1.5B-Instruct-GGUF",
        "files": [{"name": "qwen2.5-1.5b-instruct-q4_k_m.gguf", "bytes": 1117320736,
                   "sha256": "6a1a2eb6d15622bf3c96857206351ba97e1af16c30d7a74ee38970e434e9407e"}],
        "context": 8192,
        "ram_gb": 2.5,
        "license": "Apache-2.0",
        "note": "The best size-to-quality balance for a normal laptop. Start here.",
        "recommended": True,
    },
    {
        "id": "gemma-2-2b-it",
        "name": "Gemma 2 2B Instruct",
        "params": "2B",
        "quant": "Q4_K_M",
        "repo": "bartowski/gemma-2-2b-it-GGUF",
        "files": [{"name": "gemma-2-2b-it-Q4_K_M.gguf", "bytes": 1708582752,
                   "sha256": "e0aee85060f168f0f2d8473d7ea41ce2f3230c1bc1374847505ea599288a7787"}],
        "context": 8192,
        "ram_gb": 3.0,
        "license": "Gemma Terms of Use",
        "note": "Reads nicely, writes shorter answers than Qwen. Google's licence applies.",
        "recommended": False,
    },
    {
        "id": "llama-3.2-3b-instruct",
        "name": "Llama 3.2 3B Instruct",
        "params": "3B",
        "quant": "Q4_K_M",
        "repo": "bartowski/Llama-3.2-3B-Instruct-GGUF",
        "files": [{"name": "Llama-3.2-3B-Instruct-Q4_K_M.gguf", "bytes": 2019377696,
                   "sha256": "6c1a2b41161032677be168d354123594c0e6e67d2b9227c84f296ad037c728ff"}],
        "context": 131072,
        "ram_gb": 4.0,
        "license": "Llama 3.2 Community Licence",
        "note": "Best at following the 'cite your sources' instruction. Meta's licence applies.",
        "recommended": False,
    },
    {
        "id": "qwen2.5-3b-instruct",
        "name": "Qwen2.5 3B Instruct",
        "params": "3B",
        "quant": "Q4_K_M",
        "repo": "Qwen/Qwen2.5-3B-Instruct-GGUF",
        "files": [{"name": "qwen2.5-3b-instruct-q4_k_m.gguf", "bytes": 2104932768,
                   "sha256": "626b4a6678b86442240e33df819e00132d3ba7dddfe1cdc4fbb18e0a9615c62d"}],
        "context": 32768,
        "ram_gb": 4.0,
        "license": "Qwen Research Licence",
        "note": "A step up from the 1.5B if your machine has 8 GB of RAM.",
        "recommended": False,
    },
    {
        "id": "phi-3.5-mini-instruct",
        "name": "Phi-3.5 Mini Instruct",
        "params": "3.8B",
        "quant": "Q4_K_M",
        "repo": "bartowski/Phi-3.5-mini-instruct-GGUF",
        "files": [{"name": "Phi-3.5-mini-instruct-Q4_K_M.gguf", "bytes": 2393232672,
                   "sha256": "e4165e3a71af97f1b4820da61079826d8752a2088e313af0c7d346796c38eff5"}],
        "context": 131072,
        "ram_gb": 5.0,
        "license": "MIT",
        "note": "Strong at explaining technical material. Long context, more RAM.",
        "recommended": False,
    },
    {
        "id": "qwen2.5-7b-instruct",
        "name": "Qwen2.5 7B Instruct",
        "params": "7B",
        "quant": "Q4_K_M",
        "repo": "Qwen/Qwen2.5-7B-Instruct-GGUF",
        "files": [
            {"name": "qwen2.5-7b-instruct-q4_k_m-00001-of-00002.gguf", "bytes": 3993201344,
             "sha256": "dfce12e3862a5283ccfb88221b48480e58745165de856439950d0f22590580db"},
            {"name": "qwen2.5-7b-instruct-q4_k_m-00002-of-00002.gguf", "bytes": 689872288,
             "sha256": "539cf93f78e887edea1c04e2d7d8cdaca9d01dae9c9025bcb8accbe29df3d72a"},
        ],
        "context": 32768,
        "ram_gb": 8.0,
        "license": "Apache-2.0",
        "note": "Clearly the best writing here, and the slowest. Two files, 4.5 GB total.",
        "recommended": False,
    },
]

for _model in MODELS:
    for _file in _model["files"]:
        _file["url"] = _hf(_model["repo"], _file["name"])
    _model["bytes"] = sum(_file["bytes"] for _file in _model["files"])
    _model["file"] = _model["files"][0]["name"]
    _model["repo_url"] = "https://huggingface.co/{}".format(_model["repo"])


# --------------------------------------------------------------------- lookups
def entry(model_id: str) -> Optional[dict]:
    for model in MODELS:
        if model["id"] == model_id:
            return model
    return None


def for_filename(filename: str) -> Optional[dict]:
    """Which catalogue entry does this on-disk .gguf belong to (if any)?"""
    for model in MODELS:
        for item in model["files"]:
            if item["name"] == filename:
                return model
    return None


def recommended() -> dict:
    for model in MODELS:
        if model.get("recommended"):
            return model
    return MODELS[0]


def small_first() -> List[dict]:
    return sorted(MODELS, key=lambda m: m["bytes"])


# -------------------------------------------------------------------- installed
def file_state(item: dict, models_dir: Path) -> dict:
    """Is this one file present, and does its size match the catalogue?"""
    path = Path(models_dir) / item["name"]
    if not path.exists():
        part = path.with_name(path.name + ".part")
        if part.exists():
            return {"state": "downloading", "path": str(path), "on_disk": part.stat().st_size,
                    "expected": item["bytes"]}
        return {"state": "missing", "path": str(path), "on_disk": 0, "expected": item["bytes"]}
    size = path.stat().st_size
    if size != item["bytes"]:
        return {"state": "incomplete", "path": str(path), "on_disk": size, "expected": item["bytes"]}
    return {"state": "ready", "path": str(path), "on_disk": size, "expected": item["bytes"]}


def status_of(model: dict, models_dir: Path) -> dict:
    """Catalogue entry + what the disk actually has for it."""
    states = [file_state(item, models_dir) for item in model["files"]]
    if all(s["state"] == "ready" for s in states):
        overall = "ready"
    elif any(s["state"] == "downloading" for s in states):
        overall = "downloading"
    elif any(s["state"] in ("incomplete", "downloading") for s in states):
        overall = "incomplete"
    elif any(s["state"] == "ready" for s in states):
        overall = "partial"
    else:
        overall = "missing"
    payload = {
        "id": model["id"], "name": model["name"], "params": model["params"],
        "quant": model["quant"], "bytes": model["bytes"], "size": human_size(model["bytes"]),
        "ram_gb": model["ram_gb"], "license": model["license"], "note": model["note"],
        "recommended": bool(model.get("recommended")), "context": model["context"],
        "repo_url": model["repo_url"], "file": model["file"], "files": len(model["files"]),
        "state": overall,
        "path": str(Path(models_dir) / model["file"]),
        "on_disk": sum(s["on_disk"] for s in states),
        "primary_ready": states[0]["state"] == "ready",
        "file_states": [dict(s, name=item["name"]) for s, item in zip(states, model["files"])],
    }
    return payload


def installed_files(models_dir: Path) -> List[dict]:
    """Every .gguf sitting in the models folder, catalogue entry or not."""
    folder = Path(models_dir)
    if not folder.exists():
        return []
    found = []
    for path in sorted(folder.glob("*.gguf")):
        model = for_filename(path.name)
        found.append({
            "name": path.name,
            "path": str(path),
            "bytes": path.stat().st_size,
            "size": human_size(path.stat().st_size),
            "model_id": model["id"] if model else "",
            "title": model["name"] if model else path.stem,
            "known": bool(model),
            "part": None,
        })
    for path in sorted(folder.glob("*.gguf.part")):
        found.append({
            "name": path.name[:-5], "path": str(path.with_name(path.name[:-5])),
            "bytes": path.stat().st_size, "size": human_size(path.stat().st_size),
            "model_id": "", "title": path.name[:-5], "known": False, "part": str(path),
        })
    return found


# ---------------------------------------------------------------------- engine
def platform_key() -> str:
    return host.platform_key()


def arch_key() -> str:
    machine = (platform.machine() or "").lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return "x64"


def runtime_pattern(platform_name: Optional[str] = None, arch: Optional[str] = None) -> str:
    platform_name = platform_name or platform_key()
    arch = arch or arch_key()
    table = RUNTIME_ASSETS.get(platform_name) or RUNTIME_ASSETS["linux"]
    return table.get(arch) or table["x64"]


def runtime_asset(assets: List[dict], platform_name: Optional[str] = None,
                  arch: Optional[str] = None) -> Optional[dict]:
    """Pick the llama.cpp release asset that matches this machine."""
    pattern = runtime_pattern(platform_name, arch)
    for asset in assets or []:
        name = asset.get("name") or ""
        if name.startswith("cudart-") or name.startswith("llama-ui"):
            continue
        if pattern in name:
            return asset
    return None


def pinned_runtime_url(platform_name: Optional[str] = None, arch: Optional[str] = None) -> str:
    return "{}/llama-{}-{}".format(_FALLBACK_BASE.format(PINNED_RUNTIME_TAG), PINNED_RUNTIME_TAG,
                                   runtime_pattern(platform_name, arch))


def releases_url() -> str:
    return _RELEASES_API


def is_bundled_engine() -> bool:
    """True when the engine ships inside the app instead of being downloaded.

    That is the Android case: an app may only execute files that came with it,
    so `llama-server` is delivered in the APK's native libraries.
    """
    return host.is_android()


def binary_name() -> str:
    if is_bundled_engine():
        return BUNDLED_ENGINE_BINARY
    return "llama-server.exe" if platform_key() == "windows" else "llama-server"


def bundled_engine_report() -> dict:
    """Is the engine this app was built with actually in place?

    `installed` means llama-server is there *and* every shared object it loads is
    there - that is, that it can actually run. `missing` names the files the
    bundling step did not manage to include, which is what a build machine that
    could not reach GitHub looks like from inside the app.
    """
    folder = host.native_lib_dir()
    binary = Path(folder) / BUNDLED_ENGINE_BINARY if folder else None
    present = bool(binary) and binary.exists()
    missing = [name for name in ENGINE_BUNDLE_FILES if folder and not (Path(folder) / name).exists()]
    return {
        "bundled": True,
        "folder": folder,
        "binary": str(binary) if binary else "",
        "binary_present": present,
        "installed": present and not missing,
        "complete": bool(folder) and not missing,
        "missing": missing,
        "expected": list(ENGINE_BUNDLE_FILES),
        "archive": ENGINE_ARCHIVE,
    }


def find_binary(folder: Path) -> Optional[Path]:
    """llama-server lives at the archive root today, but do not assume it."""
    folder = Path(folder)
    for name in BINARY_NAMES:
        direct = folder / name
        if direct.exists():
            return direct
    if not folder.exists():
        return None
    for name in BINARY_NAMES:
        for candidate in sorted(folder.rglob(name)):
            if candidate.is_file():
                return candidate
    return None


def human_size(count: int) -> str:
    value = float(count or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit in ("B", "KB"):
                return "{:.0f} {}".format(value, unit)
            return "{:.1f} {}".format(value, unit)
        value /= 1024
    return "{} B".format(count)


def summary() -> Dict[str, object]:
    """Small blob the UI can show without guessing at anything."""
    return {"platform": platform_key(), "arch": arch_key(),
            "engine_asset": runtime_pattern(), "engine_pinned": pinned_runtime_url(),
            "engine_bundled": is_bundled_engine(), "models": len(MODELS)}
