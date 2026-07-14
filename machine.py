#!/usr/bin/env python3
"""Thin launcher: same as ``smmachine.py`` (so ``python3 machine.py nchcs …`` works)."""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_TARGET = _HERE / "smmachine.py"
if not _TARGET.is_file():
    print(f"missing {_TARGET}", file=sys.stderr)
    raise SystemExit(2)
runpy.run_path(str(_TARGET), run_name="__main__")
