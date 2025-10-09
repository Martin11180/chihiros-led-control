# custom_components/chihiros/chihiros_doser_control/protocol.py
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Tuple, Optional, Iterable, Union
from decimal import Decimal, ROUND_HALF_UP, ROUND_FLOOR
import json
import re

__all__ = [
    "UART_SERVICE", "UART_RX", "UART_TX",
    "CMD_MANUAL_DOSE", "MODE_MANUAL_DOSE", "CMD_LED_QUERY",
    "_split_ml_25_6",
    "dose_ml",
    "build_totals_query_5b", "build_totals_query", "build_totals_probes",
    "parse_totals_frame",
    "decode_weekdays", "ml_from_25_6",
    "parse_frame", "parse_log_blob", "decode_records",
    "DeviceState", "ChannelState", "TimerState", "build_device_state", "to_ctl_lines",
    # optional helper (for nicer debug on params-only logs)
    "interpret_param_burst",
]

# ────────────────────────────────────────────────────────────────
# Nordic UART UUIDs (write to RX, notify on TX)
# ────────────────────────────────────────────────────────────────
UART_SERVICE = "6E400001-B5A3-F393-E0A9-E50E24DCCA9E"
UART_RX      = "6E400002-B5A3-F393-E0A9-E50E24DCCA9E"  # write
UART_TX      = "6E400003-B5A3-F393-E0A9-E50E24DCCA9E"  # notify

# ────────────────────────────────────────────────────────────────
# Command families
# ────────────────────────────────────────────────────────────────
CMD_MANUAL_DOSE  = 0xA5  # 165  (A5-style, "doser")
MODE_MANUAL_DOSE = 0x1B  # 27
CMD_LED_QUERY    = 0x5B  # 91   (5B-style, "LED" frames used for totals)

# ────────────────────────────────────────────────────────────────
# Message-ID generator (avoid 0x5A in either byte)
# ────────────────────────────────────────────────────────────────
_last_msg_id: Tuple[int, int] = (0, 0)

def _next_msg_id() -> Tuple[int, int]:
    hi, lo = _last_msg_id
    lo = (lo + 1) & 0xFF
    if lo == 0x5A:
        lo = (lo + 1) & 0xFF
    if lo == 0:
        hi = (hi + 1) & 0xFF
        if hi == 0x5A:
            hi = (hi + 1) & 0xFF
    globals()['_last_msg_id'] = (hi, lo)
    return hi, lo

# ────────────────────────────────────────────────────────────────
# Checksums / param sanitization
# ────────────────────────────────────────────────────────────────
def _xor_checksum(buf: bytes) -> int:
    c = buf[1]
    for b in buf[2:]:
        c ^= b
    return c & 0xFF

def _sanitize_params(params: List[int]) -> List[int]:
    """Avoid 0x5A in payload bytes (some firmwares disallow it)."""
    out: List[int] = []
    for p in params:
        b = int(p) & 0xFF
        out.append(0x59 if b == 0x5A else b)
    return out

# ────────────────────────────────────────────────────────────────
# Encoders: A5 (doser) & 5B (LED-style)
# ────────────────────────────────────────────────────────────────
def _encode(cmd: int, mode: int, params: List[int]) -> bytes:
    """
    A5-style frame:
      [cmd, 0x01, len(params)+5, msg_hi, msg_lo, mode, *params, checksum]
    If checksum == 0x5A, rotate msg-id (do not mutate params).
    """
    ps = _sanitize_params(params)
    body = b""
    chk = 0
    for _ in range(8):
        hi, lo = _next_msg_id()
        body = bytes([cmd, 0x01, len(ps) + 5, hi, lo, mode, *ps])
        chk = _xor_checksum(body)
        if chk != 0x5A:
            break
    return body + bytes([chk])

def encode_5b(mode: int, params: List[int]) -> bytes:
    """
    0x5B-style frame:
      [0x5B, 0x01, len(params)+2, msg_hi, msg_lo, mode, *params, checksum]
    """
    ps = _sanitize_params(params)
    body = b""
    chk = 0
    for _ in range(8):
        hi, lo = _next_msg_id()
        body = bytes([CMD_LED_QUERY, 0x01, len(ps) + 2, hi, lo, mode, *ps])
        chk = _xor_checksum(body)
        if chk != 0x5A:
            break
    return body + bytes([chk])

