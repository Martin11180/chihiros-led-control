# custom_components/chihiros/chihiros_led_control/chihirosctl.py
"""Chihiros led control CLI entrypoint."""

from __future__ import annotations

import asyncio
import inspect
from pathlib import Path
from datetime import datetime
from typing import Any

import typer
from typing_extensions import Annotated

from typing import Optional
from bleak import BleakScanner
from rich import print
from rich.table import Table
from typing_extensions import Annotated

from . import commands
from .device import get_device_from_address, get_model_class_from_name
from .weekday_encoding import WeekdaySelect

# Mount the doser Typer app under "doser"
# (use the thin shim so the import path stays stable)
# Make this import **robust** so LED CLI still works when doser deps/HA are missing.
try:
    from ..chihiros_doser_control.chihirosdoserctl import app as doser_app  # type: ignore
except Exception as _e:
    # Provide a small stub so `chihirosctl doser --help` is informative, not a crash.
    doser_app = typer.Typer(help="Doser commands unavailable")
    @doser_app.callback()
    def _doser_unavailable():
        typer.secho(
            "Doser CLI is unavailable in this environment.\n"
            "• Wireshark helpers are still available under: chihirosctl wireshark ...\n"
            "• To use doser commands without Home Assistant, ensure optional deps (e.g. bleak) are installed\n"
            "  or use the dedicated entry point: chihirosctl-lite (if configured in pyproject.toml).",
            fg=typer.colors.YELLOW,
        )

app = typer.Typer()
app.add_typer(doser_app, name="doser", help="Chihiros doser control")

# ────────────────────────────────────────────────────────────────
# Wireshark helpers (shared; no HA dependency)
# ────────────────────────────────────────────────────────────────
wireshark_app = typer.Typer(help="Wireshark helpers (parse/peek BLE ATT payloads)")
app.add_typer(wireshark_app, name="wireshark")

# Import the new shared helpers
try:
    from ..wireshark.wireshark_core import parse_wireshark_stream, write_jsonl  # type: ignore
except Exception as _e:
    parse_wireshark_stream = None  # type: ignore
    write_jsonl = None  # type: ignore

def _require_ws():
    if parse_wireshark_stream is None or write_jsonl is None:
        raise typer.Exit(code=2)

msg_id = commands.next_message_id()


def _run_device_func(device_address: str, **kwargs: Any) -> None:
    command_name = inspect.stack()[1][3]

    async def _async_func() -> None:
        dev = await get_device_from_address(device_address)
        if hasattr(dev, command_name):
            await getattr(dev, command_name)(**kwargs)
        else:
            print(f"{dev.__class__.__name__} doesn't support {command_name}")
            raise typer.Abort()

    asyncio.run(_async_func())

@wireshark_app.command("parse")
def wireshark_parse(
    infile: Annotated[Path, typer.Argument(exists=True, readable=True, help="Wireshark export (JSON array or NDJSON)")],
    outfile: Annotated[Path, typer.Option("--out", "-o", help="Output JSONL path (use '-' for stdout)")] = Path("-"),
    handle: Annotated[str, typer.Option(help="ATT handle to match (default Nordic UART TX 0x0010)")] = "0x0010",
    op: Annotated[str, typer.Option(help="ATT op filter: write|notify|any")] = "write",
    rx: Annotated[str, typer.Option(help="Include notifications: no|also|only")] = "no",
    pretty: Annotated[bool, typer.Option("--pretty/--no-pretty", help="Pretty JSONL (indented)")] = False,
) -> None:
    """
    Convert a Wireshark JSON export into JSON Lines of BLE ATT payloads.
    """
    _require_ws()
    try:
        with infile.open("r", encoding="utf-8") as f:
            rows = parse_wireshark_stream(f, handle=handle, op=op, rx=rx)  # type: ignore
            if str(outfile) == "-":
                import sys
                write_jsonl(rows, sys.stdout, pretty=pretty)  # type: ignore
            else:
                outfile.parent.mkdir(parents=True, exist_ok=True)
                with outfile.open("w", encoding="utf-8") as out:
                    write_jsonl(rows, out, pretty=pretty)  # type: ignore
    except Exception as e:
        raise typer.BadParameter(f"Parse failed: {e}") from e

