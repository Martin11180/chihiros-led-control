from __future__ import annotations

import asyncio
import json
import logging
import os
from abc import ABC, ABCMeta
from datetime import datetime, time, timedelta
from enum import Enum
from pathlib import Path
from typing import Callable, List, Optional, Union

import typer
from typing_extensions import Annotated

from bleak import BleakScanner
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.backends.service import BleakGATTCharacteristic  # type: ignore
from bleak.backends.service import BleakGATTServiceCollection
from bleak.exc import BleakDBusError, BleakDeviceNotFoundError
from bleak_retry_connector import BLEAK_RETRY_EXCEPTIONS as BLEAK_EXCEPTIONS
from bleak_retry_connector import BleakError  # type: ignore
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    BleakNotFoundError,
    establish_connection,
    retry_bluetooth_connection_error,
)

# ─────────────────────────────────────────────────────────────────────
# INLINE REPLACEMENTS for LED package deps (const / weekday_encoding / exception)
# ─────────────────────────────────────────────────────────────────────

# Nordic UART UUIDs (same as your protocol file; keep here for Bleak service discovery)
UART_SERVICE_UUID     = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
UART_RX_CHAR_UUID     = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"  # write
UART_TX_CHAR_UUID     = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"  # notify

class CharacteristicMissingError(RuntimeError):
    """Raised when expected UART characteristics are not present."""

class WeekdaySelect(Enum):
    monday    = 64
    tuesday   = 32
    wednesday = 16
    thursday  = 8
    friday    = 4
    saturday  = 2
    sunday    = 1
    everyday  = 127

def encode_selected_weekdays(days: List[WeekdaySelect]) -> int:
    if not days:
        return 0
    if WeekdaySelect.everyday in days:
        return WeekdaySelect.everyday.value
    mask = 0
    for d in days:
        if isinstance(d, WeekdaySelect):
            mask |= d.value
        else:
            try:
                mask |= WeekdaySelect[str(d)].value  # type: ignore[index]
            except Exception:
                pass
    return mask & 0x7F

# ─────────────────────────────────────────────────────────────────────
# Use your existing helpers
# ─────────────────────────────────────────────────────────────────────
from .. import dosingcommands
from ..protocol import (
    _split_ml_25_6,
    UART_TX,            # TX/notify UUID string
    UART_RX,            # RX/write UUID string
    parse_totals_frame,
    build_totals_query_5b,
)

# CLI app
app = typer.Typer(help="Chihiros doser control")

# ─────────────────────────────────────────────────────────────────────
# Local JSON state (stand-in for app/server)
# ─────────────────────────────────────────────────────────────────────
class _DoserStateStore:
    """
    Persist per-device state:
      • manual dose history
      • per-channel container volumes (mL)
    File: ~/.chihiros_doser/state.json
    """
    def __init__(self) -> None:
        base = Path(os.path.expanduser("~")) / ".chihiros_doser"
        base.mkdir(parents=True, exist_ok=True)
        self._path = base / "state.json"
        if not self._path.exists():
            self._path.write_text("{}", encoding="utf-8")

    def _load(self) -> dict:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save(self, data: dict) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)

    def ensure_device(self, addr: str) -> dict:
        data = self._load()
        dev = data.setdefault(addr, {})
        dev.setdefault("containers", {str(i): 0.0 for i in range(4)})
        dev.setdefault("manual_history", [])
        self._save(data)
        return dev

    def record_manual(self, addr: str, ch: int, ml: float) -> None:
        data = self._load()
        dev = data.setdefault(addr, {})
        hist = dev.setdefault("manual_history", [])
        hist.append({"ts": datetime.utcnow().isoformat() + "Z", "ch": int(ch), "ml": float(ml)})
        self._save(data)

    def adjust_container(self, addr: str, ch: int, delta_ml: float) -> None:
        data = self._load()
        dev = data.setdefault(addr, {})
        cont = dev.setdefault("containers", {str(i): 0.0 for i in range(4)})
        key = str(int(ch))
        cont[key] = max(0.0, float(cont.get(key, 0.0)) + float(delta_ml))
        self._save(data)

    def set_container(self, addr: str, ch: int, ml: float) -> None:
        data = self._load()
        dev = data.setdefault(addr, {})
        cont = dev.setdefault("containers", {str(i): 0.0 for i in range(4)})
        cont[str(int(ch))] = max(0.0, float(ml))
        self._save(data)

    def get_containers(self, addr: str) -> dict[str, float]:
        return {k: float(v) for k, v in self.ensure_device(addr)["containers"].items()}

    def get_history(self, addr: str) -> list[dict]:
        dev = self.ensure_device(addr)
        return list(dev.get("manual_history", []))

    def clear_history(self, addr: str) -> None:
        data = self._load()
        dev = data.setdefault(addr, {})
        dev["manual_history"] = []
        self._save(data)

