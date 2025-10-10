#!/usr/bin/env python3
"""
Convert a Wireshark JSON export (array or NDJSON/"-T ek") into JSON Lines (1 JSON per line).
"""

from __future__ import annotations
import argparse, sys
from pathlib import Path
from custom_components.chihiros.wireshark.wireshark_core import parse_wireshark_stream, write_jsonl

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="Wireshark export (JSON array or NDJSON), or '-' for stdin")
    ap.add_argument("--handle", default="0x0010", help="ATT handle to match (default 0x0010)")
    ap.add_argument("--op", choices=["write", "notify", "any"], default="write", help="ATT op filter")
    ap.add_argument("--rx", choices=["no", "also", "only"], default="no", help="Include notifications (RX)")
    ap.add_argument("-o", "--out", default="-", help="Output JSONL path (or '-' for stdout)")
    ap.add_argument("--pretty", action="store_true", help="Pretty-print each JSON line")
    args = ap.parse_args()

    # Input stream
    if args.input == "-":
        in_stream = sys.stdin
    else:
        in_path = Path(args.input).expanduser()
        if not in_path.is_absolute():
            in_path = (Path.cwd() / in_path).resolve()
        in_stream = in_path.open("r", encoding="utf-8")

    # Output stream
    if args.out == "-":
        out_stream = sys.stdout
    else:
        out_path = Path(args.out).expanduser()
        if not out_path.is_absolute():
            out_path = (Path.cwd() / out_path).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_stream = out_path.open("w", encoding="utf-8")

    try:
        rows = parse_wireshark_stream(in_stream, handle=args.handle, op=args.op, rx=args.rx)
        write_jsonl(rows, out_stream, pretty=args.pretty)
        return 0
    finally:
        if in_stream is not sys.stdin:
            in_stream.close()
        if out_stream is not sys.stdout:
            out_stream.close()

if __name__ == "__main__":
    raise SystemExit(main())
