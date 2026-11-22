"""测试入口：``python -I tests/run_tests.py``。

-I 隔离模式忽略 PYTHONPATH 与当前目录，因此这里显式把仓库根目录加入 sys.path，
保证从任意工作目录启动都能导入 meetingq 包，且不依赖环境变量。
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

if __name__ == "__main__":
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(0 if result.wasSuccessful() else 1)
