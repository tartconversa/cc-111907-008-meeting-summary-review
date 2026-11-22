"""新摘要与已确认事项的冲突处理。

硬性规则（题述要求，代码不提供绕过路径）：
- 关联有歧义只登记为待人工裁定，绝不按标题相似度自动合并；
- 负责人裁定前不自动删除任务，也不把新增建议当作原承诺的替换；
- 未领取事项冲突 → claim_blocked，待负责人复核，期间禁止领取；
- 已开始工作的事项冲突 → in_progress_impact，维持原承诺与实际进度，
  向执行人发交接通知；执行人确认收到前通知一直留在交接队列；
- 通知不因为报表更新而消失，必须执行人显式确认。
"""

from __future__ import annotations

import sqlite3
import uuid

from .db import record_audit, record_event
from .errors import AuthzError, ConflictError, NotFoundError, StateError
from .items import ALLOWED_TRANSITIONS, get_item, require_role
from .timeutil import Clock


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def detect_conflicts(conn: sqlite3.Connection, clock: Clock | None = None) -> list[dict]:
    """扫描来源提案与事项之间需要人工判断的关系。

    判定全部基于显式来源链路（同会议的新版本、已建立的关联），
    不做任何标题文本相似度匹配。跨会议同主题仅标记 ambiguous 提示，等待人工 link/merge。
    """
    clock = clock or Clock()
    found: list[dict] = []
    now = clock.now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 1) 同会议出现新版本摘要：新版本中的提案与该会议既有提案需要人工判断关系。
        for row in conn.execute(
            "SELECT p.proposal, p.subject, p.kind, p.impact, s.meeting, s.version,"
            "       l.item_id, i.status AS item_status, i.open_conflict_id"
            " FROM proposals p"
            " JOIN summaries s ON s.summary=p.summary"
            " LEFT JOIN item_links l ON l.proposal=p.proposal"
            " LEFT JOIN items i ON i.item_id=l.item_id"
            " WHERE s.version > 1 ORDER BY s.received_at, p.proposal"
        ).fetchall():
            # 找到该会议旧版本上的、已关联事项的提案
            older = conn.execute(
                "SELECT p2.proposal, l2.item_id, i2.status"
                " FROM summaries s2"
                " JOIN proposals p2 ON p2.summary=s2.summary"
                " JOIN item_links l2 ON l2.proposal=p2.proposal"
                " JOIN items i2 ON i2.item_id=l2.item_id"
                " WHERE s2.meeting=? AND s2.version < ?",
                (row["meeting"], row["version"]),
            ).fetchall()
            for old in older:
                if row["item_id"] == old["item_id"] and row["item_id"] is not None:
                    continue  # 人工已关联到同一事项，无需再问
                exists = conn.execute(
                    "SELECT 1 FROM item_conflicts WHERE item_id=? AND new_proposal=?",
                    (old["item_id"], row["proposal"]),
                ).fetchone()
                if exists:
                    continue
                ctype = "contradict" if row["kind"] == "decision" else "ambiguous"
                cid = _open_conflict(conn, old["item_id"], row["proposal"], ctype, now)
                found.append({"conflict_id": cid, "item_id": old["item_id"],
                              "new_proposal": row["proposal"], "conflict_type": ctype})

        # 2) 跨会议同主题的提案：只提示歧义，绝不自动合并。
        candidates = conn.execute(
            "SELECT l1.item_id AS existing_item, p1.proposal AS new_p, p1.subject"
            " FROM item_links l1"
            " JOIN proposals p1 ON p1.proposal=l1.proposal"
            " JOIN summaries s1 ON s1.summary=p1.summary"
            " WHERE EXISTS ("
            "   SELECT 1 FROM item_links l2"
            "   JOIN proposals p2 ON p2.proposal=l2.proposal"
            "   JOIN summaries s2 ON s2.summary=p2.summary"
            "   WHERE p2.subject=p1.subject AND s2.meeting<>s1.meeting"
            "     AND l2.item_id<>l1.item_id)"
        ).fetchall()
        for c in candidates:
            other = conn.execute(
                "SELECT l2.item_id FROM item_links l1"
                " JOIN proposals p1 ON p1.proposal=l1.proposal"
                " JOIN summaries s1 ON s1.summary=p1.summary"
                " JOIN item_links l2 ON 1=1"
                " JOIN proposals p2 ON p2.proposal=l2.proposal"
                " JOIN summaries s2 ON s2.summary=p2.summary"
                " WHERE l1.item_id=? AND l1.proposal=p1.proposal"
                "   AND p2.subject=p1.subject AND s2.meeting<>s1.meeting"
                "   AND l2.item_id<>l1.item_id LIMIT 1",
                (c["existing_item"],),
            ).fetchone()
            if other is None:
                continue
            exists = conn.execute(
                "SELECT 1 FROM item_conflicts WHERE item_id=? AND new_proposal=?"
                " AND conflict_type='duplicate'",
                (c["existing_item"], c["new_p"]),
            ).fetchone()
            if exists:
                continue
            cid = _open_conflict(conn, c["existing_item"], c["new_p"], "duplicate", now)
            found.append({"conflict_id": cid, "item_id": c["existing_item"],
                          "new_proposal": c["new_p"], "conflict_type": "duplicate"})
        conn.commit()
        return found
    except Exception:
        conn.rollback()
        raise


