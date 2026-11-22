"""冲突分流、负责人裁定与执行人交接确认测试。"""

import unittest

from meetingq import conflicts, items
from meetingq.errors import AuthzError, ConflictError, StateError

from support import MeetingQTestCase


class ConflictTests(MeetingQTestCase):
    def _confirmed(self, proposal: str, owner: str) -> str:
        r = items.create_item_from_proposal(self.conn, owner, proposal, clock=self.clock)
        if r["status"] == "draft_proposal":
            items.request_confirmation(self.conn, owner, r["item_id"], clock=self.clock)
        items.confirm_item(self.conn, owner, r["item_id"], clock=self.clock)
        return r["item_id"]

    def test_rerun_conflict_blocks_unclaimed_item_from_claim(self):
        # A0 属 M0，M0 存在 v2 摘要 S0b/A0b
        item_id = self._confirmed("A0", "U2")
        found = conflicts.detect_conflicts(self.conn, clock=self.clock)
        self.assertTrue(any(c["item_id"] == item_id for c in found))
        self.assertEqual(items.get_item(self.conn, item_id)["status"], "claim_blocked")
        # 阻断期间禁止领取
        with self.assertRaises(StateError):
            items.claim_item(self.conn, "U4", item_id, clock=self.clock)
        # 负责人裁定前任务依然存在，没有被自动删除
        self.assertIsNotNone(items.get_item(self.conn, item_id))

    def test_in_progress_conflict_preserves_work_and_requires_ack(self):
        item_id = self._confirmed("A1", "U1")
        items.claim_item(self.conn, "U3", item_id, clock=self.clock)
        items.record_progress(self.conn, "U3", item_id, 2.5, "已投入工作", clock=self.clock)
        # 第二个 P1 事项（不同会议 M7），与 A1 事项并存；跨会议同主题仅在同主题时提示，
        # 这里直接验证执行中冲突的通知机制：手工打开冲突并模拟系统通知路径
        a7 = items.create_item_from_proposal(self.conn, "U1", "A7", clock=self.clock)
        items.confirm_item(self.conn, "U1", a7["item_id"], clock=self.clock)
        self.conn.execute(
            "INSERT INTO item_conflicts(conflict_id,item_id,new_proposal,conflict_type,"
            "status,detected_at) VALUES('CFL-IP',?,'A7','duplicate','open',?)",
            (item_id, self.clock.now()))
        self.conn.execute(
            "UPDATE items SET status='in_progress_impact', open_conflict_id='CFL-IP',"
            " row_version=row_version+1 WHERE item_id=?", (item_id,))
        self.conn.execute(
            "INSERT INTO handoff_notifications(notification_id,item_id,conflict_id,"
            "executor,created_at,status) VALUES('NTF-IP',?,'CFL-IP','U3',?,'pending')",
            (item_id, self.clock.now()))
        # 工时与原承诺完整保留
        detail = items.item_detail(self.conn, item_id)
        self.assertAlmostEqual(detail["hours_spent"], 2.5)
        self.assertEqual(detail["status"], "in_progress_impact")

        pending = conflicts.handoff_queue(self.conn, executor="U3")
        self.assertEqual(len(pending), 1)
        # 非接收人不能确认
        with self.assertRaises(AuthzError):
            conflicts.acknowledge_notification(
                self.conn, "U4", pending[0]["notification_id"], clock=self.clock)
        conflicts.acknowledge_notification(
            self.conn, "U3", pending[0]["notification_id"], "收到影响通知", clock=self.clock)
        self.assertEqual(
            conflicts.handoff_queue(self.conn, executor="U3")[0]["status"],
            "acknowledged")
        with self.assertRaises(ConflictError):
            conflicts.acknowledge_notification(
                self.conn, "U3", pending[0]["notification_id"], clock=self.clock)

    def test_cannot_decide_conflict_twice_and_non_owner_rejected(self):
        item_id = self._confirmed("A0", "U2")
        conflicts.detect_conflicts(self.conn, clock=self.clock)
        cfl = conflicts.list_open_conflicts(self.conn, project="P2")[0]
        with self.assertRaises(AuthzError):
            conflicts.decide_conflict(self.conn, "U1", cfl["conflict_id"],
                                      "uphold", "跨项目无权", clock=self.clock)
        conflicts.decide_conflict(self.conn, "U2", cfl["conflict_id"],
                                  "uphold", "维持", clock=self.clock)
        with self.assertRaises(StateError):
            conflicts.decide_conflict(self.conn, "U2", cfl["conflict_id"],
                                      "revoke", "重复裁定", clock=self.clock)
        # uphold 后事项恢复可领取
        self.assertEqual(items.get_item(self.conn, item_id)["status"], "confirmed")

    def test_revoke_refused_when_work_hours_exist(self):
        item_id = self._confirmed("A1", "U1")
        items.claim_item(self.conn, "U3", item_id, clock=self.clock)
        items.record_progress(self.conn, "U3", item_id, 1.0, "工时", clock=self.clock)
        # 手工打开一个执行中冲突
        self.conn.execute(
            "INSERT INTO item_conflicts(conflict_id,item_id,new_proposal,conflict_type,"
            "status,detected_at) VALUES('CFL-TEST',?,NULL,'withdraw','open',?)",
            (item_id, self.clock.now()))
        with self.assertRaises(StateError):
            conflicts.decide_conflict(self.conn, "U1", "CFL-TEST", "revoke",
                                      "不能抹掉工时", clock=self.clock)

    def test_new_suggestion_never_replaces_original_commitment(self):
        item_id = self._confirmed("A0", "U2")
        conflicts.detect_conflicts(self.conn, clock=self.clock)
        cfl = [c for c in conflicts.list_open_conflicts(self.conn, project="P2")
               if c["new_proposal"] == "A0b"][0]
        conflicts.decide_conflict(self.conn, "U2", cfl["conflict_id"], "uphold",
                                  "新建议不作为替换", clock=self.clock)
        detail = items.item_detail(self.conn, item_id)
        # 原事项仍只链接原提案 A0；A0b 没有自动替换链接
        self.assertNotIn("A0b", {l["proposal"] for l in detail["links"]})
        self.assertEqual(detail["status"], "confirmed")

    def test_handoff_survives_report_updates(self):
        # 通知是持久化行：执行多次报告查询不改变队列
        item_id = self._confirmed("A1", "U1")
        items.claim_item(self.conn, "U3", item_id, clock=self.clock)
        self.conn.execute(
            "INSERT INTO item_conflicts(conflict_id,item_id,new_proposal,conflict_type,"
            "status,detected_at) VALUES('CFL-Q',?,'A1','contradict','open',?)",
            (item_id, self.clock.now()))
        self.conn.execute(
            "INSERT INTO handoff_notifications(notification_id,item_id,conflict_id,"
            "executor,created_at,status) VALUES('NTF-Q',?,'CFL-Q','U3',?,'pending')",
            (item_id, self.clock.now()))
        from meetingq import reports
        reports.workload_dashboard(self.conn)
        reports.item_traceability(self.conn, item_id)
        queue = conflicts.handoff_queue(self.conn)
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
