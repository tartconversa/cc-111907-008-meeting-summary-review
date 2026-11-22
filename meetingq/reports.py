"""质量报告与业务事项的互相回查。

所有报告显式区分：
- 概率样本估计（带入选概率、置信区间）与定向争议样本（不可外推）；
- 确定性标签与不确定标签（unknown/冲突不计正确）；
- 业务事项与来源提案、复核任务、冲突、交接通知之间的双向链路。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import sampling
from .errors import NotFoundError


def quality_report(conn: sqlite3.Connection, round_id: str | None = None) -> dict:
    """生成质量报告。round_id 给定时按轮估计，否则给累计估计。"""
    if round_id:
        rnd = conn.execute(
            "SELECT * FROM review_rounds WHERE round_id=?", (round_id,)
        ).fetchone()
        if rnd is None:
            raise NotFoundError(f"轮次不存在: {round_id}")
        estimate = sampling.estimate_round(conn, round_id)
        scope = json.loads(rnd["scope_json"])
        tasks_ref = round_id
    else:
        estimate = sampling.estimate_cumulative(conn)
        scope = {"basis": "cumulative_latest_label_per_proposal"}
        tasks_ref = None
    advice = sampling.reduction_advice(conn, estimate)
    tasks_summary = {
        "probability": {"total": 0, "adjudicated": 0},
        "targeted": {"total": 0, "adjudicated": 0},
    }
    sql = "SELECT sample_kind, status FROM review_tasks"
    params: list = []
    if round_id:
        sql += " WHERE round_id=?"
        params.append(round_id)
    for r in conn.execute(sql, params).fetchall():
        tasks_summary[r["sample_kind"]]["total"] += 1
        if r["status"] == "adjudicated":
            tasks_summary[r["sample_kind"]]["adjudicated"] += 1
    return {
        "round_id": tasks_ref,
        "scope": scope,
        "tasks_summary": tasks_summary,
        "estimate": estimate,
        "reduction_advice": advice,
        "interpretation": (
            "错误率仅基于概率样本的已裁定标签；定向争议样本单独计数，不得外推；"
            "unknown 与双人冲突计入不确定性而非正确率。"
        ),
    }


def item_traceability(conn: sqlite3.Connection, item_id: str) -> dict:
    """业务事项 → 来源会议/摘要/提案、复核标签、冲突、交接的完整回查。"""
    item = conn.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
    if item is None:
        raise NotFoundError(f"事项不存在: {item_id}")
    item = dict(item)
    item["source_chain"] = [dict(r) for r in conn.execute(
        "SELECT l.linkage,l.decided_by,l.decided_at,l.note AS link_note,"
        "       p.proposal,p.subject,p.kind,p.impact,p.mentioned_owner,"
        "       s.summary,s.version,s.received_at AS summary_received,"
        "       m.meeting,m.type AS meeting_type,m.start_at AS meeting_start"
        " FROM item_links l"
        " JOIN proposals p ON p.proposal=l.proposal"
        " JOIN summaries s ON s.summary=p.summary"
        " JOIN meetings m ON m.meeting=s.meeting"
        " WHERE l.item_id=? ORDER BY m.start_at, s.version",
        (item_id,),
    ).fetchall()]
    item["review_trace"] = [dict(r) for r in conn.execute(
        "SELECT rt.round_id,rt.task_id,rt.sample_kind,rt.inclusion_prob,rt.status,"
        "       rt.final_label,rl.reviewer,rl.label,rl.received_at,rl.is_adjudication"
        " FROM item_links l"
        " JOIN review_tasks rt ON rt.proposal=l.proposal"
        " LEFT JOIN review_labels rl ON rl.task_id=rt.task_id"
        " WHERE l.item_id=? ORDER BY rt.round_id, rl.received_at",
        (item_id,),
    ).fetchall()]
    item["conflicts"] = [dict(r) for r in conn.execute(
        "SELECT * FROM item_conflicts WHERE item_id=? ORDER BY detected_at", (item_id,)
    ).fetchall()]
    item["handoffs"] = [dict(r) for r in conn.execute(
        "SELECT * FROM handoff_notifications WHERE item_id=? ORDER BY created_at", (item_id,)
    ).fetchall()]
    item["lead_decisions"] = [dict(r) for r in conn.execute(
        "SELECT * FROM lead_decisions WHERE subject_id=? OR"
        " (subject_type='confirmation' AND subject_id=?) ORDER BY decided_at",
        (item_id, item_id),
    ).fetchall()]
    return item


def review_traceability(conn: sqlite3.Connection, task_id: str) -> dict:
    """复核任务 → 提案/会议、标签、由此产生的事项或冲突的反向回查。"""
    task = conn.execute(
        "SELECT rt.*, p.subject,p.kind,p.impact,p.mentioned_owner,"
        "       s.summary,s.version,s.meeting,m.project,m.type AS meeting_type"
        " FROM review_tasks rt"
        " JOIN proposals p ON p.proposal=rt.proposal"
        " JOIN summaries s ON s.summary=p.summary"
        " JOIN meetings m ON m.meeting=s.meeting"
        " WHERE rt.task_id=?",
        (task_id,),
    ).fetchone()
    if task is None:
        raise NotFoundError(f"复核任务不存在: {task_id}")
    out = dict(task)
    out["labels"] = [dict(r) for r in conn.execute(
        "SELECT * FROM review_labels WHERE task_id=? ORDER BY received_at", (task_id,)
    ).fetchall()]
    out["linked_items"] = [dict(r) for r in conn.execute(
        "SELECT i.item_id,i.subject,i.status,l.linkage FROM item_links l"
        " JOIN items i ON i.item_id=l.item_id WHERE l.proposal=?",
        (task["proposal"],),
    ).fetchall()]
    out["lead_decisions"] = [dict(r) for r in conn.execute(
        "SELECT * FROM lead_decisions WHERE subject_type='review_outcome' AND subject_id=?",
        (task_id,),
    ).fetchall()]
    out["inclusion_probability_note"] = (
        "概率样本：可用于分层估计" if task["sample_kind"] == "probability"
        else "定向争议样本：无入选概率，不能外推为总体准确率"
    )
    return out


def workload_dashboard(conn: sqlite3.Connection) -> dict:
    """负责人工作台：待确认、被阻断、执行中影响、交接未确认、复核待办一次看全。"""
    def count(sql: str) -> int:
        return conn.execute(sql).fetchone()[0]
    return {
        "pending_confirmation": count(
            "SELECT COUNT(*) FROM items WHERE status='pending_confirmation'"),
        "claim_blocked": count("SELECT COUNT(*) FROM items WHERE status='claim_blocked'"),
        "in_progress_impact": count(
            "SELECT COUNT(*) FROM items WHERE status='in_progress_impact'"),
        "open_conflicts": count(
            "SELECT COUNT(*) FROM item_conflicts WHERE status='open'"),
        "pending_handoffs": count(
            "SELECT COUNT(*) FROM handoff_notifications WHERE status='pending'"),
        "review_conflicts": count(
            "SELECT COUNT(*) FROM review_tasks WHERE final_label='conflict' AND status='assigned'"),
        "lead_queue": len(conn.execute(
            "SELECT 1 FROM lead_decisions WHERE action LIKE 'pending%'"
        ).fetchall()),
    }


def write_report(conn: sqlite3.Connection, out_dir: str | Path,
                 round_id: str | None = None) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    report = quality_report(conn, round_id)
    name = f"quality_report_{round_id or 'cumulative'}.json"
    path = out_dir / name
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