_STATE = _DoserStateStore()

# ─────────────────────────────────────────────────────────────────────
# Embedded BaseDevice (notify is explicit; not auto-started)
# ─────────────────────────────────────────────────────────────────────
DEFAULT_ATTEMPTS = 3
DISCONNECT_DELAY = 120
BLEAK_BACKOFF_TIME = 0.25


class _classproperty(property):
    def __get__(self, owner_self: object, owner_cls: ABCMeta) -> str:  # type: ignore
        ret: str = self.fget(owner_cls)  # type: ignore
        return ret


def _is_ha_runtime() -> bool:
    try:
        import homeassistant  # type: ignore
        return True
    except Exception:
        return False


def _mk_ble_device(addr_or_ble: Union[BLEDevice, str]) -> BLEDevice:
    if isinstance(addr_or_ble, BLEDevice):
        return addr_or_ble
    if _is_ha_runtime():
        raise RuntimeError("In Home Assistant, pass a real BLEDevice (not a MAC string).")
    mac = str(addr_or_ble).upper()
    return BLEDevice(mac, None, 0)


class BaseDevice(ABC):
    """Base device with connection + UART resolve + explicit notify fan-out."""

    _model_name: str | None = None
    _model_codes: list[str] = []
    _colors: dict[str, int] = {}
    _msg_id = dosingcommands.next_message_id() if hasattr(dosingcommands, "next_message_id") else (0, 0)
    _logger: logging.Logger

    def __init__(
        self,
        ble_device: Union[BLEDevice, str],
        advertisement_data: AdvertisementData | None = None,
    ) -> None:
        self._ble_device = _mk_ble_device(ble_device)
        self._logger = logging.getLogger(self._ble_device.address.replace(":", "-"))
        self._advertisement_data = advertisement_data
        self._client: BleakClientWithServiceCache | None = None
        self._disconnect_timer: asyncio.TimerHandle | None = None
        self._operation_lock: asyncio.Lock = asyncio.Lock()
        self._read_char: BleakGATTCharacteristic | None = None  # TX/notify
        self._write_char: BleakGATTCharacteristic | None = None  # RX/write
        self._connect_lock: asyncio.Lock = asyncio.Lock()
        self._expected_disconnect = False
        self.loop = asyncio.get_running_loop()

        self._notify_active: bool = False
        self._notify_callbacks: list[Callable[[BleakGATTCharacteristic, bytearray], None]] = []

        assert self._model_name is not None

    # ── info ──
    def set_log_level(self, level: int | str) -> None:
        if isinstance(level, str):
            level = logging._nameToLevel.get(level, 20)
        self._logger.setLevel(level)

    def set_ble_device_and_advertisement_data(
        self, ble_device: BLEDevice, advertisement_data: AdvertisementData
    ) -> None:
        self._ble_device = ble_device
        self._advertisement_data = advertisement_data

    @property
    def current_msg_id(self) -> tuple[int, int]:
        return self._msg_id

    def get_next_msg_id(self) -> tuple[int, int]:
        if hasattr(dosingcommands, "next_message_id"):
            self._msg_id = dosingcommands.next_message_id(self._msg_id)  # type: ignore
        else:
            hi, lo = self._msg_id
            lo = (lo + 1) & 0xFF
            if lo == 0:
                hi = (hi + 1) & 0xFF
            self._msg_id = (hi, lo)
        return self._msg_id

    @_classproperty
    def model_name(cls) -> str | None:  # type: ignore[override]
        return cls._model_name

    @_classproperty
    def model_codes(cls) -> list[str]:  # type: ignore[override]
        return cls._model_codes

    @property
    def colors(self) -> dict[str, int]:
        return self._colors

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

    # ── notify control ──
    async def start_notify_tx(self) -> None:
        await self._ensure_connected()
        if not self._read_char:
            raise CharacteristicMissingError("Read characteristic missing (UART TX)")
        if self._notify_active:
            return
        assert self._client is not None
        await self._client.start_notify(self._read_char, self._notification_handler)  # type: ignore[arg-type]
        self._notify_active = True

    async def stop_notify_tx(self) -> None:
        if not self._notify_active:
            return
        if self._client and self._read_char and self._client.is_connected:
            try:
                await self._client.stop_notify(self._read_char)
            except Exception:
                self._logger.debug("%s: stop_notify failed (already stopped?)", self.name, exc_info=True)
        self._notify_active = False

    def add_notify_callback(self, cb: Callable[[BleakGATTCharacteristic, bytearray], None]) -> None:
        if cb not in self._notify_callbacks:
            self._notify_callbacks.append(cb)

    def remove_notify_callback(self, cb: Callable[[BleakGATTCharacteristic, bytearray], None]) -> None:
        try:
            self._notify_callbacks.remove(cb)
        except ValueError:
            pass

    def _notification_handler(self, sender: BleakGATTCharacteristic, data: bytearray) -> None:
        # print(data.hex())  # uncomment for raw prints during debugging
        self._logger.debug("%s: Notification received: %s", self.name, data.hex(" ").upper())
        for cb in tuple(self._notify_callbacks):
            try:
                cb(sender, data)
            except Exception:
                self._logger.debug("%s: notify callback raised", self.name, exc_info=True)

    # ── BLE I/O ──
    async def _send_command(
        self, commands: list[bytes] | bytes | bytearray, retry: int | None = None
    ) -> None:
        await self._ensure_connected()
        if not isinstance(commands, list):
            commands = [commands]
        await self._send_command_while_connected(commands, retry)

    async def _send_command_while_connected(
        self, commands: list[bytes], retry: int | None = None
    ) -> None:
        self._logger.debug("%s: Sending commands %s", self.name, [c.hex() for c in commands])
        if self._operation_lock.locked():
            self._logger.debug(
                "%s: Operation already in progress, waiting; RSSI: %s", self.name, self.rssi
            )
        async with self._operation_lock:
            try:
                await self._send_command_locked(commands)
                return
            except BleakNotFoundError:
                self._logger.error(
                    "%s: device not found / out of range; RSSI: %s", self.name, self.rssi, exc_info=True
                )
                raise
            except CharacteristicMissingError as ex:
                self._logger.debug("%s: characteristic missing: %s; RSSI: %s", self.name, ex, self.rssi, exc_info=True)
                raise
            except BLEAK_EXCEPTIONS:
                self._logger.debug("%s: communication failed", self.name, exc_info=True)
                raise

        raise RuntimeError("Unreachable")

    @retry_bluetooth_connection_error(DEFAULT_ATTEMPTS)
    async def _send_command_locked(self, commands: list[bytes]) -> None:
        try:
            await self._execute_command_locked(commands)
        except BleakDBusError as ex:
            await asyncio.sleep(BLEAK_BACKOFF_TIME)
            self._logger.debug(
                "%s: RSSI: %s; Backing off %ss; Disconnect due to error: %s",
                self.name, self.rssi, BLEAK_BACKOFF_TIME, ex
            )
            await self._execute_disconnect()
            raise
        except BleakError as ex:
            self._logger.debug("%s: RSSI: %s; Disconnect due to error: %s", self.name, self.rssi, ex)
            await self._execute_disconnect()
            raise

    async def _execute_command_locked(self, commands: list[bytes]) -> None:
        assert self._client is not None  # nosec
        if not self._read_char:
            raise CharacteristicMissingError("Read characteristic missing")
        if not self._write_char:
            raise CharacteristicMissingError("Write characteristic missing")
        for command in commands:
            await self._client.write_gatt_char(self._write_char, command, False)

    def _disconnected(self, client: BleakClientWithServiceCache) -> None:
        if self._expected_disconnect:
            self._logger.debug("%s: Disconnected; RSSI: %s", self.name, self.rssi)
            return
        self._logger.warning("%s: Device unexpectedly disconnected; RSSI: %s", self.name, self.rssi)

    def _resolve_characteristics(self, services: BleakGATTServiceCollection) -> bool:
        # TX (notify/read)
        for characteristic in [UART_TX_CHAR_UUID]:
            if char := services.get_characteristic(characteristic):
                self._read_char = char
                break
        # RX (write)
        for characteristic in [UART_RX_CHAR_UUID]:
            if char := services.get_characteristic(characteristic):
                self._write_char = char
                break
        return bool(self._read_char and self._write_char)

    async def _ensure_connected(self) -> None:
        if self._connect_lock.locked():
            self._logger.debug(
                "%s: Connection already in progress; RSSI: %s", self.name, self.rssi
            )
        if self._client and self._client.is_connected:
            self._reset_disconnect_timer()
            return

        async with self._connect_lock:
            if self._client and self._client.is_connected:
                self._reset_disconnect_timer()
                return

            self._logger.debug("%s: Connecting; RSSI: %s", self.name, self.rssi)

            if isinstance(self._ble_device, BLEDevice):
                device_arg: Union[str, BLEDevice] = self._ble_device
                kwargs = {
                    "use_services_cache": True,
                    "ble_device_callback": lambda: self._ble_device,  # type: ignore[return-value]
                }
            else:
                device_arg = self._ble_device.address  # type: ignore[attr-defined]
                kwargs = {"use_services_cache": True}

            client = await establish_connection(
                BleakClientWithServiceCache, device_arg, self.name, self._disconnected, **kwargs
            )

            self._logger.debug("%s: Connected; RSSI: %s", self.name, self.rssi)

            services = client.services or await client.get_services()
            resolved = self._resolve_characteristics(services)

            self._client = client
            self._reset_disconnect_timer()

            if not resolved:
                raise CharacteristicMissingError("Failed to resolve UART characteristics")

    # public helpers
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

            self._notify_callbacks.clear()
            self._notify_active = False

            if client and client.is_connected:
                if read_char:
                    try:
                        await client.stop_notify(read_char)
                    except Exception:
                        self._logger.debug("%s: Failed to stop notifications", self.name, exc_info=True)
                await client.disconnect()

    def _disconnect(self) -> None:
        self._disconnect_timer = None
        asyncio.create_task(self._execute_timed_disconnect())

    async def _execute_timed_disconnect(self) -> None:
        self._logger.debug("%s: Disconnecting after timeout of %s", self.name, DISCONNECT_DELAY)
        await self._execute_disconnect()


