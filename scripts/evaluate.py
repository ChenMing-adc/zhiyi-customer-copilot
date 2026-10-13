"""Offline evaluation entrypoint; never starts the product or calls a model."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evaluation.cli import main

if __name__ == '__main__':
    raise SystemExit(main())
