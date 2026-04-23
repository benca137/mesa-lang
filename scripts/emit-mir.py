#!/usr/bin/env python3.14
from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.frontend import build_frontend_state_for_path
from src.mirgen import MIRGenError, emit_mir_for_frontend


def main() -> int:
    ap = argparse.ArgumentParser(description="Emit experimental Mesa MIR")
    ap.add_argument("file", help="Mesa source file")
    args = ap.parse_args()

    state = build_frontend_state_for_path(args.file)
    if state.tokenize_error is not None:
        print(f"tokenize error: {state.tokenize_error}", file=sys.stderr)
        return 1
    if state.parse_error is not None:
        print(f"parse error: {state.parse_error}", file=sys.stderr)
        return 1
    if state.diags.has_errors():
        for diag in state.diags.all_errors():
            loc = f"[{diag.line}:{diag.col}] " if diag.line else ""
            print(f"error {loc}{diag.message}", file=sys.stderr)
            if diag.hint:
                print(f"hint: {diag.hint}", file=sys.stderr)
        return 1

    try:
        module = emit_mir_for_frontend(state)
    except MIRGenError as exc:
        print(f"mir error: {exc}", file=sys.stderr)
        return 1

    sys.stdout.write(module.render())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
