# custom_components/chihiros/chihiros_doser_control/device/__init__.py
"""Doser device package."""
from __future__ import annotations

from .doser_device import (
    DoserDevice,
    app,
    _resolve_ble_or_fail,
    _handle_connect_errors,
)
from .doser import Doser  # <-- export the model used by discovery

__all__ = [
    "DoserDevice",
    "app",
    "_resolve_ble_or_fail",
    "_handle_connect_errors",
    "Doser",  # <-- include in public API
]
