from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time, timedelta
from typing import Callable, List, Optional, Union

import typer
from typing_extensions import Annotated
from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.backends.service import BleakGATTCharacteristic  # type: ignore
from bleak.backends.service import BleakGATTServiceCollection
from bleak.exc import BleakDBusError
from bleak_retry_connector import (
    BLEAK_RETRY_EXCEPTIONS as BLEAK_EXCEPTIONS,
    BleakClientWithServiceCache,
    BleakError,  # type: ignore
    BleakNotFoundError,
    establish_connection,
    retry_bluetooth_connection_error,
)

# local protocol + commands
from ..const import UART_RX_CHAR_UUID, UART_TX_CHAR_UUID
from ..weekday_encoding import WeekdaySelect, encode_selected_weekdays
from .. import commands as led_cmds         # time sync etc. come from the shared LED cmds
from .. import dosingcommands               # you said: dosingpump.py -> dosingcommands.py

# 5B probe helpers
from ..protocol import (
    _split_ml_25_6,
    UART_TX,
    UART_RX,
    parse_totals_frame,
    build_totals_query_5b,
)

app = typer.Typer(help="Chihiros doser control")

__all__ = ["DoserDevice", "app", "_resolve_ble_or_fail", "_handle_connect_errors"]

# ─────────────────────────────────────────────────────────────
# Small utilities
# ─────────────────────────────────────────────────────────────

DEFAULT_ATTEMPTS = 3
DISCONNECT_DELAY = 120
BLEAK_BACKOFF_TIME = 0.25

NOT_FOUND_MSG = "Device Not Found, Unreachable or Failed to Connect, ensure Chihiro's App is not connected"


async def _resolve_ble_or_fail(device_address: str) -> BLEDevice:
    ble = await BleakScanner.find_device_by_address(device_address, timeout=10.0)
    if not ble:
        typer.echo(NOT_FOUND_MSG)
        raise typer.Exit(1)
    return ble


def _handle_connect_errors(ex: Exception) -> None:
    from bleak.exc import BleakDeviceNotFoundError
    msg = str(ex).lower()
    if (
        isinstance(ex, BleakDeviceNotFoundError)
        or "not found" in msg
        or "unreachable" in msg
        or "failed to connect" in msg
    ):
        typer.echo(NOT_FOUND_MSG)
        raise typer.Exit(1)
    raise ex


def _mk_ble_device(addr_or_ble: Union[BLEDevice, str]) -> BLEDevice:
    if isinstance(addr_or_ble, BLEDevice):
        return addr_or_ble
    mac = str(addr_or_ble).upper()
    return BLEDevice(mac, None, 0)


# ─────────────────────────────────────────────────────────────
# Doser “Base” (self-contained; does NOT auto-subscribe notify)
# ─────────────────────────────────────────────────────────────

