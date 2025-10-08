from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from typing import List, Optional

import typer
from typing_extensions import Annotated
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.exc import BleakDeviceNotFoundError, BleakError

# 👈 go up to the common LED package (shared BaseDevice, time sync, weekday utils)
from ...chihiros_led_control.device.base_device import BaseDevice
from ...chihiros_led_control import commands as led_cmds

# 👇 helpers live next door in mains/
from .. import dosingcommands

# 👇 protocol bits (alias build_totals_query -> build_totals_query_5b for clarity)
from ..protocol import (
    _split_ml_25_6,
    UART_TX,
    UART_RX,
    parse_totals_frame,
    build_totals_query_5b,
)

app = typer.Typer(help="Chihiros doser control")

# Make helpers importable/re-exportable
__all__ = ["DoserDevice", "app", "_resolve_ble_or_fail", "_handle_connect_errors"]

# ─────────────────────────────────────────────────────────────────────
# Device class
# ─────────────────────────────────────────────────────────────────────
class DoserDevice(BaseDevice):
    """Doser-specific commands mixed onto the common BLE BaseDevice."""

    _model_name = "Doser"
    _model_codes = ["DYDOSED", "DYDOSE", "DOSER"]
    _colors: dict[str, int] = {}

    def __init__(self, device_or_addr: BLEDevice | str) -> None:
        # BaseDevice handles BLEDevice vs string internally
        super().__init__(device_or_addr)

    @staticmethod
    def _add_minutes(t: time, delta_min: int) -> time:
        anchor = datetime(2000, 1, 1, t.hour, t.minute)
        return (anchor + timedelta(minutes=delta_min)).time()

    @classmethod
    async def connect_or_exit(cls, device_address: str) -> "DoserDevice":
        """
        Resolve BLE address to a device, construct a DoserDevice, connect,
        and return the instance. On common connection failures, print a friendly
        message and exit (via _handle_connect_errors).
        """
        try:
            ble = await _resolve_ble_or_fail(device_address)
            dev = cls(ble)
            await dev.connect()
            return dev
        except Exception as ex:
            _handle_connect_errors(ex)  # may typer.Exit(1) on common failures
            raise  # for type checkers; not reached if we exited above

    async def raw_dosing_pump(
        self,
        cmd_id: int,
        mode: int,
        params: Optional[List[int]] = None,
        repeats: int = 3,
    ) -> None:
        """Send a raw A5 frame (165/…) with checksum and msg-id handled."""
        p = params or []
        pkt = dosingcommands._create_command_encoding_dosing_pump(  # type: ignore[attr-defined]
            cmd_id, mode, self.get_next_msg_id(), p
        )
        await self._send_command(pkt, repeats)

    async def set_dosing_pump_manuell_ml(self, ch_id: int, ch_ml: float) -> None:
        """Immediate dose on channel using 25.6-bucket + 0.1 remainder (single frame)."""
        hi, lo = _split_ml_25_6(ch_ml)
        cmd = dosingcommands.create_add_dosing_pump_command_manuell_ml(
            self.get_next_msg_id(), ch_id, hi, lo
        )
        await self._send_command(cmd, 3)

    async def add_setting_dosing_pump(
        self,
        performance_time: time,
        ch_id: int,
        weekdays_mask: int,
        ch_ml_tenths: int,
        prefer_hilo: bool | None = None,
    ) -> None:
        """
        Program one weekly 24h dose entry at HH:MM with amount, ensure device time is
        synced and auto mode is active.

        Frames sent (prelude stays the same):
          • 90/4, 165/4 preludes + two time-syncs
          • 165/32  (switch to auto with flags)

        Then we choose one of two schedule variants:

        Variant A — byte dose×10 (legacy/simple):
          • 165/21  (time reinforce: [ch, enable, HH, MM, 0, 0])
          • 165/27  (weekly schedule: [ch, mask, enable, HH, MM, dose×10])

        Variant B — hi/lo 25.6+0.1 (robust for larger daily mL):
          • 165/21  (time reinforce: [ch, enable, HH, MM, 0, 0])
          • 165/27  (weekly schedule: [ch, mask, enable, 0, hi25.6, lo0.1])

        Selection:
          - prefer_hilo == True  → Variant B
          - prefer_hilo == False → Variant A
          - prefer_hilo is None  → Variant B if ch_ml_tenths > 255 else Variant A
        """
        # ── Prelude: ack, time sync twice, more acks, then ensure auto mode on this channel ──
        prelude = [
            dosingcommands.create_order_confirmation(self.get_next_msg_id(), 90, 4, 1),
            led_cmds.create_set_time_command(self.get_next_msg_id()),
            led_cmds.create_set_time_command(self.get_next_msg_id()),
            dosingcommands.create_order_confirmation(self.get_next_msg_id(), 165, 4, 4),
            dosingcommands.create_order_confirmation(self.get_next_msg_id(), 165, 4, 5),
            dosingcommands.create_switch_to_auto_mode_dosing_pump_command(self.get_next_msg_id(), ch_id),
        ]
        for f in prelude:
            await self._send_command(f, 3)

        # ── Decide which payload layout to use ──
        if prefer_hilo is True:
            use_hilo = True
        elif prefer_hilo is False:
            use_hilo = False
        else:
            # auto: if more than 25.5 mL/day, fall back to hi/lo split
            use_hilo = ch_ml_tenths > 255

        if use_hilo:
            # Try the dedicated hi/lo weekly helper if present, else fallback to byte layout.
            create_hi_lo = getattr(dosingcommands, "create_schedule_weekly_hi_lo", None)
            if callable(create_hi_lo):
                daily_ml = ch_ml_tenths / 10.0
                frames = create_hi_lo(
                    performance_time=performance_time,
                    msg_id_time=self.get_next_msg_id(),
                    msg_id_amount=self.get_next_msg_id(),
                    ch_id=ch_id,
                    weekdays_mask=weekdays_mask,
                    daily_ml=daily_ml,
                    enabled=True,
                )
                for pkt in frames:
                    await self._send_command(pkt, 3)
            else:
                typer.echo(
                    "Note: hi/lo weekly helper not found; falling back to dose×10 byte layout."
                )
                # 165/21 time reinforce (enabled flag)
                set_time0 = dosingcommands.create_auto_mode_dosing_pump_command_time(
                    performance_time, self.get_next_msg_id(), ch_id, enabled=True
                )
                await self._send_command(set_time0, 3)

                # 165/27 weekly entry (dose×10)
                add = dosingcommands.create_add_auto_setting_command_dosing_pump(
                    performance_time, self.get_next_msg_id(), ch_id, weekdays_mask, ch_ml_tenths
                )
                await self._send_command(add, 3)
        else:
            # Variant A: byte dose×10 (legacy/simple)
            set_time0 = dosingcommands.create_auto_mode_dosing_pump_command_time(
                performance_time, self.get_next_msg_id(), ch_id, enabled=True
            )
            await self._send_command(set_time0, 3)

            add = getattr(dosingcommands, "create_schedule_weekly_byte_amount", None)
            if callable(add):
                pkt = add(
                    performance_time=performance_time,
                    msg_id=self.get_next_msg_id(),
                    ch_id=ch_id,
                    weekdays_mask=weekdays_mask,
                    daily_ml_tenths=int(ch_ml_tenths),
                    enabled=True,
                )
            else:
                # Back-compat with your existing helper name
                pkt = dosingcommands.create_add_auto_setting_command_dosing_pump(
                    performance_time, self.get_next_msg_id(), ch_id, weekdays_mask, ch_ml_tenths
                )
            await self._send_command(pkt, 3)

    async def enable_auto_mode_dosing_pump(self, ch_id: int) -> None:
        switch_cmd = dosingcommands.create_switch_to_auto_mode_dosing_pump_command(self.get_next_msg_id(), ch_id)
        time_cmd = led_cmds.create_set_time_command(self.get_next_msg_id())
        await self._send_command(switch_cmd, 3)
        await self._send_command(time_cmd, 3)

    # NEW: read daily totals with a 0x5B probe
    async def read_daily_totals(self, timeout_s: float = 6.0, mode_5b: int = 0x22) -> Optional[List[float]]:
        """
        Send a LED-style (0x5B) totals request and return [CH1..CH4] totals in mL,
        or None if no parseable frame arrives before timeout.
        """
        got: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

        def _on_notify(_char, payload: bytearray) -> None:
            try:
                vals = parse_totals_frame(payload)
                if vals and not got.done():
                    got.set_result(bytes(payload))
            except Exception:
                # ignore malformed
                pass

        client = await self.connect()  # BaseDevice.connect() should return a Bleak client
        try:
            await client.start_notify(UART_TX, _on_notify)

            frame = build_totals_query_5b(mode_5b)
            # Most firmwares accept writes on RX; some echo off TX. Try RX first.
            try:
                await client.write_gatt_char(UART_RX, frame, response=True)
            except Exception:
                await client.write_gatt_char(UART_TX, frame, response=True)

            try:
                payload = await asyncio.wait_for(got, timeout=timeout_s)
                return parse_totals_frame(payload)  # type: ignore[return-value]
            except asyncio.TimeoutError:
                return None
            finally:
                try:
                    await client.stop_notify(UART_TX)
                except Exception:
                    pass
        except (BleakDeviceNotFoundError, BleakError, OSError) as ex:
            _handle_connect_errors(ex)  # standardize friendly output + exit for common cases
            return None  # not reached if _handle_connect_errors exits

    # placeholders for later
    async def read_dosing_pump_auto_settings(self, ch_id: int | None = None, timeout_s: float = 2.0) -> None:
        typer.echo("read_dosing_pump_auto_settings: query/parse not implemented yet.")

    async def read_dosing_container_status(self, ch_id: int | None = None, timeout_s: float = 2.0) -> None:
        typer.echo("read_dosing_container_status: query/parse not implemented yet.")


# ─────────────────────────────────────────────────────────────────────
# Typer CLI wrappers (scan for device, then operate)
# ─────────────────────────────────────────────────────────────────────
NOT_FOUND_MSG = "Device Not Found, Unreachable or Failed to Connect, ensure Chihiro's App is not connected"


async def _resolve_ble_or_fail(device_address: str) -> BLEDevice:
    ble = await BleakScanner.find_device_by_address(device_address, timeout=10.0)
    if not ble:
        typer.echo(NOT_FOUND_MSG)
        raise typer.Exit(1)
    return ble


def _handle_connect_errors(ex: Exception) -> None:
    # Normalize all "not found / unreachable" style errors to the user-friendly message
    msg = str(ex).lower()
    if (
        isinstance(ex, BleakDeviceNotFoundError)
        or "not found" in msg
        or "unreachable" in msg
        or "failed to connect" in msg
    ):
        typer.echo(NOT_FOUND_MSG)
        raise typer.Exit(1)
    # Otherwise re-raise to show the real error
    raise ex


if __name__ == "__main__":
    app()