def _open_conflict(conn: sqlite3.Connection, item_id: str, new_proposal: str | None,
                   ctype: str, now: str) -> str:
    cid = _new_id("CFL")
    item = get_item(conn, item_id)
    conn.execute(
        "INSERT INTO item_conflicts(conflict_id,item_id,new_proposal,conflict_type,"
        "status,detected_at) VALUES(?,?,?,?,'open',?)",
        (cid, item_id, new_proposal, ctype, now),
    )
    # 状态分流：未领取 → 阻断领取；执行中 → 保留进度并通知执行人
    if item["status"] in ("pending_confirmation", "confirmed"):
        target = "claim_blocked"
    elif item["status"] in ("in_progress",):
        target = "in_progress_impact"
    else:
        target = None
    if target and target in ALLOWED_TRANSITIONS[item["status"]]:
        conn.execute(
            "UPDATE items SET status=?, open_conflict_id=?, row_version=row_version+1"
            " WHERE item_id=?",
            (target, cid, item_id),
        )
        record_event(conn, item_id, "conflict_opened", "system", now,
                     item["status"], target,
                     {"conflict_id": cid, "type": ctype, "new_proposal": new_proposal})
    else:
        conn.execute("UPDATE items SET open_conflict_id=? WHERE item_id=?", (cid, item_id))
        record_event(conn, item_id, "conflict_opened", "system", now,
                     item["status"], item["status"],
                     {"conflict_id": cid, "type": ctype, "new_proposal": new_proposal})
    if item["status"] == "in_progress" and item["claimed_by"]:
        conn.execute(
            "INSERT OR IGNORE INTO handoff_notifications(notification_id,item_id,"
            "conflict_id,executor,created_at,status) VALUES(?,?,?,?,?,'pending')",
            (_new_id("NTF"), item_id, cid, item["claimed_by"], now),
        )
    return cid


def list_open_conflicts(conn: sqlite3.Connection, project: str | None = None) -> list[dict]:
    sql = (
        "SELECT c.*, i.project, i.subject, i.status AS item_status, i.claimed_by,"
        " p.subject AS new_subject, p.kind AS new_kind"
        " FROM item_conflicts c JOIN items i ON i.item_id=c.item_id"
        " LEFT JOIN proposals p ON p.proposal=c.new_proposal WHERE c.status='open'"
    )
    params: list = []
    if project:
        sql += " AND i.project=?"
        params.append(project)
    sql += " ORDER BY c.detected_at"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def decide_conflict(
    conn: sqlite3.Connection, owner: str, conflict_id: str, action: str,
    note: str, clock: Clock | None = None, kept_item_id: str | None = None,
) -> dict:
    """负责人裁定冲突。

    action:
      uphold   维持原承诺（新建议不作为替换）；事项恢复可领取/执行，通知待执行人确认
      revoke   撤销原事项（withdrawn）；仅限未发生工时的事项，否则拒绝并要求先说明
      merge    重复事项归并，须给 kept_item_id
      dismiss  误报/无实际冲突，解除阻断
    """
    clock = clock or Clock()
    if action not in ("uphold", "revoke", "merge", "dismiss"):
        raise StateError("action 必须是 uphold/revoke/merge/dismiss")
    conn.execute("BEGIN IMMEDIATE")
    try:
        crow = conn.execute(
            "SELECT * FROM item_conflicts WHERE conflict_id=?", (conflict_id,)
        ).fetchone()
        if crow is None:
            raise NotFoundError(f"冲突不存在: {conflict_id}")
        if crow["status"] != "open":
            raise StateError(f"冲突 {conflict_id} 已裁定为 {crow['status']}，不能重复裁定")
        item = get_item(conn, crow["item_id"])
        require_role(conn, owner, item["project"], "owner")

        if action == "merge":
            if not kept_item_id:
                raise StateError("merge 裁定必须提供 kept_item_id")
            target = get_item(conn, kept_item_id)
            if target["project"] != item["project"]:
                raise AuthzError("只能在同项目事项之间归并")

        hours = conn.execute(
            "SELECT COALESCE(SUM(hours_spent),0) AS h FROM progress_entries"
            " WHERE item_id=? AND superseded=0", (item["item_id"],),
        ).fetchone()["h"]
        if action == "revoke" and hours > 0:
            raise StateError(
                f"事项已花费 {hours} 工时，不能直接撤销；请先 uphold 保留进度并补充说明，"
                "或由执行人登记结果后再处理"
            )

        now = clock.now()
        status_map = {"uphold": "upheld", "revoke": "revoked",
                      "merge": "merged", "dismiss": "dismissed"}
        conn.execute(
            "UPDATE item_conflicts SET status=?, decided_by=?, decided_at=?,"
            " decision_note=?, kept_item_id=? WHERE conflict_id=?",
            (status_map[action], owner, now, note, kept_item_id, conflict_id),
        )
        conn.execute(
            "INSERT INTO lead_decisions(project,owner,decided_at,subject_type,subject_id,action,note)"
            " VALUES(?,?,?,?,?,?,?)",
            (item["project"], owner, now, "conflict", conflict_id, action, note),
        )

        # 解除事项阻断/影响状态
        if action in ("uphold", "dismiss"):
            if item["status"] == "claim_blocked":
                _move(conn, item, "confirmed", owner, now,
                      {"conflict_id": conflict_id, "decision": action})
            elif item["status"] == "in_progress_impact":
                _move(conn, item, "in_progress", owner, now,
                      {"conflict_id": conflict_id, "decision": action})
        elif action == "revoke":
            if item["status"] in ("claim_blocked", "pending_confirmation", "confirmed"):
                _move(conn, item, "withdrawn", owner, now,
                      {"conflict_id": conflict_id, "decision": "revoke"})
            else:
                raise StateError("当前状态不允许 revoke，请改用 uphold 保留工作")
        elif action == "merge":
            # 归并只解除阻断；真正的事项合并由 items.merge_items 完成（保留工作记录）
            if item["status"] == "claim_blocked":
                _move(conn, item, "confirmed", owner, now,
                      {"conflict_id": conflict_id, "decision": "merge_pending"})
            elif item["status"] == "in_progress_impact":
                _move(conn, item, "in_progress", owner, now,
                      {"conflict_id": conflict_id, "decision": "merge_pending"})

        conn.execute(
            "UPDATE items SET open_conflict_id=NULL WHERE item_id=? AND open_conflict_id=?",
            (item["item_id"], conflict_id),
        )
        record_audit(conn, owner, "conflict.decide", now,
                     {"conflict_id": conflict_id, "action": action})
        conn.commit()
        return {"conflict_id": conflict_id, "resolution": status_map[action]}
    except Exception:
        conn.rollback()
        raise


