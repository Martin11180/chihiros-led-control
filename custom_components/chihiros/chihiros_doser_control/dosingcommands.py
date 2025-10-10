# custom_components/chihiros/chihiros_doser_control/dosingcommands.py
from __future__ import annotations

from datetime import time, datetime
from typing import List, Tuple

from .protocol import _split_ml_25_6  # same 25.6+0.1 encoder

__all__ = [
    "_create_command_encoding_dosing_pump",      # kept for back-compat
    "create_command_encoding_dosing_pump",       # public alias
    "create_add_dosing_pump_command_manuell_ml",
    "create_add_dosing_pump_command_manuell_ml_amount",
    "create_add_auto_setting_command_dosing_pump",
    "create_auto_mode_dosing_pump_command_time",
    "create_switch_to_auto_mode_dosing_pump_command",
    "create_order_confirmation",
    "create_reset_auto_settings_command",
    "create_schedule_weekly_byte_amount",
    "create_schedule_weekly_hi_lo",
    "create_set_time_command",
    "next_message_id",
]

# ────────────────────────────────────────────────────────────────
# Byte helpers
# ────────────────────────────────────────────────────────────────

def _clamp_byte(v: int) -> int:
    if not isinstance(v, int):
        raise TypeError(f"Parameter must be int (got {type(v).__name__})")
    if v < 0 or v > 255:
        raise ValueError(f"Parameter byte out of range 0..255: {v}")
    return v

def _bump_msg_id(msg_hi: int, msg_lo: int) -> tuple[int, int]:
    lo = (msg_lo + 1) & 0xFF
    hi = msg_hi
    if lo == 0x5A:                # never 0x5A in msg-id low
        lo = (lo + 1) & 0xFF
    if lo == 0:                   # wrapped → bump hi
        hi = (hi + 1) & 0xFF
        if hi == 0x5A:            # never 0x5A in msg-id high
            hi = (hi + 1) & 0xFF
    return hi, lo

def next_message_id(current: Tuple[int, int] | None = None) -> Tuple[int, int]:
    """
    Return the next (hi, lo) message-id tuple, skipping 0x5A in either byte.
    If 'current' is None, start from (0, 0) and produce (0, 1).
    """
    if current is None:
        hi, lo = 0, 0
    else:
        hi, lo = int(current[0]) & 0xFF, int(current[1]) & 0xFF

    lo = (lo + 1) & 0xFF
    if lo == 0x5A:
        lo = (lo + 1) & 0xFF
    if lo == 0:
        hi = (hi + 1) & 0xFF
        if hi == 0x5A:
            hi = (hi + 1) & 0xFF
    return hi, lo

def _sanitize_params(params: List[int]) -> List[int]:
    """Avoid 0x5A in payload bytes (mirrors other parts of the project)."""
    out: List[int] = []
    for p in params:
        b = _clamp_byte(p)
        out.append(0x59 if b == 0x5A else b)
    return out

def _xor_checksum(buf: bytes | bytearray) -> int:
    """
    XOR of bytes 1..end-1 (identical to protocol.py behavior).
    """
    if len(buf) < 2:
        return 0
    c = buf[1]
    for b in buf[2:]:
        c ^= b
    return c & 0xFF

# ────────────────────────────────────────────────────────────────
# Core A5 encoder (165 family)
# ────────────────────────────────────────────────────────────────

def _create_command_encoding_dosing_pump(
    cmd_id: int,
    cmd_mode: int,
    msg_id: tuple[int, int],
    parameters: list[int],
) -> bytearray:
    """
    A5-style wire format:
      [cmd_id, 0x01, len(params)+5, msg_hi, msg_lo, cmd_mode, *params, checksum]
    Checksum is XOR over bytes 1..end-1.
    If checksum == 0x5A, bump msg-id and retry (do NOT mutate payload bytes).
    """
    _clamp_byte(cmd_id); _clamp_byte(cmd_mode)
    msg_hi, msg_lo = msg_id
    _clamp_byte(msg_hi); _clamp_byte(msg_lo)
    ps = _sanitize_params(parameters)

    # try a few msg-ids until checksum != 0x5A
    frame = bytearray()
    checksum = 0
    for _ in range(8):
        frame = bytearray([cmd_id, 1, len(ps) + 5, msg_hi, msg_lo, cmd_mode] + ps)
        checksum = _xor_checksum(frame) & 0xFF
        if checksum != 0x5A:
            return frame + bytes([checksum])
        msg_hi, msg_lo = _bump_msg_id(msg_hi, msg_lo)

    # last resort: return the last attempt
    return frame + bytes([checksum])