# ────────────────────────────────────────────────────────────────
# mL encoding (25.6-bucket + 0.1 remainder) — used by manual dose
# ────────────────────────────────────────────────────────────────
def _split_ml_25_6(total_ml: Union[float, int, str]) -> tuple[int, int]:
    """
    Encode ml as (hi, lo) with 25.6-mL buckets (+0.1-mL remainder).
      hi = floor(ml / 25.6)
      lo = round((ml - hi*25.6) * 10)  # 0..255 (0.1 mL)
    Normalize exact multiples so 25.6 -> (1,0). Clamp 0.2..999.9.
    """
    if isinstance(total_ml, str):
        s = total_ml.replace(",", ".")
    else:
        s = str(total_ml)

    q = Decimal(s).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if q < Decimal("0.2") or q > Decimal("999.9"):
        raise ValueError("ml must be within 0.2..999.9")

    hi = int((q / Decimal("25.6")).to_integral_value(rounding=ROUND_FLOOR))
    rem = (q - Decimal(hi) * Decimal("25.6")).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    lo  = int((rem * 10).to_integral_value(rounding=ROUND_HALF_UP))
    if lo == 256:
        hi += 1
        lo  = 0
    return hi & 0xFF, lo & 0xFF

# ────────────────────────────────────────────────────────────────
# Public write: immediate one-shot dose (manual)
# ────────────────────────────────────────────────────────────────
async def dose_ml(client, channel_1based: int, ml: Union[float, int, str]) -> None:
    """
    Immediate, one-shot dose on the selected channel.

    Protocol (confirmed):
      MODE=27, PARAMS=[ch0..3, 0x00, 0x00, ml_hi, ml_lo]
      ml_hi = floor(ml / 25.6), ml_lo = round(remainder*10)
    """
    ch = max(1, min(int(channel_1based), 4)) - 1  # 0-based on wire
    ml_hi, ml_lo = _split_ml_25_6(ml)
    pkt = _encode(CMD_MANUAL_DOSE, MODE_MANUAL_DOSE, [ch, 0x00, 0x00, ml_hi, ml_lo])
    await client.write_gatt_char(UART_RX, pkt, response=True)

# ────────────────────────────────────────────────────────────────
# Totals helpers (LED-style 0x5B, plus A5 fallbacks)
# ────────────────────────────────────────────────────────────────
def build_totals_query_5b(mode_5b: int = 0x22) -> bytes:
    """Prefer 0x5B totals query; default mode 0x22 (some fw use 0x1E)."""
    return encode_5b(mode_5b, [])

def build_totals_query() -> bytes:
    """Back-compat single-frame helper; try 0x5B/0x22, else A5/0x22."""
    try:
        return encode_5b(0x22, [])
    except Exception:
        return _encode(CMD_MANUAL_DOSE, 0x22, [])

def build_totals_probes() -> list[bytes]:
    """Return a small set of viable totals queries across firmwares."""
    frames: list[bytes] = []
    try:
        frames.append(encode_5b(0x22, []))
        frames.append(encode_5b(0x1E, []))
    except Exception:
        pass
    try:
        frames.append(_encode(CMD_MANUAL_DOSE, 0x22, []))
        frames.append(_encode(CMD_MANUAL_DOSE, 0x1E, []))
    except Exception:
        pass
    # de-dup preserving order
    seen, uniq = set(), []
    for f in frames:
        b = bytes(f)
        if b not in seen:
            seen.add(b)
            uniq.append(f)
    return uniq

# ────────────────────────────────────────────────────────────────
# Robust totals parsing (0x5B)
# ────────────────────────────────────────────────────────────────
def _looks_like_totals_pairs(p: List[int]) -> bool:
    """
    Heuristic: p must be length 8 and represent four (hi, lo) pairs where
    hi is small (0..10 is plenty for most realistic daily totals with 25.6 mL buckets)
    and lo fits in a byte (0..255).
    """
    if len(p) != 8:
        return False
    for i in range(0, 8, 2):
        hi = p[i]
        lo = p[i + 1]
        if not (0 <= hi <= 10 and 0 <= lo <= 255):
            return False
    return True