# ─────────────────────────────────────────────────────────────────────
# Doser device
# ─────────────────────────────────────────────────────────────────────
class DoserDevice(BaseDevice):
    """Doser-specific commands mixed onto the common BLE BaseDevice."""

    _model_name = "Doser"
    _model_codes = ["DYDOSED", "DYDOSE", "DOSER"]
    _colors: dict[str, int] = {}

    def __init__(self, device_or_addr: BLEDevice | str) -> None:
        super().__init__(device_or_addr)

    @staticmethod
    def _add_minutes(t: time, delta_min: int) -> time:
        anchor = datetime(2000, 1, 1, t.hour, t.minute)
        return (anchor + timedelta(minutes=delta_min)).time()

    async def send_frames(
        self,
        frames: list[bytes],
        inter_delay_s: float = 0.12,
        listen_s: float = 0.0,
        tap_notifications: bool = True,
        print_hex: bool = False,
    ) -> list[bytes]:
        """Send a burst of frames and optionally listen/collect notifications."""
        await self.connect()
        collected: list[bytes] = []

        def _tap(_c: BleakGATTCharacteristic, data: bytearray) -> None:
            if print_hex:
                print(data.hex())
            collected.append(bytes(data))

        if tap_notifications:
            await self.start_notify_tx()
            self.add_notify_callback(_tap)

        try:
            for pkt in frames:
                await self._send_command(pkt, 1)
                await asyncio.sleep(inter_delay_s)
            if listen_s > 0:
                await asyncio.sleep(listen_s)
        finally:
            if tap_notifications:
                self.remove_notify_callback(_tap)
                try:
                    await self.stop_notify_tx()
                except Exception:
                    pass

        return collected

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
        """
        Immediate dose on channel using 25.6-bucket + 0.1 remainder.

        Sequence:
          • 90/4 ack
          • time-sync (x2)
          • 165/4 acks (4 and 5)
          • 165/manual-dose (hi/lo 25.6+0.1)
        Then listen ~8s for late notifications. Persist locally.
        """
        hi, lo = _split_ml_25_6(ch_ml)

        prelude = [
            dosingcommands.create_order_confirmation(self.get_next_msg_id(), 90, 4, 1),
            dosingcommands.create_set_time_command(self.get_next_msg_id()),
            dosingcommands.create_set_time_command(self.get_next_msg_id()),
            dosingcommands.create_order_confirmation(self.get_next_msg_id(), 165, 4, 4),
            dosingcommands.create_order_confirmation(self.get_next_msg_id(), 165, 4, 5),
        ]
        manual = dosingcommands.create_add_dosing_pump_command_manuell_ml(
            self.get_next_msg_id(), ch_id, hi, lo
        )

        frames = prelude + [manual]
        await self.send_frames(frames, inter_delay_s=0.15, listen_s=8.0, tap_notifications=True, print_hex=False)

        # Persist locally as a stand-in for the app/server
        _STATE.record_manual(self.address, ch_id, ch_ml)
        _STATE.adjust_container(self.address, ch_id, -ch_ml)

    async def add_setting_dosing_pump(
        self,
        performance_time: time,
        ch_id: int,
        weekdays_mask: int,
        ch_ml_tenths: int,
        prefer_hilo: bool | None = None,
    ) -> None:
        """Program a weekly 24h dose entry at HH:MM and ensure auto mode is active."""
        prelude = [
            dosingcommands.create_order_confirmation(self.get_next_msg_id(), 90, 4, 1),
            dosingcommands.create_set_time_command(self.get_next_msg_id()),
            dosingcommands.create_set_time_command(self.get_next_msg_id()),
            dosingcommands.create_order_confirmation(self.get_next_msg_id(), 165, 4, 4),
            dosingcommands.create_order_confirmation(self.get_next_msg_id(), 165, 4, 5),
            dosingcommands.create_switch_to_auto_mode_dosing_pump_command(self.get_next_msg_id(), ch_id),
        ]
        for f in prelude:
            await self._send_command(f, 3)

        if prefer_hilo is True:
            use_hilo = True
        elif prefer_hilo is False:
            use_hilo = False
        else:
            use_hilo = ch_ml_tenths > 255  # >25.5 mL/day → hi/lo scheme

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
        time_cmd = dosingcommands.create_set_time_command(self.get_next_msg_id())
        await self._send_command(switch_cmd, 3)
        await self._send_command(time_cmd, 3)

    async def read_daily_totals(self, timeout_s: float = 6.0, mode_5b: int | str = 0x22) -> Optional[List[float]]:
        """Request daily totals via 0x5B; return [CH1..CH4] or None on timeout."""
        if isinstance(mode_5b, str):
            mode_5b = int(mode_5b, 0)  # accept "0x22" or "34"

        got: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

        def _on_notify(_char: BleakGATTCharacteristic, payload: bytearray) -> None:
            try:
                vals = parse_totals_frame(payload)
                if vals and not got.done():
                    got.set_result(bytes(payload))
            except Exception:
                pass

        client = await self.connect()
        await self.start_notify_tx()
        self.add_notify_callback(_on_notify)
        try:
            frame = build_totals_query_5b(mode_5b)
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
            self.remove_notify_callback(_on_notify)
            try:
                await self.stop_notify_tx()
            except Exception:
                pass

    # convenience getter for local state
    def get_local_containers(self) -> dict[str, float]:
        return _STATE.get_containers(self.address)