# Public alias (no change in behavior, easier to import without underscore)
create_command_encoding_dosing_pump = _create_command_encoding_dosing_pump

# ────────────────────────────────────────────────────────────────
# WRITE command creators
# ────────────────────────────────────────────────────────────────
# Notes:
# - Channel on wire is 0-based (0..3). UI often labels these CH1..CH4.
# - Mode 0x1B (27) is used for BOTH: manual-dose payload and weekly schedule,
#   but the payload layouts differ (confirmed from captures):
#     • Manual dose immediate: [ch, 0, 0, ml_hi(25.6), ml_lo(0.1)]
#     • Weekly schedule:       [ch, weekday_mask, enable, hour, minute, dose_x10]
# - Mode 0x15 (21): time override/reinforce: [ch, enable, hour, minute, 0, 0]
# - Mode 0x20 (32): channel init / auto mode flags: [ch, catch_up, active_flag]

def create_add_dosing_pump_command_manuell_ml(
    msg_id: tuple[int, int],
    ch_id: int,
    ch_ml_one: int,  # hi in 25.6 mL units (0..255)
    ch_ml_two: int,  # lo in 0.1 mL units (0..255)
) -> bytearray:
    """
    Manual one-shot dose (immediate):
      mode=0x1B (27), params = [channel, 0, 0, hi_25_6, lo_0_1]
    """
    _clamp_byte(ch_id)
    _clamp_byte(ch_ml_one)
    _clamp_byte(ch_ml_two)
    return _create_command_encoding_dosing_pump(165, 27, msg_id, [ch_id, 0, 0, ch_ml_one, ch_ml_two])

def create_add_dosing_pump_command_manuell_ml_amount(
    msg_id: tuple[int, int],
    ch_id: int,
    ml: float | int | str,
) -> bytearray:
    """Convenience: pass ml directly (0.2..999.9), we split to (hi, lo) with 25.6+0.1."""
    hi, lo = _split_ml_25_6(ml)
    return create_add_dosing_pump_command_manuell_ml(msg_id, ch_id, hi, lo)

def create_add_auto_setting_command_dosing_pump(
    performance_time: time,
    msg_id: tuple[int, int],
    ch_id: int,
    weekdays_mask: int,
    daily_ml_tenths: int,  # dose×10 (e.g., 75 = 7.5 mL)
    enabled: bool = True,
) -> bytearray:
    """
    Weekly schedule entry (confirmed layout):
      mode=0x1B (27), params = [channel, weekday_mask, enable(1/0), hour, minute, dose_x10]
    """
    _clamp_byte(ch_id)
    _clamp_byte(weekdays_mask & 0x7F)
    _clamp_byte(performance_time.hour)
    _clamp_byte(performance_time.minute)
    dose10 = int(daily_ml_tenths)
    if dose10 < 0 or dose10 > 255:
        raise ValueError("daily_ml_tenths must be 0..255 (i.e., 0.0..25.5 mL if limited to one byte)")
    return _create_command_encoding_dosing_pump(
        165, 27, msg_id, [ch_id, weekdays_mask & 0x7F, 1 if enabled else 0,
                          performance_time.hour, performance_time.minute, dose10]
    )

def create_auto_mode_dosing_pump_command_time(
    performance_time: time,
    msg_id: tuple[int, int],
    ch_id: int,
    enabled: bool = True,
) -> bytearray:
    """
    Time reinforce/override (last-write-wins):
      mode=0x15 (21), params = [channel, enable(1/0), hour, minute, 0, 0]
    """
    _clamp_byte(ch_id)
    _clamp_byte(performance_time.hour)
    _clamp_byte(performance_time.minute)
    return _create_command_encoding_dosing_pump(
        165, 21, msg_id, [ch_id, 1 if enabled else 0, performance_time.hour, performance_time.minute, 0, 0]
    )