class _DoserBase:
    _model_name: str | None = None
    _model_codes: list[str] = []

    def __init__(
        self,
        ble_device: Union[BLEDevice, str],
        advertisement_data: AdvertisementData | None = None,
    ) -> None:
        self._ble_device = _mk_ble_device(ble_device)
        self._advertisement_data = advertisement_data
        self._logger = logging.getLogger(self._ble_device.address.replace(":", "-"))

        self._client: BleakClientWithServiceCache | None = None
        self._connect_lock = asyncio.Lock()
        self._operation_lock = asyncio.Lock()
        self._disconnect_timer: asyncio.TimerHandle | None = None
        self._expected_disconnect = False
        self._read_char: BleakGATTCharacteristic | None = None
        self._write_char: BleakGATTCharacteristic | None = None

        # notify fan-out (used by doser-specific start_notify)
        self._notify_callbacks: list[Callable[[BleakGATTCharacteristic, bytearray], None]] = []
        self._notify_active = False

        # message id for A5/5B protocols
        self._msg_id = led_cmds.next_message_id()

        self.loop = asyncio.get_running_loop()
        assert self._model_name is not None

    # -------- identity / logging

    def set_log_level(self, level: int | str) -> None:
        if isinstance(level, str):
            level = logging._nameToLevel.get(level, 20)
        self._logger.setLevel(level)

    @property
    def address(self) -> str:
        return self._ble_device.address

    @property
    def name(self) -> str:
        if hasattr(self._ble_device, "name"):
            return self._ble_device.name or self._ble_device.address
        return self._ble_device.address

    @property
    def rssi(self) -> int | None:
        if self._advertisement_data:
            return self._advertisement_data.rssi
        return None

    # -------- msg id

    @property
    def current_msg_id(self) -> tuple[int, int]:
        return self._msg_id

    def get_next_msg_id(self) -> tuple[int, int]:
        self._msg_id = led_cmds.next_message_id(self._msg_id)
        return self._msg_id

    # -------- connect / resolve chars

    def _disconnected(self, _client: BleakClientWithServiceCache) -> None:
        if self._expected_disconnect:
            self._logger.debug("%s: Disconnected; RSSI: %s", self.name, self.rssi)
            return
        self._logger.warning("%s: Unexpected disconnect; RSSI: %s", self.name, self.rssi)

    def _resolve_characteristics(self, services: BleakGATTServiceCollection) -> bool:
        # TX (notify/read)
        self._read_char = None
        if ch := services.get_characteristic(UART_TX_CHAR_UUID):
            self._read_char = ch
        # RX (write)
        self._write_char = None
        if ch := services.get_characteristic(UART_RX_CHAR_UUID):
            self._write_char = ch
        return bool(self._read_char and self._write_char)

    async def _ensure_connected(self) -> None:
        if self._client and self._client.is_connected:
            self._reset_disconnect_timer()
            return

        if self._connect_lock.locked():
            self._logger.debug("%s: connect in progress; RSSI: %s", self.name, self.rssi)

        async with self._connect_lock:
            if self._client and self._client.is_connected:
                self._reset_disconnect_timer()
                return

            self._logger.debug("%s: Connecting; RSSI: %s", self.name, self.rssi)

            client = await establish_connection(
                BleakClientWithServiceCache,
                self._ble_device,           # pass BLEDevice (not address str)
                self.name,
                self._disconnected,
                use_services_cache=True,
                ble_device_callback=lambda: self._ble_device,  # type: ignore[return-value]
            )

            services = client.services or await client.get_services()
            if not self._resolve_characteristics(services):
                await client.disconnect()
                raise CharacteristicMissingError("Failed to resolve UART characteristics")

            self._client = client
            self._reset_disconnect_timer()

            # NOTE: we intentionally do NOT auto start_notify here.
            self._notify_active = False

            self._logger.debug("%s: Connected; RSSI: %s", self.name, self.rssi)

    # -------- notify (doser-specific)

    def add_notify_callback(self, cb: Callable[[BleakGATTCharacteristic, bytearray], None]) -> None:
        if cb not in self._notify_callbacks:
            self._notify_callbacks.append(cb)

    def remove_notify_callback(self, cb: Callable[[BleakGATTCharacteristic, bytearray], None]) -> None:
        try:
            self._notify_callbacks.remove(cb)
        except ValueError:
            pass

    async def start_notify_tx(self) -> None:
        """Doser-specific: begin notifications on TX with our own fan-out handler."""
        await self._ensure_connected()
        assert self._client is not None and self._read_char is not None  # nosec
        if self._notify_active:
            return

        def _dispatch(sender: BleakGATTCharacteristic, data: bytearray) -> None:
            # keep debug (not warning) to avoid log spam
            self._logger.debug("%s: Notify %s", self.name, data.hex(" ").upper())
            for cb in tuple(self._notify_callbacks):
                try:
                    cb(sender, data)
                except Exception:
                    self._logger.debug("%s: notify cb error", self.name, exc_info=True)

        await self._client.start_notify(self._read_char, _dispatch)
        self._notify_active = True

    async def stop_notify_tx(self) -> None:
        await self._ensure_connected()
        assert self._client is not None and self._read_char is not None  # nosec
        if not self._notify_active:
            return
        try:
            await self._client.stop_notify(self._read_char)
        except Exception:
            self._logger.debug("%s: stop_notify failed (already stopped?)", self.name, exc_info=True)
        self._notify_active = False

    # -------- write helpers

    async def _send_command(self, commands: List[bytes] | bytes | bytearray, retry: int | None = None) -> None:
        await self._ensure_connected()
        if not isinstance(commands, list):
            commands = [commands]
        await self._send_command_while_connected(commands, retry)

    async def _send_command_while_connected(self, commands: List[bytes], _retry: int | None = None) -> None:
        self._logger.debug("%s: Send %s", self.name, [c.hex() for c in commands])
        if self._operation_lock.locked():
            self._logger.debug("%s: Operation in progress; RSSI: %s", self.name, self.rssi)
        async with self._operation_lock:
            try:
                await self._send_command_locked(commands)
                return
            except BleakNotFoundError:
                self._logger.error("%s: device not found/poor RSSI: %s", self.name, self.rssi, exc_info=True)
                raise
            except CharacteristicMissingError as ex:
                self._logger.debug("%s: characteristic missing: %s", self.name, ex, exc_info=True)
                raise
            except BLEAK_EXCEPTIONS:
                self._logger.debug("%s: communication failed", self.name, exc_info=True)
                raise

        raise RuntimeError("Unreachable")

    @retry_bluetooth_connection_error(DEFAULT_ATTEMPTS)
    async def _send_command_locked(self, commands: List[bytes]) -> None:
        try:
            await self._execute_command_locked(commands)
        except BleakDBusError as ex:
            await asyncio.sleep(BLEAK_BACKOFF_TIME)
            self._logger.debug("%s: Backing off %ss; disconnect due to: %s", self.name, BLEAK_BACKOFF_TIME, ex)
            await self._execute_disconnect()
            raise
        except BleakError as ex:
            self._logger.debug("%s: Disconnect due to error: %s", self.name, ex)
            await self._execute_disconnect()
            raise

    async def _execute_command_locked(self, commands: List[bytes]) -> None:
        assert self._client is not None and self._write_char is not None  # nosec
        for command in commands:
            await self._client.write_gatt_char(self._write_char, command, False)

    # -------- connect/disconnect public

    async def connect(self) -> BleakClientWithServiceCache:
        await self._ensure_connected()
        assert self._client is not None  # nosec
        return self._client

    @property
    def client(self) -> BleakClientWithServiceCache | None:
        return self._client

    def _reset_disconnect_timer(self) -> None:
        if self._disconnect_timer:
            self._disconnect_timer.cancel()
        self._expected_disconnect = False
        self._disconnect_timer = self.loop.call_later(DISCONNECT_DELAY, self._disconnect)

    async def disconnect(self) -> None:
        self._logger.debug("%s: Disconnecting", self.name)
        await self._execute_disconnect()

    async def _execute_disconnect(self) -> None:
        async with self._connect_lock:
            read_char = self._read_char
            client = self._client

            self._expected_disconnect = True
            self._client = None
            self._read_char = None
            self._write_char = None

            # clear listeners so they never leak across sessions
            self._notify_callbacks.clear()
            self._notify_active = False

            if client and client.is_connected:
                if read_char:
                    try:
                        await client.stop_notify(read_char)
                    except Exception:
                        self._logger.debug("%s: stop_notify failed", self.name, exc_info=True)
                await client.disconnect()

    def _disconnect(self) -> None:
        self._disconnect_timer = None
        asyncio.create_task(self._execute_timed_disconnect())

    async def _execute_timed_disconnect(self) -> None:
        self._logger.debug("%s: Disconnect after idle %s", self.name, DISCONNECT_DELAY)
        await self._execute_disconnect()


