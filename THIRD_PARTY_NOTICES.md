# Third-party software in the npm package

dechatter's own code is MIT licensed (see `LICENSE`). The npm package also redistributes the
software below in `web/pyodide/`, so that the widget runs in a browser tab without a server.
The exact versions are the ones pinned in `src/dechatter/export.py` and listed in
`web/pyodide/pyodide-lock.json`.

| Files | Software | Licence | Source |
| --- | --- | --- | --- |
| `pyodide.js`, `pyodide.asm.js`, `pyodide.asm.wasm`, `pyodide-lock.json` | Pyodide | MPL-2.0 | https://github.com/pyodide/pyodide |
| `python_stdlib.zip`, and the interpreter inside `pyodide.asm.wasm` | CPython | PSF-2.0 | https://github.com/python/cpython |
| `numpy-*.whl` | NumPy | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0, as the wheel declares | https://github.com/numpy/numpy |
| `scipy-*.whl` | SciPy | BSD-3-Clause, with bundled components under their own licences | https://github.com/scipy/scipy |

Two of the files are renamed copies: `pyodide.js` and `pyodide.asm.js` are Pyodide's
`pyodide.mjs` and `pyodide.asm.mjs`, and the one reference between them is renamed to match,
because cncjs serves `.mjs` files with a type browsers refuse to run. Nothing else is changed.

The Pyodide runtime also contains the libraries it is built with; their licence texts are in
the Pyodide repository. Each wheel is a zip archive and carries the licence texts of its
project and of what it bundles: in `*.dist-info/` and beside the bundled components.

The source code for the Pyodide files, which the MPL-2.0 asks to be made available, is at the
repository above under the tag of the pinned version.
