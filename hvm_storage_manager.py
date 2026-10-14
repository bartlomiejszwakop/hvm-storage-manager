#!/usr/bin/env python3
"""Zgodność wsteczna: `python3 hvm_storage_manager.py` uruchamia hvm.cli:main."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hvm import __version__  # noqa: E402,F401
from hvm.cli import main  # noqa: E402

if __name__ == '__main__':
    sys.exit(main())
