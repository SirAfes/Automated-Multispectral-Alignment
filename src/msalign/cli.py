"""
cli.py

Command-line interface:

    msalign IMAGE_OR_FOLDER [...] [--config FILE] [--set KEY=VALUE ...]

Examples:
    msalign D:/survey/day1                      # every image in the folder
    msalign a.ARW b.ARW --set closure_check=warn
    msalign D:/survey/day1 --no-batch           # per-image alignment only
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import __version__
from .pipeline import expand_inputs, run_pipeline
from .settings import CONFIG_FILENAME, DEFAULT_SETTINGS, load_settings


def _parse_set(items):
    """KEY=VALUE pairs; VALUE is read as JSON when possible (numbers, lists, null)."""
    overrides = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"--set expects KEY=VALUE, got '{item}'")
        key, value = item.split("=", 1)
        try:
            overrides[key.strip()] = json.loads(value)
        except json.JSONDecodeError:
            overrides[key.strip()] = value
    return overrides


def build_parser():
    p = argparse.ArgumentParser(
        prog="msalign",
        description="Align the lens images of multi-lens multispectral photographs "
                    "and export aligned multiband TIFFs.",
    )
    p.add_argument("inputs", nargs="+", help="image files and/or folders")
    p.add_argument("--config", type=Path,
                   help=f"settings file (default: {CONFIG_FILENAME} next to the program, if present)")
    p.add_argument("--set", action="append", metavar="KEY=VALUE",
                   help="override one setting, e.g. --set closure_check=warn (repeatable)")
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--batch", action="store_true",
                      help="force batch consensus mode regardless of the number of images")
    mode.add_argument("--no-batch", action="store_true",
                      help="never use batch consensus mode")
    p.add_argument("--no-run-files", action="store_true",
                   help="do not write the run log and summary CSV")
    p.add_argument("-v", "--verbose", action="store_true", help="detailed alignment diagnostics")
    p.add_argument("-q", "--quiet", action="store_true", help="only print the final summary")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--list-settings", action="store_true",
                   help="print all settings with their current values and exit")
    return p


def main(argv=None) -> int:
    import multiprocessing

    multiprocessing.freeze_support()
    # --list-settings works without inputs
    argv = sys.argv[1:] if argv is None else argv
    if "--list-settings" in argv:
        argv = [a for a in argv if a != "--list-settings"]
        args, _ = build_parser().parse_known_args(argv + ["_"])
        settings = load_settings(args.config, _parse_set(args.set))
        print(json.dumps({k: v for k, v in settings.items() if k in DEFAULT_SETTINGS}, indent=2))
        return 0

    args = build_parser().parse_args(argv)

    overrides = _parse_set(args.set)
    if args.batch:
        overrides["batch_threshold"] = 1
    if args.no_batch:
        overrides["batch_threshold"] = 10 ** 9
    try:
        settings = load_settings(args.config, overrides)
    except (OSError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(message)s"))
    console.setLevel(logging.DEBUG if args.verbose else logging.WARNING)
    root = logging.getLogger("msalign")
    root.addHandler(console)
    root.setLevel(logging.DEBUG if args.verbose else logging.INFO)

    files = expand_inputs(args.inputs)
    missing = [f for f in files if not f.is_file()]
    if missing:
        print(f"Not found: {', '.join(map(str, missing))}", file=sys.stderr)
        return 2
    if not files:
        print("No supported images found.", file=sys.stderr)
        return 2

    total = len(files)
    counter = {"done": 0}

    def emit(event):
        kind = event[0]
        if kind in {"mode", "phase"} and not args.quiet:
            print(f"--- {event[1]}")
        elif kind == "success":
            counter["done"] += 1
            _, index, result, elapsed, _ = event
            if not args.quiet:
                extra = " (batch geometry)" if result.get("batch_consensus_used") else ""
                print(f"[{counter['done']}/{total}] OK    {files[index].name}: "
                      f"quality={result['quality']}, closure={result['closure_status']}, "
                      f"{elapsed:.1f}s{extra}")
        elif kind == "final_error":
            counter["done"] += 1
            _, index, error, _ = event
            print(f"[{counter['done']}/{total}] ERROR {files[index].name}: {error}")
        elif kind == "run_files" and not args.quiet:
            print(f"--- Log and summary: {event[2]}")
        elif kind == "retry_queued" and not args.quiet:
            print(f"        {files[event[1]].name}: queued for second pass ({event[2]})")

    try:
        success, failed = run_pipeline(
            files, emit, settings=settings, run_files=not args.no_run_files, verbose=args.verbose
        )
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    finally:
        root.removeHandler(console)

    print(f"Done: {len(success)} successful, {len(failed)} failed.")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
