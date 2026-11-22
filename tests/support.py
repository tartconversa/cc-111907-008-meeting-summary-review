"""测试公共夹具：每个用例独立临时工作目录，输入目录只读。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from meetingq.db import connect, initialize  # noqa: E402
from meetingq import importer  # noqa: E402
from meetingq.timeutil import Clock  # noqa: E402

INPUTS = ROOT / "inputs"
FIXED_NOW = "2026-11-23T08:00:00Z"


class MeetingQTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.workdir = Path(self._tmp.name) / "work"
        self.conn = connect(self.workdir)
        initialize(self.conn)
        self.clock = Clock.fixed(FIXED_NOW)
        importer.import_sources(self.conn, INPUTS, clock=self.clock)

    def tearDown(self) -> None:
        self.conn.close()
        self._tmp.cleanup()

    def item_id_for(self, proposal: str) -> str:
        row = self.conn.execute(
            "SELECT item_id FROM item_links WHERE proposal=?", (proposal,)
        ).fetchone()
        self.assertIsNotNone(row, f"提案 {proposal} 尚未关联事项")
        return row["item_id"]
