#!/usr/bin/env python3
import argparse
import hashlib
import math
import mmap
import re
import shlex
import struct
import sys
from pathlib import Path

EXT_RE = re.compile(rb"\.[A-Za-z0-9]{2,5}\x00")
HDR = struct.Struct("<IIIIQ")
ZONE = struct.Struct("<6I4Q")
MIN_RATE, MAX_RATE = 4000, 384000


def find_samples(d):
    out = []
    pos = 0
    while True:
        m = EXT_RE.search(d, pos)
        if not m:
            break
        e = m.end()
        pos = m.end()
        if e + HDR.size > len(d):
            continue
        bps, sr, ch, _, frames = HDR.unpack_from(d, e)
        if not (1 <= ch <= 8 and MIN_RATE <= sr <= MAX_RATE and bps in (1, 2, 3, 4) and frames > 0):
            continue
        start = e + HDR.size
        end = start + frames * ch * bps
        if end > len(d):
            continue
        lo = max(0, m.start() - 1024)
        raw_name = d[max(lo, d.rfind(b"\x00", lo, m.start()) + 1):m.end() - 1]
        name = re.split(rb"[\\/]", raw_name)[-1].decode("utf-8", "replace")
        out.append((name, ch, sr, bps, frames, start, read_loop(d, end, frames)))
        pos = end
    return out


def read_loop(d, off, frames):
    if off + ZONE.size > len(d):
        return None
    root, _, _, _, _, mode, _, _, ls, le = ZONE.unpack_from(d, off)
    if root > 127 or mode == 0 or not (ls < le < frames):
        return None
    return root, ls, le


