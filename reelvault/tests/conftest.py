"""Make the suite runnable from the repository root too (`pytest reelvault/tests` or `python -m pytest ...` from
/home/<you>/linkedin), not only from the project folder.

From the repository root, the project folder `reelvault/` (which has no __init__.py) is found first on sys.path and
Python imports it as an empty namespace package, so `from reelvault import __version__` fails in the desktop tests.
Putting the project folder first and dropping that namespace module makes `reelvault` the real package again.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]

if str(PROJECT) not in sys.path[:1]:
    sys.path.insert(0, str(PROJECT))
_pkg = sys.modules.get("reelvault")
if _pkg is not None and getattr(_pkg, "__file__", None) is None:   # the namespace package, not the real one
    for name in [m for m in sys.modules if m == "reelvault" or m.startswith("reelvault.")]:
        del sys.modules[name]

# A desktop-only install (install.ps1: no [server] extra) has no fastapi/sqlalchemy: skip the server-side suites
# instead of failing their collection (fix round 1; the Windows CI step also passes --ignore for them).
try:
    import fastapi  # noqa: F401
    import sqlalchemy  # noqa: F401
except ImportError:
    collect_ignore = ["server", "mcp", "web", "ext", "journey", "e2e"]
