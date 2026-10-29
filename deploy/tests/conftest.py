"""RT-927: make the deploy/lab helper modules importable from the tests (stdlib only)."""

import sys
from pathlib import Path

LAB = Path(__file__).resolve().parents[1] / "lab"
if str(LAB) not in sys.path:
    sys.path.insert(0, str(LAB))
