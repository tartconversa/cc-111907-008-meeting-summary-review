"""tests 包初始化：把仓库根目录加入 sys.path。

兼容两种启动方式：
- ``python -I tests/run_tests.py``（推荐，隔离模式）
- ``python -m unittest discover -s tests``（仓库既有入口）
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