# ─────────────────────────────────────────────────────────────────────
# CLI wrappers (BLE actions)
# ─────────────────────────────────────────────────────────────────────
NOT_FOUND_MSG = "Device Not Found, Unreachable or Failed to Connect, ensure Chihiros app is not connected"

async def _resolve_ble_or_fail(device_address: str) -> BLEDevice:
    ble = await BleakScanner.find_device_by_address(device_address, timeout=10.0)
    if not ble:
        typer.echo(NOT_FOUND_MSG)
        raise typer.Exit(1)
    return ble

def _handle_connect_errors(ex: Exception) -> None:
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

@app.command("set-dosing-pump-manuell-ml")
def cli_manual_ml(
    device_address: Annotated[str, typer.Argument(help="BLE MAC")],
    ch_id: Annotated[int, typer.Option("--ch-id", min=0, max=3)],
    ch_ml: Annotated[float, typer.Option("--ch-ml", min=0.2, max=999.9)],
) -> None:
    async def run():
        dd: DoserDevice | None = None
        try:
            ble = await _resolve_ble_or_fail(device_address)
            dd = DoserDevice(ble)
            await dd.set_dosing_pump_manuell_ml(ch_id, ch_ml)
        finally:
            if dd:
                await dd.disconnect()
    asyncio.run(run())

