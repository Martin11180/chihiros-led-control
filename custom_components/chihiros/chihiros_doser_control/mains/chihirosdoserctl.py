"""Expose the doser Typer app for mounting under chihirosctl.

This file re-exports the Typer `app` defined in
`custom_components.chihiros.chihiros_doser_control/device/doser_device.py`
and *extends the same app instance* with a few extra helper commands.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import List

import typer
from typing_extensions import Annotated
from bleak.exc import BleakDeviceNotFoundError, BleakError

# Weekday helpers (shared with LED package)
from ...chihiros_led_control.helper.weekday_encoding import (WeekdaySelect,encode_selected_weekdays,)

# Import the existing doser CLI app and the device class
from .doser_device import app as app
from .doser_device import DoserDevice, _resolve_ble_or_fail  # used by helpers

# Protocol bits for utilities / probes / decode→state→ctl
from .protocol import (
    UART_TX,
    UART_RX,
    build_totals_query_5b,
    parse_totals_frame,
    parse_log_blob,
    decode_records,
    build_device_state,
    to_ctl_lines,
)

# ────────────────────────────────────────────────────────────────
# Simple READ helpers (call into the DoserDevice class)
# ────────────────────────────────────────────────────────────────

@app.command(name="read-dosing-auto")
def read_dosing_auto(
    device_address: Annotated[str, typer.Argument(help="BLE MAC, e.g. AA:BB:CC:DD:EE:FF")],
    ch_id: Annotated[int | None, typer.Option(help="Channel 0..3; omit for all")] = None,
    timeout_s: Annotated[float, typer.Option(help="Timeout seconds", min=0.1)] = 2.0,
) -> None:
    async def run():
        dd = DoserDevice(device_address)
        try:
            await dd.read_dosing_pump_auto_settings(ch_id=ch_id, timeout_s=timeout_s)
        finally:
            await dd.disconnect()
    import asyncio as _asyncio
    _asyncio.run(run())


@app.command(name="read-dosing-container")
def read_dosing_container(
    device_address: Annotated[str, typer.Argument(help="BLE MAC, e.g. AA:BB:CC:DD:EE:FF")],
    ch_id: Annotated[int | None, typer.Option(help="Channel 0..3; omit for all")] = None,
    timeout_s: Annotated[float, typer.Option(help="Timeout seconds", min=0.1)] = 2.0,
) -> None:
    async def run():
        dd = DoserDevice(device_address)
        try:
            await dd.read_dosing_container_status(ch_id=ch_id, timeout_s=timeout_s)
        finally:
            await dd.disconnect()
    import asyncio as _asyncio
    _asyncio.run(run())


@app.command("set-dosing-pump-manuell-ml")
def cli_set_dosing_pump_manuell_ml(
    device_address: Annotated[str, typer.Argument(help="BLE MAC, e.g. AA:BB:CC:DD:EE:FF")],
    ch_id: Annotated[int, typer.Option("--ch-id", help="Channel 0..3 (0≙CH1)", min=0, max=3)],
    ch_ml: Annotated[float, typer.Option("--ch-ml", help="Dose (mL)", min=0.2, max=999.9)],
):
    """Immediate one-shot dose."""
    async def run():
        dd: DoserDevice | None = None
        try:
            ble = await _resolve_ble_or_fail(device_address)
            dd = DoserDevice(ble)
            await dd.set_dosing_pump_manuell_ml(ch_id, ch_ml)
        except (BleakDeviceNotFoundError, BleakError, OSError):
            raise
        finally:
            if dd:
                await dd.disconnect()
    asyncio.run(run())


@app.command("enable-auto-mode-dosing-pump")
def cli_enable_auto_mode_dosing_pump(
    device_address: Annotated[str, typer.Argument(help="BLE MAC")],
    ch_id: Annotated[int, typer.Option("--ch-id", help="Channel 0..3 (0≙CH1)", min=0, max=3)] = 0,
):
    """Explicitly switch the doser channel to auto mode and sync time."""
    async def run():
        dd: DoserDevice | None = None
        try:
            ble = await _resolve_ble_or_fail(device_address)
            dd = DoserDevice(ble)
            await dd.enable_auto_mode_dosing_pump(ch_id)
        except (BleakDeviceNotFoundError, BleakError, OSError):
            raise
        finally:
            if dd:
                await dd.disconnect()
    asyncio.run(run())


@app.command("add-setting-dosing-pump")
def cli_add_setting_dosing_pump(
    device_address: Annotated[str, typer.Argument(help="BLE MAC")],
    performance_time: Annotated[datetime, typer.Argument(formats=["%H:%M"], help="HH:MM")],
    ch_id: Annotated[int, typer.Option("--ch-id", help="Channel 0..3 (0≙CH1)", min=0, max=3)],
    ch_ml: Annotated[float, typer.Option("--ch-ml", help="Daily dose mL", min=0.2, max=999.9)],
    weekdays: Annotated[List[WeekdaySelect], typer.Option(
        "--weekdays", "-w", help="Repeat days; can be passed multiple times", case_sensitive=False
    )] = [WeekdaySelect.everyday],
):
    """Add a 24h schedule entry at time with amount, on selected weekdays."""
    async def run():
        dd: DoserDevice | None = None
        try:
            ble = await _resolve_ble_or_fail(device_address)
            dd = DoserDevice(ble)
            mask = encode_selected_weekdays(weekdays)
            tenths = int(round(ch_ml * 10))
            await dd.add_setting_dosing_pump(performance_time.time(), ch_id, mask, tenths)
        except (BleakDeviceNotFoundError, BleakError, OSError):
            raise
        finally:
            if dd:
                await dd.disconnect()
    asyncio.run(run())


# ────────────────────────────────────────────────────────────────
# Probe: send a 0x5B totals query and print decoded totals
# ────────────────────────────────────────────────────────────────

@app.command("probe-totals")
def cli_probe_totals(
    device_address: Annotated[str, typer.Argument(help="BLE MAC, e.g. AA:BB:CC:DD:EE:FF")],
    timeout_s: Annotated[float, typer.Option(help="Listen timeout seconds", min=0.5)] = 6.0,
    mode_5b: Annotated[int, typer.Option(help="0x5B mode to use (e.g. 0x22 or 0x1E)")] = 0x22,
) -> None:
    """
    Send a LED-style (0x5B) totals request and print CH1..CH4 totals (mL) if received.
    """
    async def run():
        dd: DoserDevice | None = None
        got: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

        def _on_notify(_char, payload: bytearray) -> None:
            # Accept first parseable totals frame
            vals = parse_totals_frame(payload)
            if vals and not got.done():
                got.set_result(bytes(payload))

        try:
            ble = await _resolve_ble_or_fail(device_address)
            dd = DoserDevice(ble)
            client = await dd.connect()  # ensure connected; DoserDevice should return a Bleak client
            await client.start_notify(UART_TX, _on_notify)

            frame = build_totals_query_5b(mode_5b)
            # Most firmwares take writes on RX, but some echo off TX; try RX first.
            try:
                await client.write_gatt_char(UART_RX, frame, response=True)
            except Exception:
                # Fallback to TX if RX rejects writes on this device
                await client.write_gatt_char(UART_TX, frame, response=True)

            try:
                payload = await asyncio.wait_for(got, timeout=timeout_s)
                vals = parse_totals_frame(payload) or []
                if len(vals) >= 4:
                    typer.echo(
                        f"Totals (ml): CH1={vals[0]:.2f}, CH2={vals[1]:.2f}, "
                        f"CH3={vals[2]:.2f}, CH4={vals[3]:.2f}"
                    )
                else:
                    typer.echo("Totals received but could not parse 4 channels.")
            except asyncio.TimeoutError:
                typer.echo("No totals frame received within timeout.")
            finally:
                try:
                    await client.stop_notify(UART_TX)
                except Exception:
                    pass
        except (BleakDeviceNotFoundError, BleakError, OSError):
            raise
        finally:
            if dd:
                await dd.disconnect()

    asyncio.run(run())


# ────────────────────────────────────────────────────────────────
# Raw A5 frame sender
# ────────────────────────────────────────────────────────────────

@app.command("raw-dosing-pump")
def cli_raw_dosing_pump(
    device_address: Annotated[str, typer.Argument(help="BLE MAC")],
    cmd_id: Annotated[int, typer.Option("--cmd-id", help="Command (e.g. 165)")],
    mode: Annotated[int, typer.Option("--mode", help="Mode (e.g. 27)")],
    # positional params come before any defaulted option to avoid Typer errors
    params: Annotated[List[int], typer.Argument(help="Parameter list, e.g. 0 0 14 2 0 0")],
    repeats: Annotated[int, typer.Option("--repeats", help="Send frame N times", min=1)] = 3,
):
    """Send a raw A5 frame: [cmd, 1, len, msg_hi, msg_lo, mode, *params, checksum]."""
    async def run():
        dd: DoserDevice | None = None
        try:
            ble = await _resolve_ble_or_fail(device_address)
            dd = DoserDevice(ble)
            await dd.raw_dosing_pump(cmd_id, mode, params, repeats)
        except (BleakDeviceNotFoundError, BleakError, OSError):
            raise
        finally:
            if dd:
                await dd.disconnect()
    asyncio.run(run())