def _decode_totals_pairs(pairs8: List[int]) -> List[float]:
    # hi*25.6 + lo*0.1 for 4 channels
    return [round(pairs8[i] * 25.6 + pairs8[i + 1] / 10.0, 1) for i in range(0, 8, 2)]

def parse_totals_frame(payload: bytes | bytearray) -> Optional[List[float]]:
    """
    If 'payload' is 0x5B-style, decode 4 channel totals:
      [ch1_hi, ch1_lo, ch2_hi, ch2_lo, ch3_hi, ch3_lo, ch4_hi, ch4_lo]
      ml = hi*25.6 + lo*0.1

    Supports both the strict 8-param form and longer packets that embed the 8
    bytes somewhere in the params (common on some firmwares).
    """
    if not isinstance(payload, (bytes, bytearray)) or len(payload) < 15:
        return None
    if payload[0] != CMD_LED_QUERY:
        return None

    # params live after 'mode' byte up to (but excluding) checksum
    params = list(payload[6:-1])

    # Fast path: exactly 8 → decode directly
    if len(params) == 8 and _looks_like_totals_pairs(params):
        return _decode_totals_pairs(params)

    # Robust path: scan any contiguous window of length 8 that looks like totals
    if len(params) >= 8:
        for i in range(0, len(params) - 8 + 1):
            win = params[i:i + 8]
            if _looks_like_totals_pairs(win):
                return _decode_totals_pairs(win)

    return None

# ────────────────────────────────────────────────────────────────
# Weekday helpers
# ────────────────────────────────────────────────────────────────
WEEKDAY_BITS = {
    64: "monday",
    32: "tuesday",
    16: "wednesday",
    8:  "thursday",
    4:  "friday",
    2:  "saturday",
    1:  "sunday",
}

def decode_weekdays(mask: int) -> List[str]:
    if mask == 127:
        return ["everyday"]
    return [name for bit, name in WEEKDAY_BITS.items() if mask & bit]

def ml_from_25_6(hi_buckets: int, tenths_remainder: int) -> float:
    return round(25.6 * hi_buckets + 0.1 * tenths_remainder, 1)

# ────────────────────────────────────────────────────────────────
# Frame decoders (tolerant)
# ────────────────────────────────────────────────────────────────
def _dose27_is_manual(params: List[int]) -> bool:
    """
    Heuristic: manual dose is either 5 or 6 bytes and ends with (hi, lo):
      • [ch, 0, 0, hi, lo]
      • [ch, 0, 0, x, hi, lo]  (some fw insert a filler byte)
    """
    n = len(params)
    if n == 5:
        ch, a, b, hi, lo = params
        return a == 0 and b == 0 and 0 <= hi <= 10 and 0 <= lo <= 255
    if n == 6:
        ch, a, b, x, hi, lo = params
        return a == 0 and b == 0 and 0 <= hi <= 10 and 0 <= lo <= 255
    return False

def _dose27_is_weekly(params: List[int]) -> bool:
    """Heuristic: weekly entry is [ch, mask, enable, HH, MM, dose_x10]."""
    if len(params) != 6:
        return False
    _, mask, en, HH, MM, dose10 = params
    return (0 <= mask <= 127) and (en in (0, 1)) and (0 <= HH <= 23) and (0 <= MM <= 59) and (0 <= dose10 <= 255)

def decode_time_90_9(params: List[int]) -> Dict[str, Any]:
    yy, mm, idx, HH, MM, SS = params
    return {
        "type": "time_set",
        "year": 2000 + yy,
        "month": mm,
        "day_or_week_index": idx,
        "hour": HH,
        "minute": MM,
        "second": SS,
    }

def decode_activate_165_32(params: List[int]) -> Dict[str, Any]:
    ch, _zero, enable = params
    return {"type": "activate", "channel": ch, "enabled": bool(enable)}

