# -*- coding: utf-8 -*-
"""Offline, deterministic four-layer benchmark suite for examprep-agent.

Run everything with::

    python -m benchmarks.run_all --out benchmarks/results/

The package deliberately never imports a network provider in its default path.
Every model-facing arm uses a deterministic scripted/evidence reader so the
suite can run in CI without an API key.
"""

DEFAULT_SEED = 20261030

__all__ = ["DEFAULT_SEED"]
