from __future__ import annotations

from pathlib import Path
import sys

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
if str(SRC_DIR) not in sys.path:
	sys.path.insert(0, str(SRC_DIR))

from app.main import run_once


if __name__ == "__main__":
	processed = run_once()
	print(f"Processed {processed} email(s)")
