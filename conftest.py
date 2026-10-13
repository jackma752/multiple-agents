"""Pytest root configuration.

Puts the various package directories on ``sys.path`` and points the worker's
``PYTHONPATH`` so that ``agenthub``, ``agenthub_adapters``, ``agenthub_kernel``
and ``agenthub_rt`` are importable without an editable install.
"""

import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))

# Parent dirs that contain top-level importable packages.
for sub in (
    "packages/contracts",
    "packages/adapters",
    "apps/worker",
    "apps/api",
):
    p = os.path.join(ROOT, sub)
    if p not in sys.path:
        sys.path.insert(0, p)

# Allow `from tests.helpers import ...` with `tests` as a package.
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