def create_switch_to_auto_mode_dosing_pump_command(
   msg_id: tuple[int, int],
   channel_id: int,
   catch_up_missed: int = 0,   # 0/1 (Make up for missed dose)
   active_flag: int = 1,       # 0/1 (Inactive/Active)
) -> bytearray:
    """
    Channel init / auto-mode flags:
      mode=0x20 (32), params = [channel_id, catch_up_missed, active_flag]
    """
    _clamp_byte(channel_id); _clamp_byte(catch_up_missed); _clamp_byte(active_flag)
    return _create_command_encoding_dosing_pump(165, 32, msg_id, [channel_id, catch_up_missed, active_flag])

def create_order_confirmation(
    msg_id: tuple[int, int],
    command_id: int,
    mode: int,
    command: int,
) -> bytearray:
    """Generic “button/ack” wrapper (e.g. 90/4 [1], 165/4 [4],[5])."""
    return _create_command_encoding_dosing_pump(command_id, mode, msg_id, [_clamp_byte(command)])

def create_reset_auto_settings_command(msg_id: tuple[int, int]) -> bytearray:
    """Reset auto settings (semantics are firmware-dependent)."""
    return _create_command_encoding_dosing_pump(90, 5, msg_id, [5, 255, 255])

def create_set_time_command(msg_id: tuple[int, int]) -> bytearray:
    """
    Set device time:
      90/9 [YY, MM, idx, HH, MM, SS]
      • YY = year - 2000
      • idx = ISO weekday 1..7 (Mon=1 .. Sun=7)
    """
    now = datetime.now()
    yy = (now.year - 2000) & 0xFF
    mm = now.month & 0xFF
    idx = now.isoweekday() & 0xFF
    HH = now.hour & 0xFF
    MM = now.minute & 0xFF
    SS = now.second & 0xFF
    return _create_command_encoding_dosing_pump(90, 9, msg_id, [yy, mm, idx, HH, MM, SS])

# ────────────────────────────────────────────────────────────────
# Dual schedule variants (support multiple firmware flavors)
# ────────────────────────────────────────────────────────────────

def create_schedule_weekly_byte_amount(
    performance_time: time,
    msg_id: tuple[int, int],
    ch_id: int,
    weekdays_mask: int,
    daily_ml_tenths: int,  # 0..255
    enabled: bool = True,
) -> bytearray:
    """
    Variant A (single-byte dose×10 inside 0x1B):
      0x1B / 27 : [ch, weekday_mask, enable, HH, MM, dose_x10]
    """
    _clamp_byte(ch_id)
    _clamp_byte(weekdays_mask & 0x7F)
    _clamp_byte(performance_time.hour)
    _clamp_byte(performance_time.minute)
    dose10 = int(daily_ml_tenths)
    if not (0 <= dose10 <= 255):
        raise ValueError("daily_ml_tenths must be 0..255 for byte-based variant")
    return _create_command_encoding_dosing_pump(
        165, 27, msg_id, [ch_id, weekdays_mask & 0x7F, 1 if enabled else 0,
                          performance_time.hour, performance_time.minute, dose10]
    )

def create_schedule_weekly_hi_lo(
    performance_time: time,
    msg_id_time: tuple[int, int],
    msg_id_amount: tuple[int, int],
    ch_id: int,
    weekdays_mask: int,
    daily_ml: float,
    enabled: bool = True,
) -> list[bytearray]:
    """
    Variant B (time in 0x15, amount as hi/lo in 0x1B):
      0x15 / 21 : [ch, enable, HH, MM, 0, 0]
      0x1B / 27 : [ch, weekday_mask, enable, 0, ml_hi(25.6), ml_lo(0.1)]
    Returns the two frames in order.
    """
    _clamp_byte(ch_id)
    _clamp_byte(weekdays_mask & 0x7F)
    _clamp_byte(performance_time.hour)
    _clamp_byte(performance_time.minute)
    hi, lo = _split_ml_25_6(daily_ml)
    f_time = create_auto_mode_dosing_pump_command_time(performance_time, msg_id_time, ch_id, enabled=enabled)
    f_amount = _create_command_encoding_dosing_pump(
        165, 27, msg_id_amount, [ch_id, weekdays_mask & 0x7F, 1 if enabled else 0, 0, hi, lo]
    )
    return [f_time, f_amount]
