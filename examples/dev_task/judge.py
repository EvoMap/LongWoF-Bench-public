#!/usr/bin/env python3
"""Public judge for the non-scored D0001 development example."""

from pathlib import Path
import sys


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: judge.py CANDIDATE", file=sys.stderr)
        return 2
    answer = Path(sys.argv[1]).read_text(encoding="utf-8").strip()
    passed = answer == "ANSWER: 42"
    print("PASS" if passed else "FAIL")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
