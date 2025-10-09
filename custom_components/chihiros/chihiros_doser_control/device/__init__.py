# custom_components/chihiros/chihiros_doser_control/device/__init__.py
"""Doser device package."""
from __future__ import annotations

from .doser_device import (  # noqa: F401
    DoserDevice,
    app,
    _resolve_ble_or_fail,
    _handle_connect_errors,
)

# Make the discovery model optional — only export if present
try:
    from .doser import Doser  # noqa: F401
    _HAS_DOSER_MODEL = True
except Exception:  # ModuleNotFoundError or any import error
    Doser = None  # type: ignore
    _HAS_DOSER_MODEL = False

__all__ = [
    "DoserDevice",
    "app",
    "_resolve_ble_or_fail",
    "_handle_connect_errors",
]

if _HAS_DOSER_MODEL:
    __all__.append("Doser")
