"""抽样、复核裁定、错误负担估计与结论落账测试。"""

import json
import threading
import unittest

from meetingq import items, reports, reviews, sampling
from meetingq.errors import AuthzError, ConflictError, SamplingError, StateError
from meetingq.sampling import plan_sample

from support import MeetingQTestCase


class SamplingTests(MeetingQTestCase):
    def test_plan_respects_capacity_and_ordinary_floor(self):
        policy = reviews.latest_policy(self.conn)
        # 容量 = 120 // 10 = 12；普通样本最低 ceil(12*0.25)=3
        plan = plan_sample(self.conn, policy, seed=42, targeted_proposals=["A1"])
        self.assertEqual(len(plan["tasks"]), 12)
        n_prob = sum(1 for t in plan["tasks"] if t["sample_kind"] == "probability")
        n_target = sum(1 for t in plan["tasks"] if t["sample_kind"] == "targeted")
        self.assertEqual(n_target, 1)
        self.assertGreaterEqual(n_prob, 3)
        # 概率任务全部保存入选概率；定向任务没有
        for t in plan["tasks"]:
            if t["sample_kind"] == "probability":
                self.assertIsNotNone(t["inclusion_prob"])
                self.assertGreater(t["inclusion_prob"], 0)
                self.assertLessEqual(t["inclusion_prob"], 1)
            else:
                self.assertIsNone(t["inclusion_prob"])

    def test_sampling_is_reproducible_with_seed(self):
        policy = reviews.latest_policy(self.conn)
        p1 = plan_sample(self.conn, policy, seed=7)
        p2 = plan_sample(self.conn, policy, seed=7)
        self.assertEqual([t["proposal"] for t in p1["tasks"]],
                         [t["proposal"] for t in p2["tasks"]])
        p3 = plan_sample(self.conn, policy, seed=8)
        self.assertNotEqual([t["proposal"] for t in p1["tasks"]],
                            [t["proposal"] for t in p3["tasks"]])

    def test_targeted_overflow_rejected(self):
        policy = reviews.latest_policy(self.conn)
        all_proposals = [r["proposal"] for r in self.conn.execute(
            "SELECT proposal FROM proposals").fetchall()]
        with self.assertRaises(SamplingError):
            plan_sample(self.conn, policy, seed=1,
                        targeted_proposals=all_proposals[:12])

    def test_scope_explicitly_records_frame_and_strata(self):
        policy = reviews.latest_policy(self.conn)
        plan = plan_sample(self.conn, policy, seed=5)
        self.assertEqual(plan["scope"]["frame_size"], 31)
        self.assertEqual(sum(plan["scope"]["strata"].values()), 31)