# ─────────────────────────────────────────────────────────────
# DoserDevice: user-facing API built on _DoserBase
# ─────────────────────────────────────────────────────────────

class DoserDevice(_DoserBase):
    """Doser-specific commands mixed onto the self-contained BLE base."""

    _model_name = "Doser"
    _model_codes = ["DYDOSED", "DYDOSE", "DOSER"]

    def __init__(self, device_or_addr: BLEDevice | str) -> None:
        super().__init__(device_or_addr)

    # ------- small helpers

    @staticmethod
    def _add_minutes(t: time, delta_min: int) -> time:
        anchor = datetime(2000, 1, 1, t.hour, t.minute)
        return (anchor + timedelta(minutes=delta_min)).time()

    @classmethod
    async def connect_or_exit(cls, device_address: str) -> "DoserDevice":
        try:
            ble = await _resolve_ble_or_fail(device_address)
            dev = cls(ble)
            await dev.connect()
            return dev
        except Exception as ex:
            _handle_connect_errors(ex)
            raise

    # ------- raw + dosing ops

    async def raw_dosing_pump(
        self,
        cmd_id: int,
        mode: int,
        params: Optional[List[int]] = None,
        repeats: int = 3,
    ) -> None:
        p = params or []
        pkt = dosingcommands._create_command_encoding_dosing_pump(  # type: ignore[attr-defined]
            cmd_id, mode, self.get_next_msg_id(), p
        )
        await self._send_command(pkt, repeats)

    async def set_dosing_pump_manuell_ml(self, ch_id: int, ch_ml: float) -> None:
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
        synced and auto mode is active. Chooses byte (×10) or hi/lo(25.6+0.1) payload.
        """

        # Prelude: acks + time sync + ensure auto mode on this channel
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

        # Decide payload layout
        if prefer_hilo is True:
            use_hilo = True
        elif prefer_hilo is False:
            use_hilo = False
        else:
            use_hilo = ch_ml_tenths > 255  # >25.5 mL/day -> hi/lo recommended

        if use_hilo:
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
                # Fallback to byte layout if helper not present
                set_time0 = dosingcommands.create_auto_mode_dosing_pump_command_time(
                    performance_time, self.get_next_msg_id(), ch_id, enabled=True
                )
                await self._send_command(set_time0, 3)

                add = dosingcommands.create_add_auto_setting_command_dosing_pump(
                    performance_time, self.get_next_msg_id(), ch_id, weekdays_mask, ch_ml_tenths
                )
                await self._send_command(add, 3)
        else:
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
                pkt = dosingcommands.create_add_auto_setting_command_dosing_pump(
                    performance_time, self.get_next_msg_id(), ch_id, weekdays_mask, ch_ml_tenths
                )
            await self._send_command(pkt, 3)

    async def enable_auto_mode_dosing_pump(self, ch_id: int) -> None:
        switch_cmd = dosingcommands.create_switch_to_auto_mode_dosing_pump_command(self.get_next_msg_id(), ch_id)
        time_cmd = led_cmds.create_set_time_command(self.get_next_msg_id())
        await self._send_command(switch_cmd, 3)
        await self._send_command(time_cmd, 3)

    # ------- totals probe using doser-specific notify

    async def read_daily_totals(self, timeout_s: float = 6.0, mode_5b: int | str = 0x22) -> Optional[List[float]]:
        """
        Send a LED-style (0x5B) totals request and return [CH1..CH4] totals in mL,
        or None if no parseable frame arrives before timeout.
        """
        # accept "0x22"/"34" strings too
        if isinstance(mode_5b, str):
            mode_5b = int(mode_5b, 0)

        got: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

        def _on(payload_char: BleakGATTCharacteristic, payload: bytearray) -> None:
            vals = parse_totals_frame(payload)
            if vals and not got.done():
                got.set_result(bytes(payload))

        await self.connect()
        self.add_notify_callback(_on)
        try:
            await self.start_notify_tx()

            frame = build_totals_query_5b(mode_5b)
            try:
                assert self.client is not None  # nosec
                await self.client.write_gatt_char(UART_RX, frame, response=True)
            except Exception:
                await self.client.write_gatt_char(UART_TX, frame, response=True)

            try:
                payload = await asyncio.wait_for(got, timeout=timeout_s)
                return parse_totals_frame(payload)  # type: ignore[return-value]
            except asyncio.TimeoutError:
                return None
        finally:
            # always detach listener; keep notify running decisions to caller as needed
            self.remove_notify_callback(_on)
            try:
                await self.stop_notify_tx()
            except Exception:
                pass

    # ------- placeholders (wire up later if you like)

    async def read_dosing_pump_auto_settings(self, ch_id: int | None = None, timeout_s: float = 2.0) -> None:
        typer.echo("read_dosing_pump_auto_settings: query/parse not implemented yet.")

    async def read_dosing_container_status(self, ch_id: int | None = None, timeout_s: float = 2.0) -> None:
        typer.echo("read_dosing_container_status: query/parse not implemented yet.")


# ─────────────────────────────────────────────────────────────
# Optional: tiny CLI test hooks (kept minimal)
# ─────────────────────────────────────────────────────────────

@app.command("probe-totals")
def cli_probe_totals(
    device_address: Annotated[str, typer.Argument(help="BLE MAC, e.g. AA:BB:CC:DD:EE:FF")],
    timeout_s: Annotated[float, typer.Option(help="Listen timeout seconds", min=0.5)] = 6.0,
    mode_5b: Annotated[str, typer.Option(help="0x5B mode (e.g. 0x22 or 0x1E)")]= "0x22",
) -> None:
    async def run():
        dd: DoserDevice | None = None
        try:
            ble = await _resolve_ble_or_fail(device_address)
            dd = DoserDevice(ble)
            vals = await dd.read_daily_totals(timeout_s=timeout_s, mode_5b=mode_5b)
            if vals and len(vals) >= 4:
                typer.echo(
                    f"Totals (ml): CH1={vals[0]:.2f}, CH2={vals[1]:.2f}, "
                    f"CH3={vals[2]:.2f}, CH4={vals[3]:.2f}"
                )
            else:
                typer.echo("No totals frame received within timeout.")
        finally:
            if dd:
                await dd.disconnect()
    asyncio.run(run())


if __name__ == "__main__":
    app()