@wireshark_app.command("peek")
def wireshark_peek(
    infile: Annotated[Path, typer.Argument(exists=True, readable=True, help="Wireshark export (JSON array or NDJSON)")],
    limit: Annotated[int, typer.Option("--limit", "-n", help="Number of frames to show", min=1)] = 12,
    handle: Annotated[str, typer.Option(help="ATT handle match (default 0x0010)")]= "0x0010",
    op: Annotated[str, typer.Option(help="ATT op filter: write|notify|any")] = "any",
    rx: Annotated[str, typer.Option(help="Include notifications: no|also|only")] = "also",
) -> None:
    """
    Show the first few normalized frames (ts, op, handle, len, hex…).
    """
    _require_ws()
    try:
        from rich.table import Table  # pretty if available
        from rich.console import Console
        has_rich = True
    except Exception:
        has_rich = False

    try:
        with infile.open("r", encoding="utf-8") as f:
            rows = parse_wireshark_stream(f, handle=handle, op=op, rx=rx)  # type: ignore
            shown = 0
            if has_rich:
                table = Table("idx", "time", "op", "handle", "len", "hex")
                for rec in rows:
                    shown += 1
                    if shown > limit:
                        break
                    table.add_row(
                        str(shown),
                        str(rec.get("ts", ""))[:23],
                        str(rec.get("att_op", "")),
                        str(rec.get("att_handle", "")),
                        str(rec.get("len", "")),
                        (rec.get("bytes_hex", "")[:64] + ("…" if rec.get("len", 0) > 32 else "")),
                    )
                Console().print(table)
            else:
                for rec in rows:
                    shown += 1
                    if shown > limit:
                        break
                    ts = str(rec.get("ts",""))
                    op = rec.get("att_op","")
                    h  = rec.get("att_handle","")
                    ln = rec.get("len","")
                    hx = rec.get("bytes_hex","")
                    print(f"[{shown:02d}] {ts}  {op}  handle={h}  len={ln}  hex={hx[:64]}{'…' if ln and ln>32 else ''}")
            if shown == 0:
                typer.secho("No matching frames.", fg=typer.colors.YELLOW)
    except Exception as e:
        raise typer.BadParameter(f"Peek failed: {e}") from e


@app.command()
def list_devices(timeout: Annotated[int, typer.Option()] = 5) -> None:
    """List all bluetooth devices.

    TODO: add an option to show only Chihiros devices
    """
    table = Table("Name", "Address", "Model")
    discovered_devices = asyncio.run(BleakScanner.discover(timeout=timeout))
    for device in discovered_devices:
        name = device.name or ""
        model_name = "???"
        if name:
            model_class = get_model_class_from_name(name)
            # Use safe getattr so we don't assume any class attributes exist
            model_name = getattr(model_class, "model_name", "???") or "???"
        table.add_row(name or "(unknown)", device.address, model_name)
    print("Discovered the following devices:")
    print(table)


@app.command()
def turn_on(device_address: str) -> None:
    """Turn on a light."""
    _run_device_func(device_address)


@app.command()
def turn_off(device_address: str) -> None:
    """Turn off a light."""
    _run_device_func(device_address)


@app.command()
def set_color_brightness(
    device_address: str,
    color: int,
    brightness: Annotated[int, typer.Argument(min=0, max=100)],
) -> None:
    """Set color brightness of a light."""
    _run_device_func(device_address, color=color, brightness=brightness)


@app.command()
def set_brightness(
    device_address: str, brightness: Annotated[int, typer.Argument(min=0, max=100)]
) -> None:
    """Set brightness of a light."""
    set_color_brightness(device_address, color=0, brightness=brightness)


@app.command()
def set_rgb_brightness(
    device_address: str, brightness: Annotated[tuple[int, int, int], typer.Argument()]
) -> None:
    """Set brightness of a RGB light."""
    _run_device_func(device_address, brightness=brightness)


@app.command()
def add_setting(
    device_address: str,
    sunrise: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
    sunset: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
    max_brightness: Annotated[int, typer.Option(max=100, min=0)] = 100,
    ramp_up_in_minutes: Annotated[int, typer.Option(min=0, max=150)] = 0,
    weekdays: Annotated[list[WeekdaySelect], typer.Option()] = [WeekdaySelect.everyday],
) -> None:
    """Add setting to a light."""
    _run_device_func(
        device_address,
        sunrise=sunrise,
        sunset=sunset,
        max_brightness=max_brightness,
        ramp_up_in_minutes=ramp_up_in_minutes,
        weekdays=weekdays,
    )


@app.command()
def add_rgb_setting(
    device_address: str,
    sunrise: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
    sunset: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
    max_brightness: Annotated[tuple[int, int, int], typer.Option()] = (100, 100, 100),
    ramp_up_in_minutes: Annotated[int, typer.Option(min=0, max=150)] = 0,
    weekdays: Annotated[list[WeekdaySelect], typer.Option()] = [WeekdaySelect.everyday],
) -> None:
    """Add setting to a RGB light."""
    _run_device_func(
        device_address,
        sunrise=sunrise,
        sunset=sunset,
        max_brightness=max_brightness,
        ramp_up_in_minutes=ramp_up_in_minutes,
        weekdays=weekdays,
    )


@app.command()
def remove_setting(
    device_address: str,
    sunrise: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
    sunset: Annotated[datetime, typer.Argument(formats=["%H:%M"])],
    ramp_up_in_minutes: Annotated[int, typer.Option(min=0, max=150)] = 0,
    weekdays: Annotated[list[WeekdaySelect], typer.Option()] = [WeekdaySelect.everyday],
) -> None:
    """Remove setting from a light."""
    _run_device_func(
        device_address,
        sunrise=sunrise,
        sunset=sunset,
        ramp_up_in_minutes=ramp_up_in_minutes,
        weekdays=weekdays,
    )


@app.command()
def reset_settings(device_address: str) -> None:
    """Reset settings from a light."""
    _run_device_func(device_address)


@app.command()
def enable_auto_mode(device_address: str) -> None:
    """Enable auto mode in a light."""
    _run_device_func(device_address)


if __name__ == "__main__":
    try:
        app()
    except asyncio.CancelledError:
        pass
