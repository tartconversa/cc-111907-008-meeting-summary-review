"""质量复核：任务抽取、双人标注、冲突裁定、结论处置。

权限边界：
- reviewer 只能产生质量标签；任何业务承诺/撤销都必须由 owner 在 lead_decisions 中裁定。
- 双人标签冲突在第三人裁定前 final_label='conflict'，不计确定标签，不进入错误率。
- 同一复核任务的处置只生效一次（UNIQUE 约束 + 状态条件更新），
  两个并发复核几乎同时完成时不会各自派出重复工作，后到标签也不能覆盖人工决定。
"""

from __future__ import annotations

import json
import sqlite3
import uuid

from . import items as items_mod
from .db import record_audit, record_event
from .errors import AuthzError, ConflictError, NotFoundError, StateError
from .sampling import plan_sample
from .timeutil import Clock

LABELS = ("accepted", "extra", "missing", "duplicate", "owner", "unknown")
# 双人一致即成为确定标签的情形；其余分歧进入冲突裁定
CONFLICT = "conflict"


def resolve_final_label(labels: list[str]) -> str | None:
    """根据普通标注（不含裁定标）推导任务当前标签。

    - 无标注：None；
    - 全部一致：该标签（含 unknown，unknown 仍属不确定而非正确）；
    - 非 unknown 标签实质一致（另一人 unknown）：实质标签；
    - 其余分歧：conflict，等待第三人裁定。
    """
    if not labels:
        return None
    uniq = set(labels)
    if len(uniq) == 1:
        return labels[0]
    non_unknown = [l for l in labels if l != "unknown"]
    if non_unknown and len(set(non_unknown)) == 1:
        return non_unknown[0]
    return CONFLICT


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def latest_policy(conn: sqlite3.Connection, policy_id: str | None = None) -> sqlite3.Row:
    if policy_id:
        row = conn.execute(
            "SELECT * FROM review_policy_versions WHERE policy_id=?", (policy_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"复核策略不存在: {policy_id}")
        return row
    row = conn.execute(
        "SELECT * FROM review_policy_versions ORDER BY received_at, policy_id LIMIT 1"
    ).fetchone()
    if row is None:
        raise NotFoundError("尚未导入 review_policy，无法规划复核")
    return row


def create_round(
    conn: sqlite3.Connection, actor: str, seed: int,
    targeted_proposals: list[str] | None = None,
    policy_id: str | None = None, clock: Clock | None = None,
) -> dict:
    """按策略容量创建复核轮次并落任务；概率任务保存入选概率。"""
    clock = clock or Clock()
    policy = latest_policy(conn, policy_id)
    plan = plan_sample(conn, policy, seed, targeted_proposals, round_id=None)
    round_id = _new_id("RND")
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO review_rounds(round_id,policy_id,created_at,planned_n,ordinary_n,"
            "targeted_n,seed,status,scope_json) VALUES(?,?,?,?,?,?,?,'planned',?)",
            (round_id, policy["policy_id"], clock.now(), len(plan["tasks"]),
             plan["scope"]["ordinary_slots"],
             len([t for t in plan["tasks"] if t["sample_kind"] == "targeted"]),
             seed, json.dumps(plan["scope"], ensure_ascii=False)),
        )
        for t in plan["tasks"]:
            conn.execute(
                "INSERT INTO review_tasks(task_id,round_id,proposal,stratum,sample_kind,"
                "inclusion_prob,status,created_at) VALUES(?,?,?,?,?,?, 'assigned',?)",
                (_new_id("TSK"), round_id, t["proposal"], t["stratum"], t["sample_kind"],
                 t["inclusion_prob"], clock.now()),
            )
        record_audit(conn, actor, "review.round_create", clock.now(),
                     {"round_id": round_id, "scope": plan["scope"]})
        conn.commit()
        plan["round_id"] = round_id
        return plan
    except Exception:
        conn.rollback()
        raise


def list_tasks(conn: sqlite3.Connection, round_id: str,
               status: str | None = None) -> list[dict]:
    sql = (
        "SELECT t.*, p.subject, p.kind, p.impact, m.project"
        " FROM review_tasks t JOIN proposals p ON p.proposal=t.proposal"
        " JOIN summaries s ON s.summary=p.summary JOIN meetings m ON m.meeting=s.meeting"
        " WHERE t.round_id=?"
    )
    params: list = [round_id]
    if status:
        sql += " AND t.status=?"
        params.append(status)
    sql += " ORDER BY t.sample_kind DESC, t.task_id"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def submit_label(
    conn: sqlite3.Connection, reviewer: str, task_id: str, label: str,
    clock: Clock | None = None,
) -> dict:
    """复核员提交标签。两名复核员分歧时自动置 conflict，等待裁定。"""
    clock = clock or Clock()
    if label not in LABELS:
        raise StateError(f"标签必须是 {LABELS}")
    conn.execute("BEGIN IMMEDIATE")
    try:
        task = conn.execute(
            "SELECT t.*, m.project FROM review_tasks t"
            " JOIN proposals p ON p.proposal=t.proposal"
            " JOIN summaries s ON s.summary=p.summary"
            " JOIN meetings m ON m.meeting=s.meeting WHERE t.task_id=?",
            (task_id,),
        ).fetchone()
        if task is None:
            raise NotFoundError(f"复核任务不存在: {task_id}")
        if task["status"] == "cancelled":
            raise StateError("任务已取消")
        items_mod.require_role(conn, reviewer, task["project"], "reviewer")
        now = clock.now()
        try:
            conn.execute(
                "INSERT INTO review_labels(task_id,reviewer,label,received_at)"
                " VALUES(?,?,?,?)",
                (task_id, reviewer, label, now),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("该复核员已提交过本任务标签，不能重复提交") from exc

        labels = conn.execute(
            "SELECT label FROM review_labels WHERE task_id=? AND is_adjudication=0",
            (task_id,),
        ).fetchall()
        if len(labels) >= 2:
            final = resolve_final_label([r["label"] for r in labels])
            conn.execute(
                "UPDATE review_tasks SET final_label=? WHERE task_id=?", (final, task_id)
            )
        record_audit(conn, reviewer, "review.label", now,
                     {"task_id": task_id, "label": label})
        conn.commit()
        return {"task_id": task_id, "labels_count": len(labels),
                "final_label": task["final_label"] if len(labels) < 2 else final}
    except Exception:
        conn.rollback()
        raise


def adjudicate_label(
    conn: sqlite3.Connection, adjudicator: str, task_id: str, final_label: str,
    note: str, clock: Clock | None = None,
) -> dict:
    """双人冲突后的第三人裁定；裁定后任务才成为确定标签。"""
    clock = clock or Clock()
    if final_label not in LABELS:
        raise StateError(f"裁定标签必须是 {LABELS}")
    conn.execute("BEGIN IMMEDIATE")
    try:
        task = conn.execute(
            "SELECT t.*, m.project FROM review_tasks t"
            " JOIN proposals p ON p.proposal=t.proposal"
            " JOIN summaries s ON s.summary=p.summary"
            " JOIN meetings m ON m.meeting=s.meeting WHERE t.task_id=?",
            (task_id,),
        ).fetchone()
        if task is None:
            raise NotFoundError(f"复核任务不存在: {task_id}")
        # 裁定人可以是同项目复核员或负责人；已对本任务提交过标签的复核员须回避，
        # 由唯一约束兜底（第三人裁定前双人冲突不计确定标签）。
        role = items_mod.role_of(conn, adjudicator, task["project"])
        if role not in ("reviewer", "owner"):
            raise AuthzError(
                f"用户 {adjudicator} 在项目 {task['project']} 无权裁定复核分歧"
            )
        already_voted = conn.execute(
            "SELECT 1 FROM review_labels WHERE task_id=? AND reviewer=?"
            " AND is_adjudication=0", (task_id, adjudicator),
        ).fetchone()
        if already_voted:
            raise ConflictError("裁定人已对本任务提交过标签，须回避并由第三人裁定")
        existing = conn.execute(
            "SELECT 1 FROM review_labels WHERE task_id=? AND is_adjudication=1", (task_id,)
        ).fetchone()
        if existing:
            raise ConflictError("任务已经裁定，不能重复裁定；如需纠正请发起新一轮复核")
        now = clock.now()
        conn.execute(
            "INSERT INTO review_labels(task_id,reviewer,label,received_at,is_adjudication)"
            " VALUES(?,?,?,?,1)",
            (task_id, adjudicator, final_label, now),
        )
        # 条件更新：只有仍处于未裁定状态才落锤，防止覆盖并行处置
        cur = conn.execute(
            "UPDATE review_tasks SET status='adjudicated', final_label=?"
            " WHERE task_id=? AND status<>'adjudicated'",
            (final_label, task_id),
        )
        if cur.rowcount == 0:
            raise ConflictError("任务已被并行处置，裁定未生效")
        record_audit(conn, adjudicator, "review.adjudicate", now,
                     {"task_id": task_id, "final_label": final_label, "note": note})
        conn.commit()
        return {"task_id": task_id, "final_label": final_label, "status": "adjudicated"}
    except Exception:
        conn.rollback()
        raise


def auto_adjudicate_consensus(conn: sqlite3.Connection, round_id: str) -> int:
    """把双人一致（非 conflict、非 unknown 分歧）的任务置为已裁定，返回处理数。"""
    rows = conn.execute(
        "SELECT task_id, final_label, status FROM review_tasks"
        " WHERE round_id=? AND status='assigned' AND final_label IS NOT NULL"
        " AND final_label<>'conflict'",
        (round_id,),
    ).fetchall()
    n = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for r in rows:
            cur = conn.execute(
                "UPDATE review_tasks SET status='adjudicated' WHERE task_id=?"
                " AND status='assigned'",
                (r["task_id"],),
            )
            n += cur.rowcount
        conn.commit()
        return n
    except Exception:
        conn.rollback()
        raise


def close_round(conn: sqlite3.Connection, actor: str, round_id: str,
                clock: Clock | None = None) -> dict:
    clock = clock or Clock()
    open_tasks = conn.execute(
        "SELECT COUNT(*) AS c FROM review_tasks WHERE round_id=? AND status='assigned'",
        (round_id,),
    ).fetchone()["c"]
    if open_tasks:
        raise StateError(f"仍有 {open_tasks} 个任务未裁定，不能关闭轮次")
    conn.execute(
        "UPDATE review_rounds SET status='closed', closed_at=? WHERE round_id=?",
        (clock.now(), round_id),
    )
    record_audit(conn, actor, "review.round_close", clock.now(), {"round_id": round_id})
    return {"round_id": round_id, "status": "closed"}


# ----------------------------------------------- 复核结论对事项的影响 ----
def apply_adjudicated_outcome(
    conn: sqlite3.Connection, actor: str, task_id: str,
    clock: Clock | None = None, note: str | None = None,
) -> dict:
    """把已裁定标签落到业务侧。质量员不能替负责人做承诺：

    - missing  → 建立 draft_proposal 待确认事项（review_missing 来源），等负责人确认；
    - extra    → 不直接撤销；生成负责人决定队列条目（lead_decisions），由 owner 裁 revoke/keep；
    - duplicate→ 生成 duplicate 型 item_conflict，由 owner 决定保留哪个真实事项及工作记录；
    - owner    → 归属挂起，owner 裁定正确负责人后才能继续；
    - accepted → 仅记录，无业务动作；
    - unknown  → 拒绝处置（无法判断不能当正确）。
    同一任务处置仅生效一次。
    """
    clock = clock or Clock()
    conn.execute("BEGIN IMMEDIATE")
    try:
        task = conn.execute(
            "SELECT t.*, p.subject, p.impact, p.kind, m.project, m.meeting"
            " FROM review_tasks t JOIN proposals p ON p.proposal=t.proposal"
            " JOIN summaries s ON s.summary=p.summary JOIN meetings m ON m.meeting=s.meeting"
            " WHERE t.task_id=?",
            (task_id,),
        ).fetchone()
        if task is None:
            raise NotFoundError(f"复核任务不存在: {task_id}")
        if task["status"] != "adjudicated":
            raise StateError("只有已裁定任务可以处置，冲突请先 adjudicate")
        label = task["final_label"]
        if label == "unknown":
            raise StateError("unknown（无法判断）不能作为处置依据")
        already = conn.execute(
            "SELECT 1 FROM lead_decisions WHERE subject_type='review_outcome'"
            " AND subject_id=? AND action<>'reject'", (task_id,),
        ).fetchone()
        if already:
            raise ConflictError("该复核任务的结论已处置，不能重复派出工作")

        now = clock.now()
        result: dict = {"task_id": task_id, "label": label}

        if label == "accepted":
            conn.execute(
                "INSERT INTO lead_decisions(project,owner,decided_at,subject_type,subject_id,action,note)"
                " VALUES(?,?,?,?, 'review_outcome',?, 'accepted',?)",
                (task["project"], actor, now, task_id, note),
            )
            result["effect"] = "recorded_only"

        elif label == "missing":
            # 漏决定：建立待确认提案，而非直接承诺
            item_id = _new_id("ITM")
            conn.execute(
                "INSERT INTO items(item_id,project,subject,impact,status,origin,created_by,created_at)"
                " VALUES(?,?,?,?,'draft_proposal','review_missing',?,?)",
                (item_id, task["project"], task["subject"], task["impact"], actor, now),
            )
            conn.execute(
                "INSERT INTO item_links(item_id,proposal,linkage,decided_by,decided_at,note)"
                " VALUES(?,?, 'reference',?,?,?)",
                (item_id, task["proposal"], actor, now, "漏项复核建立，待负责人确认"),
            )
            record_event(conn, item_id, "created_from_missing_review", actor, now,
                         None, "draft_proposal", {"task_id": task_id})
            conn.execute(
                "INSERT INTO lead_decisions(project,owner,decided_at,subject_type,subject_id,action,note)"
                " VALUES(?,?,?, 'review_outcome',?, 'create_proposal',?)",
                (task["project"], actor, now, task_id,
                 f"漏项→待确认事项 {item_id}; {note or ''}"),
            )
            result["effect"] = "draft_proposal_created"
            result["item_id"] = item_id

        elif label == "extra":
            # 多报承诺：要求 owner 撤销或保留并补说明；质量员不代行
            link = conn.execute(
                "SELECT item_id FROM item_links WHERE proposal=?", (task["proposal"],)
            ).fetchone()
            if link is None:
                # 尚未建立事项：登记一条待负责人决定的记录，不自动建/删
                conn.execute(
                    "INSERT INTO lead_decisions(project,owner,decided_at,subject_type,subject_id,action,note)"
                    " VALUES(?,?,?, 'review_outcome',?, 'pending_extra',?)",
                    (task["project"], actor, now, task_id,
                     f"多报承诺且无对应事项，待负责人判断; {note or ''}"),
                )
                result["effect"] = "queued_for_owner"
            else:
                cid = _new_id("CFL")
                conn.execute(
                    "INSERT INTO item_conflicts(conflict_id,item_id,new_proposal,conflict_type,"
                    "status,detected_at,decision_note) VALUES(?,?,?,?,'open',?,?)",
                    (cid, link["item_id"], task["proposal"], "contradict", now,
                     f"复核标签 extra：多报承诺，待负责人撤销或保留补说明; {note or ''}"),
                )
                item = items_mod.get_item(conn, link["item_id"])
                if item["status"] in ("pending_confirmation", "confirmed"):
                    conn.execute(
                        "UPDATE items SET status='claim_blocked', open_conflict_id=?,"
                        " row_version=row_version+1 WHERE item_id=?",
                        (cid, link["item_id"]),
                    )
                    record_event(conn, link["item_id"], "conflict_opened", actor, now,
                                 item["status"], "claim_blocked",
                                 {"conflict_id": cid, "from_review": "extra"})
                elif item["status"] == "in_progress":
                    # 已开始工作：不撤销、不阻断，保留进度并通知执行人确认影响
                    conn.execute(
                        "UPDATE items SET status='in_progress_impact', open_conflict_id=?,"
                        " row_version=row_version+1 WHERE item_id=?",
                        (cid, link["item_id"]),
                    )
                    record_event(conn, link["item_id"], "conflict_opened", actor, now,
                                 item["status"], "in_progress_impact",
                                 {"conflict_id": cid, "from_review": "extra"})
                    conn.execute(
                        "INSERT OR IGNORE INTO handoff_notifications(notification_id,item_id,"
                        "conflict_id,executor,created_at,status)"
                        " VALUES(?,?,?,?,?,'pending')",
                        (_new_id("NTF"), link["item_id"], cid,
                         item["claimed_by"], now),
                    )
                conn.execute(
                    "INSERT INTO lead_decisions(project,owner,decided_at,subject_type,subject_id,action,note)"
                    " VALUES(?,?,?, 'review_outcome',?, 'pending_revoke_or_keep',?)",
                    (task["project"], actor, now, task_id, f"冲突 {cid}"),
                )
                result["effect"] = "conflict_queued"
                result["conflict_id"] = cid

        elif label == "duplicate":
            link = conn.execute(
                "SELECT item_id FROM item_links WHERE proposal=?", (task["proposal"],)
            ).fetchone()
            item_id = link["item_id"] if link else None
            if item_id is None:
                raise StateError("duplicate 标签对应的提案尚未关联事项，请先建立事项再裁定重复")
            cid = _new_id("CFL")
            conn.execute(
                "INSERT INTO item_conflicts(conflict_id,item_id,new_proposal,conflict_type,"
                "status,detected_at,decision_note) VALUES(?,?,?, 'duplicate','open',?,?)",
                (cid, item_id, task["proposal"], now,
                 f"复核标签 duplicate：待负责人裁定保留哪个真实事项及工作记录; {note or ''}"),
            )
            conn.execute(
                "INSERT INTO lead_decisions(project,owner,decided_at,subject_type,subject_id,action,note)"
                " VALUES(?,?,?, 'review_outcome',?, 'pending_duplicate_adjudication',?)",
                (task["project"], actor, now, task_id, f"冲突 {cid}"),
            )
            result["effect"] = "duplicate_queued"
            result["conflict_id"] = cid

        elif label == "owner":
            conn.execute(
                "INSERT INTO lead_decisions(project,owner,decided_at,subject_type,subject_id,action,note)"
                " VALUES(?,?,?, 'review_outcome',?, 'pending_owner_correction',?)",
                (task["project"], actor, now, task_id,
                 f"归属错误：待负责人确认正确负责人; {note or ''}"),
            )
            result["effect"] = "owner_correction_queued"

        record_audit(conn, actor, "review.outcome_apply", now, result)
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise


def import_source_reviews_to_round(
    conn: sqlite3.Connection, actor: str, round_id: str,
    clock: Clock | None = None,
) -> dict:
    """把随资料导入的人工标注（reviews.json）登记到指定轮次。

    同一提案的导入标注若分歧，同样落 conflict，等待第三人裁定；
    unknown 保留为不确定标签，绝不当正确。
    """
    clock = clock or Clock()
    rnd = conn.execute("SELECT * FROM review_rounds WHERE round_id=?", (round_id,)).fetchone()
    if rnd is None:
        raise NotFoundError(f"轮次不存在: {round_id}")
    imported = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for sr in conn.execute(
            "SELECT * FROM source_reviews ORDER BY received_at, review_id"
        ).fetchall():
            task = conn.execute(
                "SELECT * FROM review_tasks WHERE round_id=? AND proposal=?",
                (round_id, sr["proposal"]),
            ).fetchone()
            if task is None:
                # 导入标注对应的提案不在本轮样本：建定向任务承载，保证可追溯
                stratum = conn.execute(
                    "SELECT m.project, p.impact, p.kind, m.type AS meeting_type"
                    " FROM proposals p JOIN summaries s ON s.summary=p.summary"
                    " JOIN meetings m ON m.meeting=s.meeting WHERE p.proposal=?",
                    (sr["proposal"],),
                ).fetchone()
                task_id = _new_id("TSK")
                conn.execute(
                    "INSERT INTO review_tasks(task_id,round_id,proposal,stratum,sample_kind,"
                    "inclusion_prob,status,created_at) VALUES(?,?,?,?,'targeted',NULL,'assigned',?)",
                    (task_id, round_id, sr["proposal"],
                     f"{stratum['project']}|{stratum['impact']}|{stratum['kind']}|{stratum['meeting_type']}",
                     clock.now()),
                )
                task = conn.execute(
                    "SELECT * FROM review_tasks WHERE task_id=?", (task_id,)
                ).fetchone()
            try:
                conn.execute(
                    "INSERT INTO review_labels(task_id,reviewer,label,received_at)"
                    " VALUES(?,?,?,?)",
                    (task["task_id"], sr["reviewer"], sr["label"], sr["received_at"]),
                )
                imported += 1
            except sqlite3.IntegrityError:
                continue
        # 重算受影响任务的 final_label
        for task in conn.execute(
            "SELECT task_id FROM review_tasks WHERE round_id=? AND status='assigned'",
            (round_id,),
        ).fetchall():
            labels = conn.execute(
                "SELECT label FROM review_labels WHERE task_id=? AND is_adjudication=0",
                (task["task_id"],),
            ).fetchall()
            if not labels:
                continue
            final = resolve_final_label([r["label"] for r in labels])
            conn.execute(
                "UPDATE review_tasks SET final_label=? WHERE task_id=?",
                (final, task["task_id"]),
            )
        record_audit(conn, actor, "review.import_source_labels", clock.now(),
                     {"round_id": round_id, "imported": imported})
        conn.commit()
        return {"round_id": round_id, "imported_labels": imported}
    except Exception:
        conn.rollback()
        raise


def pending_lead_decisions(conn: sqlite3.Connection, project: str | None = None) -> list[dict]:
    """负责人待办裁定队列（复核产生，质量标签本身不改变业务承诺）。"""
    sql = (
        "SELECT d.* FROM lead_decisions d WHERE d.subject_type='review_outcome'"
        " AND d.action LIKE 'pending%'"
    )
    params: list = []
    if project:
        sql += " AND d.project=?"
        params.append(project)
    sql += " ORDER BY d.decided_at"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