class ReviewWorkflowTests(MeetingQTestCase):
    def _round(self, target=None):
        return reviews.create_round(self.conn, "Q1", seed=101,
                                    targeted_proposals=target or [],
                                    clock=self.clock)

    def test_dual_reviewer_conflict_not_certain_until_adjudicated(self):
        rnd = self._round()
        task = next(t for t in reviews.list_tasks(self.conn, rnd["round_id"])
                    if t["project"] == "P1")
        reviews.submit_label(self.conn, "Q1", task["task_id"], "accepted", clock=self.clock)
        reviews.submit_label(self.conn, "Q2", task["task_id"], "extra", clock=self.clock)
        refreshed = [t for t in reviews.list_tasks(self.conn, rnd["round_id"])
                     if t["task_id"] == task["task_id"]][0]
        self.assertEqual(refreshed["final_label"], "conflict")
        self.assertEqual(refreshed["status"], "assigned")
        # 已投票复核员不能裁定自己的分歧
        with self.assertRaises(ConflictError):
            reviews.adjudicate_label(self.conn, "Q1", task["task_id"], "extra",
                                     "回避", clock=self.clock)
        reviews.adjudicate_label(self.conn, "U1", task["task_id"], "extra",
                                 "负责人裁定", clock=self.clock)
        done = [t for t in reviews.list_tasks(self.conn, rnd["round_id"])
                if t["task_id"] == task["task_id"]][0]
        self.assertEqual(done["status"], "adjudicated")
        self.assertEqual(done["final_label"], "extra")
        # 裁定不可重复
        with self.assertRaises(ConflictError):
            reviews.adjudicate_label(self.conn, "U1", task["task_id"], "accepted",
                                     "重复裁定", clock=self.clock)

    def test_unknown_is_never_counted_as_correct(self):
        rnd = self._round()
        task = reviews.list_tasks(self.conn, rnd["round_id"])[0]
        rv1, rv2 = ("Q1", "Q2") if task["project"] == "P1" else ("Q3", "Q3")
        reviews.submit_label(self.conn, rv1, task["task_id"], "unknown", clock=self.clock)
        if rv2 != rv1:
            reviews.submit_label(self.conn, rv2, task["task_id"], "unknown", clock=self.clock)
            reviews.auto_adjudicate_consensus(self.conn, rnd["round_id"])
        else:
            # P2 单复核员：负责人裁定 unknown 之外，这里直接验证 unknown 不自动确定
            reviews.auto_adjudicate_consensus(self.conn, rnd["round_id"])
        t = [x for x in reviews.list_tasks(self.conn, rnd["round_id"])
             if x["task_id"] == task["task_id"]][0]
        # 单人 unknown 不会自动形成 final_label；双人 unknown 一致才确定为 unknown
        if rv2 == rv1:
            self.assertIsNone(t["final_label"])
            self.assertEqual(t["status"], "assigned")
        else:
            self.assertEqual(t["final_label"], "unknown")
        # unknown 任务不能处置为业务动作
        with self.assertRaises(StateError):
            reviews.apply_adjudicated_outcome(self.conn, "Q1", task["task_id"],
                                              clock=self.clock)

    def test_reviewer_cannot_make_business_commitments(self):
        # missing 标签只能建待确认草稿，复核员无权 confirm
        rnd = self._round()
        # 选一个 P1 任务，保证 Q1/Q2 都有权标注
        task = next(t for t in reviews.list_tasks(self.conn, rnd["round_id"])
                    if t["project"] == "P1")
        reviews.submit_label(self.conn, "Q1", task["task_id"], "missing", clock=self.clock)
        reviews.submit_label(self.conn, "Q2", task["task_id"], "missing", clock=self.clock)
        reviews.auto_adjudicate_consensus(self.conn, rnd["round_id"])
        out = reviews.apply_adjudicated_outcome(self.conn, "Q1", task["task_id"],
                                                clock=self.clock)
        self.assertEqual(out["effect"], "draft_proposal_created")
        item = items.get_item(self.conn, out["item_id"])
        self.assertEqual(item["status"], "draft_proposal")
        self.assertEqual(item["origin"], "review_missing")
        # 复核员无权确认业务事项（负责人才能承诺）
        with self.assertRaises(AuthzError):
            items.confirm_item(self.conn, "Q1", out["item_id"], clock=self.clock)

    def test_extra_label_queues_owner_decision_without_revoking(self):
        # 建立并确认一个事项，再让复核任务判 extra
        r = items.create_item_from_proposal(self.conn, "U1", "A5", clock=self.clock)
        items.confirm_item(self.conn, "U1", r["item_id"], clock=self.clock)
        rnd = self._round(target=["A5"])
        task = [t for t in reviews.list_tasks(self.conn, rnd["round_id"])
                if t["proposal"] == "A5"][0]
        reviews.submit_label(self.conn, "Q1", task["task_id"], "extra", clock=self.clock)
        reviews.submit_label(self.conn, "Q2", task["task_id"], "extra", clock=self.clock)
        reviews.auto_adjudicate_consensus(self.conn, rnd["round_id"])
        out = reviews.apply_adjudicated_outcome(self.conn, "Q1", task["task_id"],
                                                clock=self.clock)
        self.assertEqual(out["effect"], "conflict_queued")
        # 事项未被删除，而是 claim_blocked 等待负责人
        self.assertEqual(items.get_item(self.conn, r["item_id"])["status"], "claim_blocked")
        # 重复处置被拒绝，不会派两次工作
        with self.assertRaises(ConflictError):
            reviews.apply_adjudicated_outcome(self.conn, "Q1", task["task_id"],
                                              clock=self.clock)

    def test_concurrent_label_submissions_do_not_duplicate_work(self):
        from meetingq.db import connect as open_conn
        rnd = self._round()
        task = next(t for t in reviews.list_tasks(self.conn, rnd["round_id"])
                    if t["project"] == "P1")
        errors: list[Exception] = []

        def vote(label: str) -> None:
            conn = open_conn(self.workdir)
            try:
                reviews.submit_label(conn, "Q1", task["task_id"], label,
                                     clock=self.clock)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                conn.close()

        t1 = threading.Thread(target=vote, args=("accepted",))
        t2 = threading.Thread(target=vote, args=("extra",))
        t1.start(); t2.start(); t1.join(); t2.join()
        labels = self.conn.execute(
            "SELECT COUNT(*) AS c FROM review_labels WHERE task_id=? AND reviewer='Q1'",
            (task["task_id"],),
        ).fetchone()["c"]
        self.assertEqual(labels, 1)  # 同一复核员只能投一次
        self.assertEqual(len(errors), 1)

    def test_imported_source_reviews_produce_conflict_and_unknown(self):
        rnd = reviews.create_round(self.conn, "Q1", seed=202,
                                   targeted_proposals=["A1", "A2"], clock=self.clock)
        reviews.import_source_reviews_to_round(self.conn, "Q1", rnd["round_id"],
                                               clock=self.clock)
        tasks = {t["proposal"]: t for t in
                 reviews.list_tasks(self.conn, rnd["round_id"])}
        self.assertEqual(tasks["A1"]["final_label"], "conflict")
        self.assertEqual(tasks["A2"]["final_label"], "unknown")