@app.command("probe-totals")
def cli_probe_totals(
    device_address: Annotated[str, typer.Argument(help="BLE MAC")],
    timeout_s: Annotated[float, typer.Option(min=0.5)] = 6.0,
    mode_5b: Annotated[str, typer.Option(help="0x5B mode (e.g. 0x22 or 0x1E)")]= "0x22",
) -> None:
    async def run():
        dd: DoserDevice | None = None
        try:
            ble = await _resolve_ble_or_fail(device_address)
            dd = DoserDevice(ble)
            vals = await dd.read_daily_totals(timeout_s=timeout_s, mode_5b=mode_5b)
            if vals and len(vals) >= 4:
                typer.echo(f"Totals (ml): CH1={vals[0]:.2f}, CH2={vals[1]:.2f}, CH3={vals[2]:.2f}, CH4={vals[3]:.2f}")
            else:
                typer.echo("No totals frame received within timeout.")
        finally:
            if dd:
                await dd.disconnect()
    asyncio.run(run())

# ─────────────────────────────────────────────────────────────────────
# NEW: Tiny CLI for local container volumes & manual history
# ─────────────────────────────────────────────────────────────────────

@app.command("show-containers")
def cli_show_containers(
    device_address: Annotated[str, typer.Argument(help="BLE MAC (used as key for local store)")],
) -> None:
    # No BLE needed to view local cache
    vols = _STATE.get_containers(device_address.upper())
    for ch in range(4):
        ml = vols.get(str(ch), 0.0)
        typer.echo(f"CH{ch+1}: {ml:.1f} mL")

