"""入口：``python -I meetingq ...``。

使用目录路径启动时，Python 会把该目录加入 sys.path，
因此即使采用 -I 隔离模式（忽略 PYTHONPATH 与当前目录）也能导入同包模块。
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from meetingq.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
