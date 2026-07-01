"""Make the sibling ``triage.py`` importable as ``triage`` for the tests."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
