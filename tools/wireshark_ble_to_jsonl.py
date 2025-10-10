#!/usr/bin/env python3
"""
Convert a Wireshark JSON export (array or NDJSON/"-T ek") into JSON Lines (one JSON per line).
"""

import argparse, sys
from pathlib import Path
from custom_components.chihiros.wireshark.wireshark_core import (
    parse_wireshark_stream, write_jsonl
)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("input", help="Wireshark JSON export (array or NDJSON)")
    ap.add_argument("--handle", default="0x0010", help="ATT handle to match (default 0x0010)")
    ap.add_argument("--op", choices=["write","notify","any"], default="write", help="ATT op filter")
    ap.add_argument("--rx", choices=["no","also","only"], default="no", help="Include notifications (RX)")
    ap.add_argument("--output", "-o", type=Path, help="Output JSONL path (default: stdout)")
    ap.add_argument("--pretty", action="store_true", help="Pretty-print each JSON line")
    args = ap.parse_args()

    with open(args.input, "r", encoding="utf-8") as f:
        rows = parse_wireshark_stream(f, handle=args.handle, op=args.op, rx=args.rx)
        if args.output:
            with args.output.open("w", encoding="utf-8") as w:
                write_jsonl(rows, w, pretty=args.pretty)
        else:
            write_jsonl(rows, sys.stdout, pretty=args.pretty)

if __name__ == "__main__":
    main()