def decode_dose_165_27(params: List[int]) -> Dict[str, Any]:
    # Manual-dose variants first
    if _dose27_is_manual(params):
        if len(params) == 5:
            ch, _a, _b, hi, tenths = params
        else:  # len == 6
            ch, _a, _b, _x, hi, tenths = params
        ml = ml_from_25_6(hi, tenths)
        return {
            "type": "manual_dose",
            "channel": ch,
            "amount_ml": ml,
            "raw": params,
        }

    # Weekly schedule (dose×10 byte)
    if _dose27_is_weekly(params):
        ch, mask, enable, HH, MM, dose10 = params
        return {
            "type": "dose_entry",
            "channel": ch,
            "weekdays_mask": mask,
            "weekdays": decode_weekdays(mask),
            "enabled": bool(enable),
            "time_hour": HH,
            "time_minute": MM,
            "amount_ml": round(dose10 / 10.0, 1),
            "raw_aux": [],
        }

    # Fallback: original 6-field 25.6+0.1 layout from captures
    if len(params) == 6:
        ch, mask, c1, c2, hi, tenths = params
        ml = ml_from_25_6(hi, tenths)
        obj = {
            "type": "dose_entry",
            "channel": ch,
            "weekdays_mask": mask,
            "weekdays": decode_weekdays(mask),
            "amount_ml": ml,
            "raw_aux": [c1, c2],
        }
        if not (0.2 <= ml <= 99999.0):
            obj["warning"] = "dose_out_of_range"
        return obj

    return {"type": "unknown_27", "params": params}

def decode_timer_165_21(params: List[int]) -> Dict[str, Any]:
    # [channel, enable_or_type, hour, minute, r1, r2]
    ch, t_or_en, hh, mm, r1, r2 = params
    entry = {
        "type": "timer",
        "channel": ch,
        "timer_type": t_or_en,   # some fw use 1 == 24-hour; others treat this as enable
        "start_hour": hh,
        "start_minute": mm,
    }
    if not (0 <= mm <= 59):
        entry["warning"] = "minute_out_of_range"
    return entry

def parse_frame(cmd_id: int, mode: int, params: List[int]) -> Dict[str, Any]:
    if cmd_id == 90 and mode == 9 and len(params) == 6:
        return decode_time_90_9(params)
    if cmd_id == 165 and mode == 32 and len(params) == 3:
        return decode_activate_165_32(params)
    if cmd_id == 165 and mode == 27 and (len(params) in (5, 6)):
        return decode_dose_165_27(params)
    if cmd_id == 165 and mode == 21 and len(params) == 6:
        return decode_timer_165_21(params)
    if (cmd_id, mode) in {(90, 4), (165, 4)}:
        return {"type": "control", "cmd": cmd_id, "mode": mode, "params": params}
    return {"type": "unknown", "cmd": cmd_id, "mode": mode, "params": params}

# ────────────────────────────────────────────────────────────────
# Log parsing → records
# ────────────────────────────────────────────────────────────────
_ENCODE_BLOCK_RE = re.compile(
    r"Encode Message.*?Command ID\s*:\s*(\d+).*?Mode\s*:\s*(\d+).*?Parameters\s*:\s*(\[[^\]]*\])",
    re.IGNORECASE | re.DOTALL,
)

def _safe_int_list_from_str(lst_str: str) -> List[int]:
    try:
        raw = json.loads(lst_str)
        if isinstance(raw, list):
            return [int(x) & 0xFF for x in raw]
    except Exception:
        pass
    # fallback: split on non-digits
    vals = re.findall(r"-?\d+", lst_str)
    return [int(v) & 0xFF for v in vals]

def parse_log_blob(text: str) -> List[Tuple[int, int, List[int]]]:
    """
    Accepts:
      • "Encode Message ... Command ID: N ... Mode: M ... Parameters: [..]"
      • JSON lines: {"cmd":165,"mode":27,"params":[...]} or arrays
    Returns list of (cmd, mode, params).
    """
    out: List[Tuple[int,int,List[int]]] = []

    # 1) Encode Message blocks
    for m in _ENCODE_BLOCK_RE.finditer(text):
        try:
            cmd = int(m.group(1))
            mode = int(m.group(2))
            params = _safe_int_list_from_str(m.group(3))
            out.append((cmd, mode, params))
        except Exception:
            continue

    # 2) JSON lines / blobs
    for line in text.splitlines():
        line = line.strip()
        if not line or not (line.startswith("{") or line.startswith("[")):
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        def add(cmd: int, mode: int, params: List[int]) -> None:
            out.append((int(cmd), int(mode), [int(x) & 0xFF for x in params]))
        if isinstance(obj, dict):
            if "cmd" in obj and "mode" in obj and "params" in obj:
                add(obj["cmd"], obj["mode"], list(obj["params"]))
        elif isinstance(obj, list):
            # support bare [cmd, mode, [params...]]
            if len(obj) == 3 and isinstance(obj[2], list):
                add(obj[0], obj[1], obj[2])

    return out