def is_float(pcm, bps):
    if bps != 4 or len(pcm) % 4:
        return False
    values = memoryview(pcm).cast("f")
    step = max(1, len(values) // 65536)
    return all(math.isfinite(x) and abs(x) <= 16 for x in values[::step])


def wav_bytes(pcm, ch, sr, bps, loop, float_data=False):
    fmt_chunk = struct.pack("<HHIIHH", 3 if float_data else 1, ch, sr, sr * ch * bps, ch * bps, bps * 8)
    if float_data:
        fmt_chunk += struct.pack("<H", 0)
    chunks = [b"fmt " + struct.pack("<I", len(fmt_chunk)) + fmt_chunk]
    if float_data:
        chunks.append(b"fact" + struct.pack("<II", 4, len(pcm) // (ch * bps)))
    data = bytes(pcm)
    chunks.append(b"data" + struct.pack("<I", len(data)) + data + (b"\x00" if len(data) % 2 else b""))
    if loop:
        root, ls, le = loop
        period = round(1e9 / sr)
        smpl = struct.pack("<9I", 0, 0, period, root, 0, 0, 0, 1, 0)
        smpl += struct.pack("<6I", 0, 0, ls, le, 0, 0)
        chunks.append(b"smpl" + struct.pack("<I", len(smpl)) + smpl)
    body = b"WAVE" + b"".join(chunks)
    return b"RIFF" + struct.pack("<I", len(body)) + body


def safe_name(s):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s).strip() or "sample"


def extract(mse_path, out_dir, claimed, write_loops=True, overwrite=False):
    with open(mse_path, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as d:
        if d[:4] != b"MSE ":
            print(f"  ! {mse_path.name}: not an MSE file, skipped")
            return 0, 0, 0
        samples = find_samples(d)
        if not samples:
            print(f"  ! {mse_path.name}: no samples found")
            return 0, 0, 0
        out_dir.mkdir(parents=True, exist_ok=True)
        written = skipped = shared = 0
        for name, ch, sr, bps, frames, start, loop in samples:
            pcm = d[start:start + frames * ch * bps]
            float_data = is_float(pcm, bps)
            data = wav_bytes(pcm, ch, sr, bps, loop if write_loops else None, float_data)
            digest = hashlib.sha1(struct.pack("<3I", ch, sr, bps) + pcm).digest()
            stem = Path(safe_name(name)).stem
            fname, i = stem + ".wav", 2
            while True:
                key = (out_dir, fname.lower())
                dest = out_dir / fname
                if key in claimed:
                    if claimed[key] == digest:
                        status = "shared"
                        break
                elif not dest.exists() or overwrite:
                    status = "write"
                    break
                elif dest.stat().st_size == len(data) and dest.read_bytes() == data:
                    status = "exists"
                    break
                fname, i = f"{stem} ({i}).wav", i + 1
            claimed[key] = digest
            if status == "shared":
                print(f"    ~ {fname} already extracted from another .mse, skipped")
                shared += 1
            elif status == "exists":
                print(f"    = {fname} already exists, skipped")
                skipped += 1
            else:
                dest.write_bytes(data)
                info = f"{ch}ch {sr}Hz {bps * 8}bit{' float' if float_data else ''} {frames / sr:.2f}s"
                if loop and write_loops:
                    info += f" loop {loop[1]}-{loop[2]}"
                print(f"    + {fname}  ({info})")
                written += 1
        return written, skipped, shared


def collect(inp):
    if inp.is_dir():
        return sorted(p for p in inp.rglob("*") if p.suffix.lower() == ".mse" and p.is_file()), inp
    if inp.is_file():
        return [inp], inp.parent
    return [], None


def folder_name(name):
    return name[:-len(".instruments")] if name.lower().endswith(".instruments") else name


def default_output(base):
    return base.parent / (folder_name(base.name) + "_wav")


def run(inputs, output=None, per_mse=False, write_loops=True, overwrite=False):
    total_files = total_written = total_skipped = total_shared = 0
    claimed = {}
    for inp in inputs:
        inp = inp.expanduser().resolve()
        files, base = collect(inp)
        if base is None:
            print(f"! {inp} not found", file=sys.stderr)
            continue
        out_root = output.expanduser().resolve() if output else default_output(base)
        for f in files:
            rel = f.relative_to(base)
            target = out_root.joinpath(*(folder_name(p) for p in rel.parent.parts))
            if per_mse:
                target /= f.stem
            print(f"{rel}")
            try:
                w, s, sh = extract(f, target, claimed, write_loops, overwrite)
                total_written += w
                total_skipped += s
                total_shared += sh
                total_files += 1
            except Exception as ex:
                print(f"  ! error on {f.name}: {ex}", file=sys.stderr)
        print(f"-> output in: {out_root}")
    print(f"\nDone: {total_written} WAVs written, {total_skipped} skipped (already existing), "
          f"{total_shared} shared between .mse files, from {total_files} .mse files")


def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        raise KeyboardInterrupt


def parse_paths(text):
    if not text:
        return []
    if Path(text).expanduser().exists():
        return [Path(text)]
    try:
        parts = shlex.split(text)
    except ValueError:
        parts = [text.strip("'\"")]
    return [Path(p) for p in parts]


def clear():
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")


def menu():
    inputs, output = [], None
    per_mse, write_loops, overwrite = False, True, False
    msg = ""
    while True:
        clear()
        found = sum(len(collect(p.expanduser().resolve())[0]) for p in inputs)
        if inputs:
            first_base = collect(inputs[0].expanduser().resolve())[1]
            out_label = str(output) if output else (
                f"{default_output(first_base)}  (default)" if first_base else "(default)")
            in_label = "\n            ".join(str(p) for p in inputs) + f"\n            -> {found} .mse file(s) found"
        else:
            in_label, out_label = "(not set)", str(output) if output else "(default: <input>_wav)"

        print("=" * 52)
        print("  MSE EXTRACT TOOL")
        print("=" * 52)
        print(f"  Input:    {in_label}")
        print(f"  Output:   {out_label}")
        print(f"  Layout:   {'one subfolder per .mse' if per_mse else 'one folder per library'}")
        print(f"  Loops:    {'write root note + loop points' if write_loops else 'off'}")
        print(f"  Existing: {'overwrite' if overwrite else 'skip'}")
        print("-" * 52)
        print("  1) Set input (folder or .mse file)")
        print("  2) Set output folder")
        print("  3) Toggle layout (per library / per .mse)")
        print("  4) Toggle loop points (on / off)")
        print("  5) Toggle existing files (skip / overwrite)")
        print("  6) Extract")
        print("  0) Quit")
        print("-" * 52)
        if msg:
            print(f"  {msg}\n")
            msg = ""

        choice = ask("Choose an option: ")
        if choice == "1":
            paths = parse_paths(ask("Input path (you can drag a folder here): "))
            missing = [p for p in paths if not p.expanduser().exists()]
            if not paths:
                msg = "No path entered."
            elif missing:
                msg = f"Not found: {missing[0]}"
            else:
                inputs = paths
        elif choice == "2":
            paths = parse_paths(ask("Output folder (leave empty for default): "))
            output = paths[0] if paths else None
        elif choice == "3":
            per_mse = not per_mse
        elif choice == "4":
            write_loops = not write_loops
        elif choice == "5":
            overwrite = not overwrite
        elif choice == "6":
            if not inputs:
                msg = "Set an input first (option 1)."
            elif not found:
                msg = "No .mse files in the selected input."
            else:
                clear()
                run(inputs, output, per_mse, write_loops, overwrite)
                ask("\nPress Enter to return to the menu...")
        elif choice == "0":
            return
        else:
            msg = "Invalid option."


def main():
    if len(sys.argv) == 1:
        try:
            menu()
        except KeyboardInterrupt:
            print()
        return

    ap = argparse.ArgumentParser(description="Extract WAVs from .mse files (whole folders too, recursively). "
                                             "Run without arguments for the interactive menu.")
    ap.add_argument("inputs", nargs="+", type=Path, help=".mse files or folders")
    ap.add_argument("-o", "--output", type=Path, help="output folder (default: <input>_wav next to the input)")
    ap.add_argument("--per-mse", action="store_true",
                    help="one subfolder per .mse (default: one folder per library, WAVs directly inside)")
    ap.add_argument("--no-loops", action="store_true", help="don't write root note and loop points (smpl chunk)")
    ap.add_argument("--overwrite", action="store_true", help="overwrite existing WAVs")
    args = ap.parse_args()
    run(args.inputs, args.output, args.per_mse, not args.no_loops, args.overwrite)


if __name__ == "__main__":
    main()
