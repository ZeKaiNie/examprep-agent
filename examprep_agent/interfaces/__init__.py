# -*- coding: utf-8 -*-
"""Optional transports over the shared service layer.

Both modules import their third-party dependency lazily, so importing this
package (or either module) never fails when the optional extras are absent;
calling the factory raises :class:`~examprep_agent.service.errors.ExtrasMissing`
with the exact ``pip install`` hint instead.
"""
from __future__ import annotations

from ..service.errors import ExtrasMissing


def http_available() -> bool:
    """True when FastAPI/httpx extras are importable."""
    try:
        import fastapi  # noqa: F401
        import httpx  # noqa: F401
    except ImportError:
        return False
    return True


def mcp_available() -> bool:
    """True when the MCP SDK extra is importable."""
    try:
        import mcp  # noqa: F401
    except ImportError:
        return False
    return True


__all__ = ["ExtrasMissing", "http_available", "mcp_available"]
