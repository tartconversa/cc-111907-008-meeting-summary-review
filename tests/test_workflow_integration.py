"""工作目录隔离与端到端演练测试。"""

import unittest
from pathlib import Path

from meetingq import importer, workflow
from meetingq.db import connect, initialize
from meetingq.timeutil import Clock

from support import INPUTS, MeetingQTestCase


class WorkdirIsolationTests(MeetingQTestCase):
    def test_separate_workdirs_do_not_share_state(self):
        other = Path(self._tmp.name) / "other_work"
        conn2 = connect(other)
        initialize(conn2)
        importer.import_sources(conn2, INPUTS, clock=Clock.fixed("2026-11-23T10:00:00Z"))
        # 另一个工作目录只导入不建事项；本目录建的事项不可见
        from meetingq import items
        items.create_item_from_proposal(self.conn, "U1", "A1", clock=self.clock)
        self.assertEqual(
            conn2.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"], 0)
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"], 1)
        # 状态库文件确实分属两个目录
        self.assertTrue((other / "meetingq.sqlite3").exists())
        self.assertTrue((self.workdir / "meetingq.sqlite3").exists())
        conn2.close()

    def test_inputs_directory_is_never_written(self):
        before = sorted(p.name for p in INPUTS.iterdir())
        importer.import_sources(self.conn, INPUTS, clock=self.clock)
        after = sorted(p.name for p in INPUTS.iterdir())
        self.assertEqual(before, after)


class DemoWorkflowTests(MeetingQTestCase):
    def test_demo_runs_and_closes_all_loops(self):
        conn = connect(self.workdir)
        initialize(conn)
        result = workflow.run_demo(
            conn, INPUTS, Clock.fixed("2026-11-23T12:00:00Z"))
        self.assertTrue(result["log"])
        # 所有来源冲突均已裁定
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) AS c FROM item_conflicts WHERE status='open'"
            ).fetchone()["c"], 0)
        # 所有交接通知均已确认
        self.assertEqual(
            self.conn.execute(
                "SELECT COUNT(*) AS c FROM handoff_notifications WHERE status<>'acknowledged'"
            ).fetchone()["c"], 0)
        # 轮次已关闭且报告只基于概率样本
        self.assertEqual(result["report"]["estimate"]["basis"], "probability_only")
        # 样本不足时明确不建议降配
        self.assertEqual(result["report"]["reduction_advice"]["advice"], "hold")
        # 执行中事项的工时在冲突后完整保留
        row = self.conn.execute(
            "SELECT COALESCE(SUM(hours_spent),0) AS h FROM progress_entries"
        ).fetchone()
        self.assertGreaterEqual(row["h"], 2.5)

    def test_demo_is_repeatable_on_fresh_workdir(self):
        clk = Clock.fixed("2026-11-23T12:00:00Z")
        r1 = workflow.run_demo(connect(self.workdir), INPUTS, clk)
        other = Path(self._tmp.name) / "repeat_work"
        conn2 = connect(other)
        initialize(conn2)
        r2 = workflow.run_demo(conn2, INPUTS, clk)
        self.assertEqual(len(r1["log"]), len(r2["log"]))


if __name__ == "__main__":
    unittest.main()
