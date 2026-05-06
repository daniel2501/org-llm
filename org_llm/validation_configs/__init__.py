"""Registered ValidationConfig instances for org-llm features.

Each module in this package registers one or more configs at import time.
The registry is read by `org-llm validate` (CLI) and by any future agents
that surface validation status (e.g. the proposed `doctor -w` extension
for stale-validation alerts).

Add a new feature: create `<name>.py` here that constructs and registers
its config; import it from this `__init__` so the side-effect happens
at package load.
"""
from __future__ import annotations

# Side-effect imports — each registers its config(s).
from . import recipes    # noqa: F401
from . import inferrers  # noqa: F401