def _move(conn, item: sqlite3.Row, to_status: str, actor: str, now: str, detail: dict) -> None:
    if to_status not in ALLOWED_TRANSITIONS[item["status"]]:
        raise StateError(
            f"事项 {item['item_id']} 状态 {item['status']} 无法转到 {to_status}"
        )
    conn.execute(
        "UPDATE items SET status=?, row_version=row_version+1 WHERE item_id=?",
        (to_status, item["item_id"]),
    )
    record_event(conn, item["item_id"], "conflict_resolved", actor, now,
                 item["status"], to_status, detail)
    # 裁定后仍需执行人确认收到：不删除既有通知，再补发一条“裁定结果”通知
    if item["claimed_by"]:
        conn.execute(
            "INSERT OR IGNORE INTO handoff_notifications(notification_id,item_id,"
            "conflict_id,executor,created_at,status) VALUES(?,?,?,?,?,'pending')",
            (_new_id("NTF"), item["item_id"], detail.get("conflict_id"),
             item["claimed_by"], now),
        )


# ------------------------------------------------------- 交接确认 ----
def handoff_queue(conn: sqlite3.Connection, executor: str | None = None) -> list[dict]:
    sql = (
        "SELECT n.*, i.subject, i.status AS item_status, c.conflict_type, c.status AS conflict_status"
        " FROM handoff_notifications n"
        " JOIN items i ON i.item_id=n.item_id"
        " JOIN item_conflicts c ON c.conflict_id=n.conflict_id"
    )
    params: list = []
    if executor:
        sql += " WHERE n.executor=?"
        params.append(executor)
    sql += " ORDER BY n.created_at"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def acknowledge_notification(
    conn: sqlite3.Connection, executor: str, notification_id: str,
    note: str = "", clock: Clock | None = None,
) -> dict:
    """执行人确认收到变更。未确认的影响一直留在交接队列，报表更新不会让它消失。"""
    clock = clock or Clock()
    conn.execute("BEGIN IMMEDIATE")
    try:
        row = conn.execute(
            "SELECT * FROM handoff_notifications WHERE notification_id=?", (notification_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"通知不存在: {notification_id}")
        if row["executor"] != executor:
            raise AuthzError("只有通知接收人可以确认")
        if row["status"] == "acknowledged":
            raise ConflictError("通知已确认，不能重复确认")
        conn.execute(
            "UPDATE handoff_notifications SET status='acknowledged', acknowledged_at=?,"
            " executor_note=? WHERE notification_id=?",
            (clock.now(), note, notification_id),
        )
        record_event(conn, row["item_id"], "handoff_acknowledged", executor, clock.now(),
                     None, None, {"notification_id": notification_id, "note": note})
        conn.commit()
        return {"notification_id": notification_id, "status": "acknowledged"}
    except Exception:
        conn.rollback()
        raise
