"""
Makes `src/` importable as `repository` for the test suite, without
requiring a `pip install -e .` packaging step yet. Minimal on purpose —
block 1 is about the repository foundation, not project packaging; a proper
`pyproject.toml` can replace this later without any test code changing.
"""

import sys
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))
