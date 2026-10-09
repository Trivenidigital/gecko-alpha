#!/usr/bin/env python3
"""Spec S1 guard: gecko/ <= 2800 code lines, tests/gecko <= 5000. Exit 1 on breach."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

LIMITS = {"gecko": 2800, "tests/gecko": 5000}


def count(root: Path, rel: str) -> int:
    n = 0
    for f in (root / rel).rglob("*.py"):
        if "__pycache__" in f.parts:
            continue
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            s = line.strip()
            if s and not s.startswith("#"):
                n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    root = Path(ap.parse_args().root)
    missing = [rel for rel in LIMITS if not (root / rel).is_dir()]
    if missing:
        print(f"counted dir(s) missing under {root}: {missing}", file=sys.stderr)
        return 2
    g, t = count(root, "gecko"), count(root, "tests/gecko")
    print(f"gecko={g}/{LIMITS['gecko']} tests={t}/{LIMITS['tests/gecko']}")
    return 1 if g > LIMITS["gecko"] or t > LIMITS["tests/gecko"] else 0


if __name__ == "__main__":
    sys.exit(main())
