"""导入与资料契约测试。"""

import hashlib
import json
import shutil
import unittest
from pathlib import Path

from meetingq import importer
from meetingq.errors import InputContractError
from meetingq.timeutil import Clock

from support import INPUTS, MeetingQTestCase


class ImportTests(MeetingQTestCase):
    def test_idempotent_reimport_creates_no_new_facts(self):
        before = {
            t: self.conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
            for t in ("meetings", "summaries", "proposals", "members", "source_reviews")
        }
        stats2 = importer.import_sources(self.conn, INPUTS, clock=self.clock)
        after = {
            t: self.conn.execute(f"SELECT COUNT(*) AS c FROM {t}").fetchone()["c"]
            for t in ("meetings", "summaries", "proposals", "members", "source_reviews")
        }
        self.assertEqual(before, after)
        self.assertEqual(sum(stats2["inserted"].values()), 0)
        self.assertGreater(sum(stats2["reinserted"].values()), 0)

    def test_rerun_summary_version_is_kept_as_separate_version(self):
        rows = self.conn.execute(
            "SELECT version FROM summaries WHERE meeting='M0' ORDER BY version"
        ).fetchall()
        self.assertEqual([r["version"] for r in rows], [1, 2])
        # 重跑不产生任何事项（事项只能由人工建立）
        self.assertEqual(
            self.conn.execute("SELECT COUNT(*) AS c FROM items").fetchone()["c"], 0
        )

    def test_manifest_tampering_is_rejected(self):
        bad_dir = Path(self._tmp.name) / "bad_inputs"
        shutil.copytree(INPUTS, bad_dir)
        manifest = json.loads((bad_dir / "manifest.json").read_text(encoding="utf-8"))
        manifest["files"]["meetings.json"] = "0" * 64
        (bad_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        with self.assertRaises(InputContractError):
            importer.import_sources(self.conn, bad_dir, clock=self.clock)

    def test_content_conflict_is_recorded_not_silently_overwritten(self):
        bad_dir = Path(self._tmp.name) / "conflict_inputs"
        shutil.copytree(INPUTS, bad_dir)
        # 改数据后重新计算除 meetings 外所有文件哈希，并只让 meetings 内容变化
        meetings = json.loads((bad_dir / "meetings.json").read_text(encoding="utf-8"))
        meetings[0]["team"] = "tampered"
        (bad_dir / "meetings.json").write_text(
            json.dumps(meetings, ensure_ascii=False), encoding="utf-8"
        )
        manifest = json.loads((bad_dir / "manifest.json").read_text(encoding="utf-8"))
        for name in list(manifest["files"]):
            manifest["files"][name] = hashlib.sha256(
                (bad_dir / name).read_bytes()
            ).hexdigest()
        (bad_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        # setUp 已导入原始 M0(team=beta)；导入同主键但内容不同的数据：
        # 登记 content_conflict，原值保留不被静默覆盖
        stats2 = importer.import_sources(self.conn, bad_dir,
                                         clock=Clock.fixed("2026-11-23T09:00:00Z"))
        self.assertGreaterEqual(stats2["issues"], 1)
        issue = self.conn.execute(
            "SELECT kind FROM import_issues WHERE entity='meetings' AND source_key='M0'"
        ).fetchone()
        self.assertEqual(issue["kind"], "content_conflict")
        team = self.conn.execute(
            "SELECT team FROM meetings WHERE meeting='M0'"
        ).fetchone()["team"]
        self.assertEqual(team, "beta")  # 未被篡改值覆盖

    def test_orphan_reference_rolls_back_entire_batch(self):
        bad_dir = Path(self._tmp.name) / "orphan_inputs"
        shutil.copytree(INPUTS, bad_dir)
        proposals = json.loads((bad_dir / "proposals.json").read_text(encoding="utf-8"))
        proposals[0]["summary"] = "S-NOPE"
        (bad_dir / "proposals.json").write_text(
            json.dumps(proposals, ensure_ascii=False), encoding="utf-8"
        )
        manifest = json.loads((bad_dir / "manifest.json").read_text(encoding="utf-8"))
        for name in list(manifest["files"]):
            manifest["files"][name] = hashlib.sha256(
                (bad_dir / name).read_bytes()
            ).hexdigest()
        (bad_dir / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
        )
        # 全新数据库：坏批次必须整体回滚，不留任何来源行
        from meetingq.db import connect, initialize
        fresh_dir = Path(self._tmp.name) / "fresh"
        fresh_conn = connect(fresh_dir)
        initialize(fresh_conn)
        with self.assertRaises(InputContractError):
            importer.import_sources(fresh_conn, bad_dir, clock=self.clock)
        self.assertEqual(
            fresh_conn.execute("SELECT COUNT(*) AS c FROM meetings").fetchone()["c"], 0)
        fresh_conn.close()


if __name__ == "__main__":
    unittest.main()
