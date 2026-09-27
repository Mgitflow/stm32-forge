# -*- coding: utf-8 -*-
"""conftest：core 根目录进 sys.path（与 src/api/server.py 的裸奔注入保持同一模式）。"""
from __future__ import annotations

import sys
from pathlib import Path

CORE_ROOT = Path(__file__).resolve().parent.parent
if str(CORE_ROOT) not in sys.path:
    sys.path.insert(0, str(CORE_ROOT))