@app.command("set-container")
def cli_set_container(
    device_address: Annotated[str, typer.Argument(help="BLE MAC (used as key for local store)")],
    ch_id: Annotated[int, typer.Option("--ch-id", min=0, max=3)],
    ml: Annotated[float, typer.Option("--ml", help="Absolute content in mL", min=0.0)],
) -> None:
    _STATE.set_container(device_address.upper(), ch_id, ml)
    typer.echo(f"Set CH{ch_id+1} to {ml:.1f} mL")

@app.command("add-container")
def cli_add_container(
    device_address: Annotated[str, typer.Argument(help="BLE MAC (used as key for local store)")],
    ch_id: Annotated[int, typer.Option("--ch-id", min=0, max=3)],
    delta: Annotated[float, typer.Option("--delta", help="Add (or subtract) mL; negative allowed")],
) -> None:
    _STATE.adjust_container(device_address.upper(), ch_id, delta)
    new_ml = _STATE.get_containers(device_address.upper()).get(str(ch_id), 0.0)
    sign = "+" if delta >= 0 else ""
    typer.echo(f"Adjusted CH{ch_id+1} by {sign}{delta:.1f} mL → {new_ml:.1f} mL")

@app.command("show-history")
def cli_show_history(
    device_address: Annotated[str, typer.Argument(help="BLE MAC (used as key for local store)")],
    limit: Annotated[int, typer.Option("--limit", help="Max entries to show", min=1)] = 20,
) -> None:
    hist = list(reversed(_STATE.get_history(device_address.upper())))
    if not hist:
        typer.echo("No manual dose history recorded.")
        return
    for row in hist[:limit]:
        ts = row.get("ts", "?")
        ch = int(row.get("ch", -1))
        ml = float(row.get("ml", 0.0))
        typer.echo(f"{ts}  CH{ch+1}: {ml:.1f} mL")

@app.command("clear-history")
def cli_clear_history(
    device_address: Annotated[str, typer.Argument(help="BLE MAC (used as key for local store)")],
) -> None:
    _STATE.clear_history(device_address.upper())
    typer.echo("Manual dose history cleared.")

# ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app()
