# custom_components/chihiros/chihiros_doser_control/device/__init__.py
"""Doser device package."""
from __future__ import annotations

from .doser_device import DoserDevice, app  # re-export for convenience

__all__ = ["DoserDevice", "app"]