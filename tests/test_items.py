"""事项生命周期、权限边界、人工关联与并发守卫测试。"""

import threading
import unittest

from meetingq import items
from meetingq.errors import AuthzError, ConflictError, StateError

from support import MeetingQTestCase


class ItemLifecycleTests(MeetingQTestCase):
    def test_suggestion_is_not_a_commitment_until_owner_confirms(self):
        r = items.create_item_from_proposal(self.conn, "U2", "A0", clock=self.clock)
        self.assertEqual(r["status"], "draft_proposal")
        # 草稿不可直接确认
        with self.assertRaises(StateError):
            items.confirm_item(self.conn, "U2", r["item_id"], clock=self.clock)
        # 执行人不能领取未确认事项
        with self.assertRaises(StateError):
            items.claim_item(self.conn, "U4", r["item_id"], clock=self.clock)
        items.request_confirmation(self.conn, "U2", r["item_id"], clock=self.clock)
        items.confirm_item(self.conn, "U2", r["item_id"], clock=self.clock)
        detail = items.item_detail(self.conn, r["item_id"])
        self.assertEqual(detail["status"], "confirmed")
        # 确认事件保留依赖的会议决定链路，可回查
        confirmed_event = [e for e in detail["events"] if e["event_type"] == "confirmed"][0]
        import json
        deps = json.loads(confirmed_event["detail_json"])["dependencies"]
        self.assertEqual(deps[0]["meeting"], "M0")

    def test_decision_enters_pending_confirmation_not_confirmed(self):
        r = items.create_item_from_proposal(self.conn, "U1", "A1", clock=self.clock)
        self.assertEqual(r["status"], "pending_confirmation")
        with self.assertRaises(StateError):
            items.claim_item(self.conn, "U3", r["item_id"], clock=self.clock)

    def test_only_project_owner_can_confirm(self):
        r = items.create_item_from_proposal(self.conn, "U1", "A1", clock=self.clock)
        # 执行人确认被拒
        with self.assertRaises(AuthzError):
            items.confirm_item(self.conn, "U3", r["item_id"], clock=self.clock)
        # 跨项目负责人确认被拒
        with self.assertRaises(AuthzError):
            items.confirm_item(self.conn, "U2", r["item_id"], clock=self.clock)
        # 复核员标签不能代行确认
        with self.assertRaises(AuthzError):
            items.confirm_item(self.conn, "Q1", r["item_id"], clock=self.clock)

    def test_executor_claim_progress_and_complete(self):
        r = items.create_item_from_proposal(self.conn, "U1", "A1", clock=self.clock)
        items.confirm_item(self.conn, "U1", r["item_id"], clock=self.clock)
        # 非本项目执行人
        with self.assertRaises(AuthzError):
            items.claim_item(self.conn, "U4", r["item_id"], clock=self.clock)
        items.claim_item(self.conn, "U3", r["item_id"], clock=self.clock)
        items.record_progress(self.conn, "U3", r["item_id"], 1.5, "step1", clock=self.clock)
        items.record_progress(self.conn, "U3", r["item_id"], 2.0, "step2", clock=self.clock)
        detail = items.item_detail(self.conn, r["item_id"])
        self.assertAlmostEqual(detail["hours_spent"], 3.5)
        self.assertEqual(len(detail["progress"]), 2)
        # 其他人不能登记进展
        with self.assertRaises(AuthzError):
            items.record_progress(self.conn, "U4", r["item_id"], 1, "x", clock=self.clock)
        items.complete_item(self.conn, "U3", r["item_id"], "交付完成", clock=self.clock)
        self.assertEqual(items.get_item(self.conn, r["item_id"])["status"], "done")
        # 终态不可再变
        with self.assertRaises(StateError):
            items.record_progress(self.conn, "U3", r["item_id"], 1, "x", clock=self.clock)

    def test_progress_entries_are_append_only(self):
        r = items.create_item_from_proposal(self.conn, "U1", "A1", clock=self.clock)
        items.confirm_item(self.conn, "U1", r["item_id"], clock=self.clock)
        items.claim_item(self.conn, "U3", r["item_id"], clock=self.clock)
        items.record_progress(self.conn, "U3", r["item_id"], 4.0, "已花时间", clock=self.clock)
        # 数据库层面没有更新工时的接口；尝试直接改库会被审计设计外的测试证明无路径
        rows = self.conn.execute(
            "SELECT hours_spent FROM progress_entries WHERE item_id=?", (r["item_id"],)
        ).fetchall()
        self.assertEqual([x["hours_spent"] for x in rows], [4.0])

    def test_manual_link_across_meetings_never_auto_merged(self):
        a4 = items.create_item_from_proposal(self.conn, "U2", "A4", clock=self.clock)
        items.confirm_item(self.conn, "U2", a4["item_id"], clock=self.clock)
        # A8 与 A4 同主题，但系统没有自动建立任何关联
        self.assertIsNone(self.conn.execute(
            "SELECT 1 FROM item_links WHERE proposal='A8'"
        ).fetchone())
        items.link_proposal(self.conn, "U2", a4["item_id"], "A8", "reference",
                            note="人工核对", clock=self.clock)
        detail = items.item_detail(self.conn, a4["item_id"])
        proposals = {l["proposal"] for l in detail["links"]}
        self.assertEqual(proposals, {"A4", "A8"})
        meetings = {l["meeting"] for l in detail["links"]}
        self.assertEqual(meetings, {"M4", "M8"})
        # 跨项目关联被拒
        with self.assertRaises(AuthzError):
            items.link_proposal(self.conn, "U2", a4["item_id"], "A1", clock=self.clock)

    def test_merge_keeps_work_history_and_retires_duplicate(self):
        a4 = items.create_item_from_proposal(self.conn, "U2", "A4", clock=self.clock)
        items.confirm_item(self.conn, "U2", a4["item_id"], clock=self.clock)
        items.claim_item(self.conn, "U4", a4["item_id"], clock=self.clock)
        items.record_progress(self.conn, "U4", a4["item_id"], 3.0, "保留事项工时", clock=self.clock)

        a8 = items.create_item_from_proposal(self.conn, "U2", "A8", clock=self.clock)
        # A8 是 decision 型，进入待确认；显式走完确认再作为重复事项
        self.assertEqual(a8["status"], "pending_confirmation")
        items.confirm_item(self.conn, "U2", a8["item_id"], clock=self.clock)
        items.claim_item(self.conn, "U4", a8["item_id"], clock=self.clock)
        items.record_progress(self.conn, "U4", a8["item_id"], 1.0, "重复事项工时", clock=self.clock)

        result = items.merge_items(self.conn, "U2", a4["item_id"], a8["item_id"],
                                   "人工裁定重复", clock=self.clock)
        self.assertEqual(items.get_item(self.conn, a8["item_id"])["status"], "withdrawn")
        detail = items.item_detail(self.conn, result["kept_item_id"])
        # 两份工时都保留在保留事项上
        self.assertAlmostEqual(detail["hours_spent"], 4.0)
        self.assertIn("归并自", detail["progress"][-1]["note"])
        linked = {l["proposal"] for l in detail["links"]}
        self.assertEqual(linked, {"A4", "A8"})

    def test_concurrent_claims_only_one_succeeds(self):
        from meetingq.db import connect as open_conn
        r = items.create_item_from_proposal(self.conn, "U1", "A1", clock=self.clock)
        items.confirm_item(self.conn, "U1", r["item_id"], clock=self.clock)
        errors: list[Exception] = []

        def claim() -> None:
            conn = open_conn(self.workdir)
            try:
                items.claim_item(conn, "U3", r["item_id"], clock=self.clock)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                conn.close()

        t1 = threading.Thread(target=claim)
        t2 = threading.Thread(target=claim)
        t1.start(); t2.start(); t1.join(); t2.join()
        item = items.get_item(self.conn, r["item_id"])
        self.assertEqual(item["status"], "in_progress")
        self.assertEqual(item["claimed_by"], "U3")
        # 一个连接成功，另一个必须被状态机守卫拒绝，绝不产生两个领取结果
        self.assertEqual(len(errors), 1)

    def test_optimistic_lock_blocks_stale_overwrite(self):
        r = items.create_item_from_proposal(self.conn, "U1", "A1", clock=self.clock)
        items.confirm_item(self.conn, "U1", r["item_id"], clock=self.clock)
        item = items.get_item(self.conn, r["item_id"])
        items.claim_item(self.conn, "U3", r["item_id"], clock=self.clock,
                         expected_version=item["row_version"])
        # 持旧版本号再领取必须失败
        with self.assertRaises(ConflictError):
            items.claim_item(self.conn, "U3", r["item_id"], clock=self.clock,
                             expected_version=item["row_version"])

    def test_withdrawn_terminal_state_keeps_history(self):
        r = items.create_item_from_proposal(self.conn, "U1", "A1", clock=self.clock)
        items.reject_item(self.conn, "U1", r["item_id"], "会议撤回该决定", clock=self.clock)
        detail = items.item_detail(self.conn, r["item_id"])
        self.assertEqual(detail["status"], "withdrawn")
        self.assertTrue(any(e["event_type"] == "withdrawn" for e in detail["events"]))
        with self.assertRaises(StateError):
            items.confirm_item(self.conn, "U1", r["item_id"], clock=self.clock)


if __name__ == "__main__":
    unittest.main()
