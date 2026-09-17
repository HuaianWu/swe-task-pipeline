#!/usr/bin/env python3
"""Entry point: python3 tools/swepipe.py <command>  (see tools/swepipe/cli.py or README.md)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from swepipe.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