def decode_records(records: Iterable[Tuple[int,int,List[int]]]) -> List[Dict[str,Any]]:
    return [parse_frame(c, m, p) for (c, m, p) in records]

# ────────────────────────────────────────────────────────────────
# State builder
# ────────────────────────────────────────────────────────────────
@dataclass
class TimerState:
    timer_type: Optional[int] = None   # 1 == 24-hour on some fw
    start_hour: Optional[int] = None
    start_minute: Optional[int] = None

@dataclass
class ChannelState:
    channel: int
    enabled: Optional[bool] = None
    amount_ml: Optional[float] = None
    weekdays_mask: Optional[int] = None
    weekdays: List[str] = field(default_factory=list)
    time_hour: Optional[int] = None
    time_minute: Optional[int] = None
    timer: TimerState = field(default_factory=TimerState)

    def merge(self, event: Dict[str, Any]) -> None:
        t = event.get("type")
        if t == "activate":
            self.enabled = bool(event.get("enabled"))
        elif t == "dose_entry":
            # weekly variant can carry time and enable
            self.amount_ml = float(event.get("amount_ml"))
            if "weekdays_mask" in event:
                self.weekdays_mask = int(event.get("weekdays_mask"))
                self.weekdays = list(event.get("weekdays", []))
            if "time_hour" in event:
                self.time_hour = int(event.get("time_hour"))
            if "time_minute" in event:
                self.time_minute = int(event.get("time_minute"))
            if "enabled" in event and self.enabled is None:
                self.enabled = bool(event.get("enabled"))
        elif t == "manual_dose":
            # keep last manual dose amount (debugging/telemetry)
            self.amount_ml = float(event.get("amount_ml"))
        elif t == "timer":
            self.timer.timer_type = int(event.get("timer_type"))
            self.timer.start_hour = int(event.get("start_hour"))
            self.timer.start_minute = int(event.get("start_minute"))

@dataclass
class DeviceState:
    device_time: Optional[Dict[str, Any]] = None  # from 90/9
    channels: Dict[int, ChannelState] = field(default_factory=dict)
    other_events: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "device_time": self.device_time,
            "channels": {ch: asdict(state) for ch, state in self.channels.items()},
            "other_events": self.other_events,
        }

def build_device_state(parsed_rows: List[Dict[str, Any]]) -> DeviceState:
    ds = DeviceState()
    for ev in parsed_rows:
        et = ev.get("type")
        if et == "time_set":
            ds.device_time = ev  # keep last seen
            continue
        if et in {"activate", "dose_entry", "manual_dose", "timer"}:
            ch = int(ev["channel"])
            st = ds.channels.get(ch) or ChannelState(channel=ch)
            st.merge(ev)
            ds.channels[ch] = st
            continue
        ds.other_events.append(ev)
    return ds

# ────────────────────────────────────────────────────────────────
# CTL export (flat key=value lines)
# ────────────────────────────────────────────────────────────────
def _weekday_str(mask: Optional[int]) -> str:
    if mask is None:
        return "unknown"
    names = decode_weekdays(int(mask))
    if names == ["everyday"]:
        return "Every day"
    # preserve Mon..Sun ordering for readability
    order = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]
    names_sorted = [n for n in order if n in names]
    short = {"monday":"Mon","tuesday":"Tue","wednesday":"Wed","thursday":"Thu",
             "friday":"Fri","saturday":"Sat","sunday":"Sun"}
    return ",".join(short[n] for n in names_sorted) if names_sorted else "None"

