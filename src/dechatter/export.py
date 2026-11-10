"""Build the static widget folder that cncjs serves.

The folder holds the widget, dechatter's own Python source, and a pinned Pyodide with numpy
and scipy, so the widget needs no server of ours and no internet at the machine. Pyodide is
downloaded once, at export time, into a cache.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import urllib.request
import zipfile
from collections.abc import Callable
from importlib import resources
from pathlib import Path

log = logging.getLogger(__name__)

PYODIDE_VERSION = "314.0.7"
PYODIDE_URL = f"https://cdn.jsdelivr.net/pyodide/v{PYODIDE_VERSION}/full/"
LOCK = "pyodide-lock.json"
LOADER = "pyodide.mjs"
RUNTIME = "pyodide.asm.mjs"
MODULES = {LOADER: "pyodide.js", RUNTIME: "pyodide.asm.js"}
CORE = (LOADER, RUNTIME, "pyodide.asm.wasm", "python_stdlib.zip", LOCK)
PACKAGES = ("numpy", "scipy")
SOURCE_ZIP = "dechatter.zip"
WIDGET_FILES = ("index.html", "widget.js", "widget.css", "engine.js", "mic.js", "mic-worklet.js")

Fetch = Callable[[str, Path], None]


class ExportError(RuntimeError):
    pass


def default_cache() -> Path:
    return Path.home() / ".cache" / "dechatter" / f"pyodide-{PYODIDE_VERSION}"


def _download(url: str, target: Path) -> None:
    log.info("downloading %s", url)
    partial = target.with_name(target.name + ".part")
    try:
        with urllib.request.urlopen(url) as response, partial.open("wb") as out:
            shutil.copyfileobj(response, out)
    except OSError as exc:
        raise ExportError(f"cannot download {url}: {exc}") from exc
    partial.replace(target)


def _closure(lock: dict, names: tuple[str, ...]) -> list[dict]:
    """The named packages and everything they depend on, as lock entries."""
    packages = lock["packages"]
    seen: dict[str, dict] = {}
    pending = list(names)
    while pending:
        name = pending.pop()
        if name in seen:
            continue
        if name not in packages:
            raise ExportError(f"Pyodide {PYODIDE_VERSION} has no package {name!r}")
        seen[name] = packages[name]
        pending.extend(packages[name]["depends"])
    return list(seen.values())


def pyodide_files(cache: Path, fetch: Fetch = _download) -> list[Path]:
    """Pyodide core and the packages dechatter needs, fetched into `cache` when missing."""
    cache.mkdir(parents=True, exist_ok=True)
    for name in CORE:
        if not (cache / name).exists():
            fetch(PYODIDE_URL + name, cache / name)
    lock = json.loads((cache / LOCK).read_text(encoding="utf-8"))
    files = [cache / name for name in CORE]
    for package in _closure(lock, PACKAGES):
        target = cache / package["file_name"]
        if not target.exists():
            fetch(PYODIDE_URL + package["file_name"], target)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if digest != package["sha256"]:
            target.unlink()
            raise ExportError(f"{package['file_name']} does not match its checksum; run again")
        files.append(target)
    return files


def export(out: Path, cache: Path | None = None, fetch: Fetch = _download) -> None:
    out.mkdir(parents=True, exist_ok=True)
    widget = resources.files("dechatter.widget")
    for name in WIDGET_FILES:
        (out / name).write_bytes(widget.joinpath(name).read_bytes())

    package = resources.files("dechatter")
    with zipfile.ZipFile(out / SOURCE_ZIP, "w", zipfile.ZIP_DEFLATED) as archive:
        for source in sorted(package.iterdir(), key=lambda p: p.name):
            if source.name.endswith(".py"):
                archive.writestr(f"dechatter/{source.name}", source.read_bytes())

    (out / "pyodide").mkdir(exist_ok=True)
    for file in pyodide_files(cache or default_cache(), fetch):
        if file.name in MODULES:
            continue
        shutil.copyfile(file, out / "pyodide" / file.name)
    _write_modules(cache or default_cache(), out / "pyodide")


def _write_modules(cache: Path, out: Path) -> None:
    """Copy Pyodide's two ES modules under `.js` names.

    cncjs serves `.mjs` as application/octet-stream, and browsers refuse to run a module with
    that type. The loader names its companion module once; that one reference is rewritten.
    """
    loader = (cache / LOADER).read_text(encoding="utf-8")
    if loader.count(RUNTIME) != 1:
        raise ExportError(f"Pyodide {PYODIDE_VERSION}: {LOADER} no longer names {RUNTIME} once")
    renamed = loader.replace(RUNTIME, MODULES[RUNTIME])
    (out / MODULES[LOADER]).write_text(renamed, encoding="utf-8")
    shutil.copyfile(cache / RUNTIME, out / MODULES[RUNTIME])
