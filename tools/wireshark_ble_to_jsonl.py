#!/usr/bin/env python3
"""
Convert a Wireshark JSON export (array or NDJSON/"-T ek") into JSON Lines (1 JSON per line).

✅ Windows-friendly:
- Accepts relative paths like .\capture.json
- Searches current dir, repo root, and tools\ if not found
- Defaults output to custom_components\chihiros\wireshark\captures\<input_stem>.jsonl
- Works with stdin/stdout ("-" argument)
"""

from __future__ import annotations
import argparse, sys
from pathlib import Path
from custom_components.chihiros.wireshark.wireshark_core import parse_wireshark_stream, write_jsonl


def _repo_root() -> Path:
    """Return repository root (tools/ is directly under it)."""
    return Path(__file__).resolve().parent.parent


def _captures_dir() -> Path:
    """Return path to the captures folder."""
    return _repo_root() / "custom_components/chihiros/wireshark/captures"


def _resolve_input(p: str) -> Path:
    """
    Try CWD, absolute, repo root, and tools/ folder.
    Returns a Path if found, else raises FileNotFoundError.
    """
    cand = Path(p).expanduser()
    if cand.is_absolute() and cand.exists():
        return cand

    cwd_cand = (Path.cwd() / cand).resolve()
    if cwd_cand.exists():
        return cwd_cand

    root_cand = (_repo_root() / cand).resolve()
    if root_cand.exists():
        return root_cand

    tools_cand = (_repo_root() / "tools" / cand.name).resolve()
    if tools_cand.exists():
        return tools_cand

    raise FileNotFoundError(
        f"Input not found: {p}\n"
        f"  Tried: {cwd_cand}\n"
        f"         {root_cand}\n"
        f"         {tools_cand}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Convert a Wireshark JSON export (array or NDJSON) into JSON Lines (1 JSON per line)."
    )
    ap.add_argument("input", help="Wireshark export file (JSON array or NDJSON), or '-' for stdin")
    ap.add_argument("--handle", default="0x0010", help="ATT handle to match (default 0x0010)")
    ap.add_argument("--op", choices=["write", "notify", "any"], default="write", help="ATT op filter")
    ap.add_argument("--rx", choices=["no", "also", "only"], default="no", help="Include notifications (RX)")
    ap.add_argument(
        "-o",
        "--out",
        help="Output JSONL path (default: captures/<input_stem>.jsonl, or '-' for stdout)",
    )
    ap.add_argument("--pretty", action="store_true", help="Pretty-print each JSON line")
    args = ap.parse_args()

    # Resolve input stream
    if args.input == "-":
        in_path = None
        in_stream = sys.stdin
    else:
        in_path = _resolve_input(args.input)
        in_stream = in_path.open("r", encoding="utf-8")

    # Resolve output stream (default → captures/<input_stem>.jsonl)
    if args.out in (None, "") and in_path is not None:
        out_path = _captures_dir() / f"{in_path.stem}.jsonl"
    elif args.out == "-":
        out_path = None
    else:
        outp = Path(args.out).expanduser()
        out_path = (Path.cwd() / outp).resolve() if not outp.is_absolute() else outp

    # Open output
    if out_path is None:
        out_stream = sys.stdout
    else:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_stream = out_path.open("w", encoding="utf-8")

    try:
        rows = parse_wireshark_stream(in_stream, handle=args.handle, op=args.op, rx=args.rx)
        write_jsonl(rows, out_stream, pretty=args.pretty)
        if out_path:
            print(f"✅ Output written to: {out_path}", file=sys.stderr)
        return 0
    finally:
        if in_stream is not sys.stdin:
            in_stream.close()
        if out_stream is not sys.stdout:
            out_stream.close()


if __name__ == "__main__":
    raise SystemExit(main())
