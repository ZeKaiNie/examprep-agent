from __future__ import annotations

import sys
from pathlib import Path

# importlib 模式下测试目录不在 sys.path 上；让测试能 import helpers
HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

REPO = HERE.parents[2]
SAMPLES = REPO / "samples"