def to_ctl_lines(state: DeviceState) -> List[str]:
    lines: List[str] = []
    if state.device_time:
        dt = state.device_time
        lines.append(
            f"device_time={dt.get('year'):04d}-{dt.get('month'):02d} "
            f"{dt.get('hour'):02d}:{dt.get('minute'):02d}:{dt.get('second'):02d}"
        )
    for ch_idx in sorted(state.channels.keys()):
        st = state.channels[ch_idx]
        chn = ch_idx  # wire is 0..3; caller can map to CH1..CH4 if needed
        if st.enabled is not None:
            lines.append(f"ch{chn}.enabled={'1' if st.enabled else '0'}")
        if st.amount_ml is not None:
            lines.append(f"ch{chn}.amount_ml={st.amount_ml:.1f}")
        if st.weekdays_mask is not None:
            lines.append(f"ch{chn}.weekday_mask={int(st.weekdays_mask)}")
            lines.append(f"ch{chn}.weekdays={_weekday_str(st.weekdays_mask)}")
        # prefer explicit weekly time if known; else timer start
        hh = st.time_hour if st.time_hour is not None else st.timer.start_hour
        mm = st.time_minute if st.time_minute is not None else st.timer.start_minute
        if hh is not None and mm is not None:
            lines.append(f"ch{chn}.time={int(hh):02d}:{int(mm):02d}")
        if st.timer.timer_type is not None:
            lines.append(f"ch{chn}.timer_type={int(st.timer.timer_type)}")
    return lines

# ────────────────────────────────────────────────────────────────
# Optional: interpret bare params bursts from notify logs
# ────────────────────────────────────────────────────────────────
def interpret_param_burst(params: List[int]) -> Dict[str, Any]:
    """
    Best-effort interpretation of a bare params array captured from notify logs
    when the logger only prints the params (no cmd/mode header). This is only
    for diagnostics; it cannot be definitive.
    """
    n = len(params)
    out: Dict[str, Any] = {"guess": "unknown", "details": {"len": n, "params": params}}

    # Obvious 6-field timer shape: [ch, type/en, HH, MM, r1, r2]
    if n == 6 and 0 <= params[2] <= 23 and 0 <= params[3] <= 59:
        out["guess"] = "maybe_timer_165_21"
        out["details"].update({"channel": params[0], "hour": params[2], "minute": params[3]})
        return out

    # 6-field weekly schedule [ch, mask, enable, HH, MM, dose×10]
    if n == 6 and 0 <= params[1] <= 127 and params[2] in (0, 1) and 0 <= params[3] <= 23 and 0 <= params[4] <= 59:
        out["guess"] = "maybe_weekly_165_27"
        out["details"].update({
            "channel": params[0], "weekdays_mask": params[1], "enabled": bool(params[2]),
            "hour": params[3], "minute": params[4], "dose_tenths": params[5],
        })
        return out

    # 5/6-byte manual dose ending in (hi, lo)
    if (n == 5 and params[1] == 0 and params[2] == 0) or (n == 6 and params[1] == 0 and params[2] == 0):
        hi, lo = (params[-2], params[-1])
        if 0 <= hi <= 10 and 0 <= lo <= 255:
            out["guess"] = "maybe_manual_dose_165_27"
            out["details"].update({"channel": params[0], "amount_ml": ml_from_25_6(hi, lo)})
            return out

    # Try totals window if someone logged only the params section of a 0x5B frame
    if n >= 8:
        for i in range(0, n - 8 + 1):
            win = params[i:i + 8]
            if _looks_like_totals_pairs(win):
                out["guess"] = "maybe_led_totals_0x5B"
                out["details"]["totals_ml"] = _decode_totals_pairs(win)
                return out

    return out

# ────────────────────────────────────────────────────────────────
# Quick self-test / example
# ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # Demo with your Channel 3 capture (24-hour set, 37.0 mL, minute 42, everyday)
    example_frames = [
        (90, 4,  [1]),
        (90, 9,  [25, 10, 2, 11, 28, 47]),
        (90, 9,  [25, 10, 2, 11, 28, 47]),
        (CMD_MANUAL_DOSE, 4,  [4]),
        (CMD_MANUAL_DOSE, 4,  [5]),
        (CMD_MANUAL_DOSE, 32, [2, 0, 1]),
        (CMD_MANUAL_DOSE, 27, [2, 127, 1, 1, 1, 114]),  # legacy weekly variant
        (CMD_MANUAL_DOSE, 21, [2, 1, 0, 42, 0, 0]),     # 24h start 00:42
        (CMD_MANUAL_DOSE, 27, [1, 0, 0, 1, 140]),       # manual dose 25.6*1 + 14.0 = 39.6 mL (5-byte form)
    ]
    parsed = decode_records(example_frames)
    state = build_device_state(parsed)

    from pprint import pprint
    print("STATE:")
    pprint(state.to_dict())
    print("\nCTL:")
    for ln in to_ctl_lines(state):
        print(ln)
