from __future__ import annotations

from datetime import time
from typing import List, Tuple

from ..chihiros_led_control import commands
from .protocol import _split_ml_25_6  # use the same 25.6+0.1 encoder

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

def _sanitize_params(params: List[int]) -> List[int]:
    """Avoid 0x5A in payload bytes (mirrors other parts of the project)."""
    out: List[int] = []
    for p in params:
        b = _clamp_byte(p)
        out.append(0x59 if b == 0x5A else b)
    return out

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
    Checksum is XOR over bytes 1..end-1 (same as led-control `commands._calculate_checksum`).
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
        checksum = commands._calculate_checksum(frame) & 0xFF
        if checksum != 0x5A:
            return frame + bytes([checksum])
        msg_hi, msg_lo = _bump_msg_id(msg_hi, msg_lo)

    # last resort: return the last attempt
    return frame + bytes([checksum])

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
    """
    Convenience: pass ml directly (0.2..999.9), we split to (hi, lo) with 25.6+0.1.
    """
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

    Examples:
      CH2, Monday 00:27 @ 7.5 mL → [1, 0x02, 1, 0, 27, 75]
      CH1, Sat 01:00 @ 7.5 mL    → [0, 0x40, 1, 1,  0, 75]
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
    """Generic “button/ack” wrapper."""
    return _create_command_encoding_dosing_pump(command_id, mode, msg_id, [_clamp_byte(command)])

def create_reset_auto_settings_command(msg_id: tuple[int, int]) -> bytearray:
    """Reset auto settings (semantics are firmware-dependent)."""
    return _create_command_encoding_dosing_pump(90, 5, msg_id, [5, 255, 255])