class EstimationTests(MeetingQTestCase):
    def _adjudicated_round(self, label_by_proposal: dict[str, str]):
        rnd = reviews.create_round(self.conn, "Q1", seed=303, clock=self.clock)
        for t in reviews.list_tasks(self.conn, rnd["round_id"]):
            label = label_by_proposal.get(t["proposal"], "accepted")
            if t["project"] == "P1":
                reviewers = ("Q1", "Q2")
            else:
                reviewers = ("Q3",)
            for rv in reviewers:
                reviews.submit_label(self.conn, rv, t["task_id"], label, clock=self.clock)
            if t["project"] == "P2":
                reviews.adjudicate_label(self.conn, "U2", t["task_id"], label,
                                         "落锤", clock=self.clock)
        reviews.auto_adjudicate_consensus(self.conn, rnd["round_id"])
        return rnd

    def test_estimate_uses_only_probability_and_certain_labels(self):
        rnd = self._adjudicated_round({"A1": "extra", "A3": "owner"})
        report = reports.quality_report(self.conn, rnd["round_id"])
        est = report["estimate"]
        self.assertEqual(est["basis"], "probability_only")
        self.assertTrue(est["targeted_excluded_from_estimation"])
        # 存在错误标签时，总错误率估计为正且 CI 顺序正确
        any_err = est["estimates"]["any_error"]
        self.assertGreaterEqual(any_err["ci_low"], 0.0)
        self.assertLessEqual(any_err["ci_high"], 1.0)
        self.assertLessEqual(any_err["ci_low"], any_err["rate"] + 1e-9)
        self.assertGreaterEqual(any_err["ci_high"], any_err["rate"] - 1e-9)

    def test_reduction_not_recommended_on_small_sample(self):
        rnd = self._adjudicated_round({})
        report = reports.quality_report(self.conn, rnd["round_id"])
        self.assertEqual(report["reduction_advice"]["advice"], "hold")
        self.assertTrue(any("样本量不足" in r for r in
                            report["reduction_advice"]["reasons"]))

    def test_traceability_both_directions(self):
        r = items.create_item_from_proposal(self.conn, "U1", "A1", clock=self.clock)
        items.confirm_item(self.conn, "U1", r["item_id"], clock=self.clock)
        rnd = reviews.create_round(self.conn, "Q1", seed=404,
                                   targeted_proposals=["A1"], clock=self.clock)
        task = [t for t in reviews.list_tasks(self.conn, rnd["round_id"])
                if t["proposal"] == "A1"][0]
        reviews.submit_label(self.conn, "Q1", task["task_id"], "accepted", clock=self.clock)
        reviews.submit_label(self.conn, "Q2", task["task_id"], "accepted", clock=self.clock)
        reviews.auto_adjudicate_consensus(self.conn, rnd["round_id"])

        item_view = reports.item_traceability(self.conn, r["item_id"])
        self.assertTrue(any(tr["task_id"] == task["task_id"]
                            for tr in item_view["review_trace"]))
        review_view = reports.review_traceability(self.conn, task["task_id"])
        self.assertIn(r["item_id"], {x["item_id"] for x in review_view["linked_items"]})
        self.assertIn("不能外推", review_view["inclusion_probability_note"])

    def test_version_diff_states_facts_without_merge(self):
        diff = sampling.diff_summary_versions(self.conn, "M0")
        self.assertEqual([v["version"] for v in diff["versions"]], [1, 2])
        v2 = diff["versions"][1]
        self.assertEqual(v2["proposals"][0]["proposal"], "A0b")
        self.assertIsNone(v2["proposals"][0]["item_id"])  # 未自动建事项/合并


if __name__ == "__main__":
    unittest.main()
