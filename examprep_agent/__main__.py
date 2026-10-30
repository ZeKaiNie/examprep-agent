# -*- coding: utf-8 -*-
"""Allow `python -m examprep_agent` to behave like the `examprep` console script."""

from .cli import main

if __name__ == "__main__":  # pragma: no cover - thin entry point
    raise SystemExit(main())
