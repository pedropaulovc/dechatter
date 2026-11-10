"""The exported widget folder, with the Pyodide download replaced by a local fake."""

from __future__ import annotations

import hashlib
import json
import zipfile

import pytest

from dechatter import export

WHEELS = {"numpy": b"numpy wheel", "scipy": b"scipy wheel", "unrelated": b"other wheel"}
DEPENDS = {"numpy": [], "scipy": ["numpy"], "unrelated": []}


class Cdn:
    """Serves a miniature Pyodide release and counts what was asked for."""

    def __init__(self) -> None:
        self.fetched: list[str] = []
        packages = {
            name: {
                "file_name": f"{name}.whl",
                "depends": DEPENDS[name],
                "sha256": hashlib.sha256(data).hexdigest(),
            }
            for name, data in WHEELS.items()
        }
        self.files = {f"{name}.whl": data for name, data in WHEELS.items()}
        self.files[export.LOCK] = json.dumps({"packages": packages}).encode()
        self.files[export.LOADER] = b'import("./pyodide.asm.mjs")'
        self.files[export.RUNTIME] = b"runtime"
        self.files["pyodide.asm.wasm"] = b"wasm"
        self.files["python_stdlib.zip"] = b"stdlib"

    def fetch(self, url: str, target) -> None:
        name = url.removeprefix(export.PYODIDE_URL)
        self.fetched.append(name)
        target.write_bytes(self.files[name])


def test_export_is_self_contained_and_reuses_the_cache(tmp_path):
    cdn = Cdn()
    out = tmp_path / "widget"
    export.export(out, tmp_path / "cache", cdn.fetch)

    served = {p.name for p in (out / "pyodide").iterdir()}
    assert served == {
        "pyodide.js",
        "pyodide.asm.js",
        "pyodide.asm.wasm",
        "python_stdlib.zip",
        export.LOCK,
        "numpy.whl",
        "scipy.whl",
    }
    # cncjs cannot serve .mjs as a module, so the loader must point at the renamed runtime.
    assert (out / "pyodide" / "pyodide.js").read_text() == 'import("./pyodide.asm.js")'
    for name in export.WIDGET_FILES:
        assert (out / name).stat().st_size > 0
    with zipfile.ZipFile(out / export.SOURCE_ZIP) as archive:
        assert {"dechatter/browser.py", "dechatter/detect.py"} <= set(archive.namelist())

    cdn.fetched.clear()
    export.export(tmp_path / "again", tmp_path / "cache", cdn.fetch)
    assert cdn.fetched == []


def test_corrupt_download_is_refused_and_fetched_again_next_time(tmp_path):
    cdn = Cdn()
    good = cdn.files["scipy.whl"]
    cdn.files["scipy.whl"] = b"truncated"
    with pytest.raises(export.ExportError, match="checksum"):
        export.export(tmp_path / "widget", tmp_path / "cache", cdn.fetch)

    cdn.files["scipy.whl"] = good
    export.export(tmp_path / "widget", tmp_path / "cache", cdn.fetch)
    assert (tmp_path / "widget" / "pyodide" / "scipy.whl").read_bytes() == good
