#!/usr/bin/env python3
"""Punkt wejścia z katalogu repozytorium / instalacji (/opt/hvm-storage-manager/main.py) — logika w hvm/cli.py."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hvm.cli import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main())
