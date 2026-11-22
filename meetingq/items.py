"""事项（项目真实行动项）生命周期。

严格区分四类事实，状态机与权限在代码中显式守卫：
1. 提案（proposal）：来源摘录，suggestion 只是“有人提议”，decision 是会议决定，二者都不是承诺。
2. 会议决定：decision 型提案进入待确认队列，仍需负责人确认。
3. 负责人确认：只有项目 owner 能把事项转为 confirmed；质量标签不代行。
4. 执行结果：executor 领取后记录进展（只追加），完成后 done。

关联歧义一律人工裁定（link/merge），应用不做标题相似度自动合并。
"""

from __future__ import annotations

import sqlite3
import uuid

from .db import record_audit, record_event
from .errors import AuthzError, ConflictError, NotFoundError, StateError
from .timeutil import Clock

# 状态机：from_status -> 允许的 to_status 集合
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "draft_proposal": {"pending_confirmation", "withdrawn"},
    "pending_confirmation": {"confirmed", "withdrawn", "claim_blocked"},
    "confirmed": {"in_progress", "withdrawn", "claim_blocked"},
    "claim_blocked": {"confirmed", "withdrawn"},
    "in_progress": {"done", "withdrawn", "in_progress_impact"},
    "in_progress_impact": {"in_progress", "withdrawn", "done"},
    "done": set(),
    "withdrawn": set(),
}


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------- 权限 ----
def role_of(conn: sqlite3.Connection, user: str, project: str) -> str | None:
    row = conn.execute(
        "SELECT role FROM members WHERE user_id=? AND project=?", (user, project)
    ).fetchone()
    return row["role"] if row else None


def require_role(conn: sqlite3.Connection, user: str, project: str, *roles: str) -> str:
    role = role_of(conn, user, project)
    if role is None:
        raise AuthzError(f"用户 {user} 不是项目 {project} 成员")
    if roles and role not in roles:
        raise AuthzError(f"用户 {user} 在项目 {project} 的角色 {role} 不允许该操作，需要 {roles}")
    return role


def proposal_context(conn: sqlite3.Connection, proposal: str) -> sqlite3.Row:
    """确认事项时必须知道其依赖的会议决定：返回提案→摘要→会议→项目全链路。"""
    row = conn.execute(
        "SELECT p.proposal,p.subject,p.kind,p.impact,p.mentioned_owner,p.summary,"
        "       s.meeting,s.version,s.received_at AS summary_received,"
        "       m.project,m.team,m.type AS meeting_type,m.start_at"
        " FROM proposals p"
        " JOIN summaries s ON s.summary=p.summary"
        " JOIN meetings m ON m.meeting=s.meeting"
        " WHERE p.proposal=?",
        (proposal,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"提案不存在: {proposal}")
    return row


def get_item(conn: sqlite3.Connection, item_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM items WHERE item_id=?", (item_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"事项不存在: {item_id}")
    return row


def _transition(conn, item: sqlite3.Row, to_status: str) -> None:
    if to_status not in ALLOWED_TRANSITIONS[item["status"]]:
        raise StateError(
            f"事项 {item['item_id']} 状态 {item['status']} 不允许转到 {to_status}"
        )


def _guard_row_version(conn, item_id: str, expected: int | None) -> sqlite3.Row:
    """乐观锁：并发操作携带 row_version，后到者不能覆盖先到的人工决定。"""
    item = get_item(conn, item_id)
    if expected is not None and item["row_version"] != expected:
        raise ConflictError(
            f"事项 {item_id} 已被其他操作修改（版本 {item['row_version']} != {expected}），请刷新后重试"
        )
    return item


def _update_status(
    conn, item: sqlite3.Row, to_status: str, actor: str, clock: Clock, event: str, detail: dict
) -> None:
    _transition(conn, item, to_status)
    conn.execute(
        "UPDATE items SET status=?, row_version=row_version+1 WHERE item_id=? AND row_version=?",
        (to_status, item["item_id"], item["row_version"]),
    )
    record_event(conn, item["item_id"], event, actor, clock.now(),
                 item["status"], to_status, detail)


# ----------------------------------------------------------- 事项建立 ----
def create_item_from_proposal(
    conn: sqlite3.Connection, actor: str, proposal: str, clock: Clock | None = None
) -> dict:
    """从来源提案建立事项。

    - decision（会议决定）→ pending_confirmation，等待负责人确认；
    - suggestion（有人提议）→ draft_proposal，只有负责人显式提升才能进入确认队列；
    应用本身绝不把建议直接变成承诺。
    """
    clock = clock or Clock()
    ctx = proposal_context(conn, proposal)
    if conn.execute(
        "SELECT 1 FROM item_links WHERE proposal=?", (proposal,)
    ).fetchone():
        raise StateError(f"提案 {proposal} 已关联事项，不能重复建立；如需归并用 merge")
    require_role(conn, actor, ctx["project"], "owner", "reviewer")
    item_id = _new_id("ITM")
    initial = "pending_confirmation" if ctx["kind"] == "decision" else "draft_proposal"
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "INSERT INTO items(item_id,project,subject,impact,status,origin,created_by,created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (item_id, ctx["project"], ctx["subject"], ctx["impact"], initial,
             "source_proposal", actor, clock.now()),
        )
        conn.execute(
            "INSERT INTO item_links(item_id,proposal,linkage,decided_by,decided_at)"
            " VALUES(?,?,?,?,?)",
            (item_id, proposal, "primary", actor, clock.now()),
        )
        record_event(conn, item_id, "created", actor, clock.now(), None, initial,
                     {"proposal": proposal, "kind": ctx["kind"], "meeting": ctx["meeting"]})
        record_audit(conn, actor, "item.create", clock.now(),
                     {"item_id": item_id, "proposal": proposal})
        conn.commit()
        return {"item_id": item_id, "status": initial, "project": ctx["project"]}
    except Exception:
        conn.rollback()
        raise


def request_confirmation(
    conn: sqlite3.Connection, actor: str, item_id: str, clock: Clock | None = None
) -> dict:
    """把建议型草稿提请负责人确认（suggestion → pending_confirmation）。"""
    clock = clock or Clock()
    conn.execute("BEGIN IMMEDIATE")
    try:
        item = get_item(conn, item_id)
        require_role(conn, actor, item["project"], "owner", "reviewer")
        _update_status(conn, item, "pending_confirmation", actor, clock,
                       "confirmation_requested", {})
        conn.commit()
        return {"item_id": item_id, "status": "pending_confirmation"}
    except Exception:
        conn.rollback()
        raise


def confirm_item(
    conn: sqlite3.Connection,
    owner: str,
    item_id: str,
    clock: Clock | None = None,
    note: str | None = None,
) -> dict:
    """负责人确认事项 → confirmed。确认前可回查依赖的会议决定与权限。"""
    clock = clock or Clock()
    conn.execute("BEGIN IMMEDIATE")
    try:
        item = get_item(conn, item_id)
        require_role(conn, owner, item["project"], "owner")
        # 确认必须基于会议决定链路：把依赖事实写入事件，供后续回查。
        links = conn.execute(
            "SELECT proposal FROM item_links WHERE item_id=? AND linkage IN ('primary','reference')",
            (item_id,),
        ).fetchall()
        dependencies = [dict(proposal_context(conn, r["proposal"])) for r in links]
        _update_status(conn, item, "confirmed", owner, clock, "confirmed",
                       {"dependencies": dependencies, "note": note})
        conn.execute(
            "UPDATE items SET confirmed_by=?, confirmed_at=?, note=COALESCE(?,note) WHERE item_id=?",
            (owner, clock.now(), note, item_id),
        )
        conn.execute(
            "INSERT INTO lead_decisions(project,owner,decided_at,subject_type,subject_id,action,note)"
            " VALUES(?,?,?,?,?,?,?)",
            (item["project"], owner, clock.now(), "confirmation", item_id, "confirm", note),
        )
        conn.commit()
        return {"item_id": item_id, "status": "confirmed", "dependencies": dependencies}
    except Exception:
        conn.rollback()
        raise


def reject_item(
    conn: sqlite3.Connection, owner: str, item_id: str, note: str,
    clock: Clock | None = None,
) -> dict:
    """负责人拒绝/撤销事项 → withdrawn（终态，历史保留）。"""
    clock = clock or Clock()
    conn.execute("BEGIN IMMEDIATE")
    try:
        item = get_item(conn, item_id)
        require_role(conn, owner, item["project"], "owner")
        _update_status(conn, item, "withdrawn", owner, clock, "withdrawn", {"note": note})
        conn.execute(
            "INSERT INTO lead_decisions(project,owner,decided_at,subject_type,subject_id,action,note)"
            " VALUES(?,?,?,?,?,?,?)",
            (item["project"], owner, clock.now(), "confirmation", item_id, "reject", note),
        )
        conn.commit()
        return {"item_id": item_id, "status": "withdrawn"}
    except Exception:
        conn.rollback()
        raise


# ----------------------------------------------------------- 执行侧 ----
def claim_item(
    conn: sqlite3.Connection, executor: str, item_id: str,
    clock: Clock | None = None, expected_version: int | None = None,
) -> dict:
    """执行人领取事项。claim_blocked 期间禁止领取；唯一领取人由状态机保证。"""
    clock = clock or Clock()
    conn.execute("BEGIN IMMEDIATE")
    try:
        item = _guard_row_version(conn, item_id, expected_version)
        require_role(conn, executor, item["project"], "executor")
        if item["status"] == "claim_blocked":
            raise StateError(
                f"事项 {item_id} 存在未裁定的来源冲突，待负责人复核前不可领取"
            )
        _update_status(conn, item, "in_progress", executor, clock, "claimed", {})
        conn.execute(
            "UPDATE items SET claimed_by=?, claimed_at=? WHERE item_id=?",
            (executor, clock.now(), item_id),
        )
        conn.commit()
        return {"item_id": item_id, "status": "in_progress", "claimed_by": executor}
    except Exception:
        conn.rollback()
        raise


def record_progress(
    conn: sqlite3.Connection, executor: str, item_id: str, hours_spent: float,
    note: str, clock: Clock | None = None,
) -> dict:
    """登记进展与已花费工时。只追加，不允许改写历史工时。"""
    clock = clock or Clock()
    if hours_spent < 0:
        raise StateError("工时不能为负")
    conn.execute("BEGIN IMMEDIATE")
    try:
        item = get_item(conn, item_id)
        require_role(conn, executor, item["project"], "executor")
        if item["claimed_by"] != executor:
            raise AuthzError(f"事项 {item_id} 由 {item['claimed_by']} 领取，{executor} 不能登记进展")
        if item["status"] not in ("in_progress", "in_progress_impact"):
            raise StateError(f"事项 {item_id} 状态 {item['status']} 不可登记进展")
        cur = conn.execute(
            "INSERT INTO progress_entries(item_id,executor,recorded_at,hours_spent,note)"
            " VALUES(?,?,?,?,?)",
            (item_id, executor, clock.now(), hours_spent, note),
        )
        record_event(conn, item_id, "progress", executor, clock.now(),
                     item["status"], item["status"],
                     {"hours_spent": hours_spent, "note": note})
        conn.commit()
        return {"progress_id": cur.lastrowid, "item_id": item_id, "hours_spent": hours_spent}
    except Exception:
        conn.rollback()
        raise


def complete_item(
    conn: sqlite3.Connection, executor: str, item_id: str,
    result_note: str, clock: Clock | None = None,
) -> dict:
    clock = clock or Clock()
    conn.execute("BEGIN IMMEDIATE")
    try:
        item = get_item(conn, item_id)
        require_role(conn, executor, item["project"], "executor")
        if item["claimed_by"] != executor:
            raise AuthzError("只有领取人可以登记执行结果")
        _update_status(conn, item, "done", executor, clock, "completed", {"result": result_note})
        conn.commit()
        return {"item_id": item_id, "status": "done"}
    except Exception:
        conn.rollback()
        raise


# ----------------------------------------------------- 人工关联裁定 ----
def link_proposal(
    conn: sqlite3.Connection, actor: str, item_id: str, proposal: str,
    linkage: str = "reference", note: str | None = None, clock: Clock | None = None,
) -> dict:
    """把另一场会议/摘要中的提案人工关联到既有事项。

    会议合并、说话人更正、摘要重跑不自动产生新事项；关联有歧义时由人工在此裁定。
    """
    clock = clock or Clock()
    if linkage not in ("reference", "duplicate_merged"):
        raise StateError("linkage 仅支持 reference / duplicate_merged")
    conn.execute("BEGIN IMMEDIATE")
    try:
        item = get_item(conn, item_id)
        require_role(conn, actor, item["project"], "owner", "reviewer")
        ctx = proposal_context(conn, proposal)
        if ctx["project"] != item["project"]:
            raise AuthzError("跨项目提案不能关联到该事项")
        existing = conn.execute(
            "SELECT item_id, linkage FROM item_links WHERE proposal=?", (proposal,)
        ).fetchone()
        if existing:
            if existing["item_id"] == item_id:
                raise StateError("该提案已关联本事项")
            raise StateError(
                f"提案 {proposal} 已关联事项 {existing['item_id']}；"
                "若确认为同一真实事项，请使用 merge 归并"
            )
        conn.execute(
            "INSERT INTO item_links(item_id,proposal,linkage,decided_by,decided_at,note)"
            " VALUES(?,?,?,?,?,?)",
            (item_id, proposal, linkage, actor, clock.now(), note),
        )
        record_event(conn, item_id, "proposal_linked", actor, clock.now(),
                     item["status"], item["status"],
                     {"proposal": proposal, "linkage": linkage, "meeting": ctx["meeting"]})
        conn.commit()
        return {"item_id": item_id, "proposal": proposal, "linkage": linkage}
    except Exception:
        conn.rollback()
        raise


def merge_items(
    conn: sqlite3.Connection, actor: str, kept_item_id: str, duplicate_item_id: str,
    note: str, clock: Clock | None = None,
) -> dict:
    """裁定两个事项为同一真实事项：保留 kept，工作记录与来源关联并入，duplicate 撤销。

    绝不按标题自动合并；调用方必须显式给出两个事项与裁定说明。
    已花费工时通过事件与 progress 保留在 kept 的可回查历史中。
    """
    clock = clock or Clock()
    conn.execute("BEGIN IMMEDIATE")
    try:
        kept = get_item(conn, kept_item_id)
        dup = get_item(conn, duplicate_item_id)
        if kept_item_id == duplicate_item_id:
            raise StateError("不能归并事项自身")
        require_role(conn, actor, kept["project"], "owner")
        if dup["project"] != kept["project"]:
            raise AuthzError("跨项目事项不能归并")
        if kept["status"] in ("withdrawn", "done"):
            raise StateError(f"保留事项处于 {kept['status']}，不可作为归并目标")
        # 把 duplicate 的来源链接迁到 kept（冲突行跳过并登记）
        moved: list[str] = []
        for link in conn.execute(
            "SELECT proposal,linkage FROM item_links WHERE item_id=?", (duplicate_item_id,)
        ).fetchall():
            clash = conn.execute(
                "SELECT 1 FROM item_links WHERE item_id=? AND proposal=?",
                (kept_item_id, link["proposal"]),
            ).fetchone()
            if clash:
                continue
            conn.execute(
                "INSERT INTO item_links(item_id,proposal,linkage,decided_by,decided_at,note)"
                " VALUES(?,?,?,?,?,?)",
                (kept_item_id, link["proposal"], "duplicate_merged", actor, clock.now(), note),
            )
            moved.append(link["proposal"])
        # 进展归属：执行人不同则保留原执行人记录，并在 kept 上留痕
        for prog in conn.execute(
            "SELECT * FROM progress_entries WHERE item_id=? AND superseded=0",
            (duplicate_item_id,),
        ).fetchall():
            conn.execute(
                "INSERT INTO progress_entries(item_id,executor,recorded_at,hours_spent,note)"
                " VALUES(?,?,?,?,?)",
                (kept_item_id, prog["executor"], prog["recorded_at"],
                 prog["hours_spent"], f"[归并自 {duplicate_item_id}] {prog['note']}"),
            )
        if dup["status"] != "withdrawn":
            _transition(conn, dup, "withdrawn")
            conn.execute(
                "UPDATE items SET status='withdrawn', row_version=row_version+1,"
                " open_conflict_id=NULL WHERE item_id=?",
                (duplicate_item_id,),
            )
            record_event(conn, duplicate_item_id, "merged_away", actor, clock.now(),
                         dup["status"], "withdrawn",
                         {"kept_item_id": kept_item_id, "note": note})
        record_event(conn, kept_item_id, "merge_kept", actor, clock.now(),
                     kept["status"], kept["status"],
                     {"duplicate_item_id": duplicate_item_id, "moved_proposals": moved,
                      "note": note})
        conn.execute(
            "INSERT INTO lead_decisions(project,owner,decided_at,subject_type,subject_id,action,note)"
            " VALUES(?,?,?,?,?,?,?)",
            (kept["project"], actor, clock.now(), "conflict", duplicate_item_id,
             "merge", f"并入 {kept_item_id}: {note}"),
        )
        conn.commit()
        return {"kept_item_id": kept_item_id, "merged_item_id": duplicate_item_id,
                "moved_proposals": moved}
    except Exception:
        conn.rollback()
        raise


# ------------------------------------------------------------- 查询 ----
def list_items(conn: sqlite3.Connection, project: str | None = None,
               status: str | None = None) -> list[dict]:
    sql = ("SELECT i.*, "
           "  (SELECT COALESCE(SUM(hours_spent),0) FROM progress_entries p"
           "    WHERE p.item_id=i.item_id AND p.superseded=0) AS hours_spent "
           "FROM items i WHERE 1=1")
    params: list = []
    if project:
        sql += " AND i.project=?"
        params.append(project)
    if status:
        sql += " AND i.status=?"
        params.append(status)
    sql += " ORDER BY i.created_at, i.item_id"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def item_detail(conn: sqlite3.Connection, item_id: str) -> dict:
    item = dict(get_item(conn, item_id))
    item["links"] = [dict(r) for r in conn.execute(
        "SELECT l.*, p.subject, p.kind, p.impact, s.meeting, s.version"
        " FROM item_links l JOIN proposals p ON p.proposal=l.proposal"
        " JOIN summaries s ON s.summary=p.summary WHERE l.item_id=? ORDER BY l.link_id",
        (item_id,),
    ).fetchall()]
    item["events"] = [dict(r) for r in conn.execute(
        "SELECT * FROM item_events WHERE item_id=? ORDER BY event_id", (item_id,)
    ).fetchall()]
    item["progress"] = [dict(r) for r in conn.execute(
        "SELECT * FROM progress_entries WHERE item_id=? AND superseded=0 ORDER BY progress_id",
        (item_id,),
    ).fetchall()]
    item["hours_spent"] = sum(p["hours_spent"] for p in item["progress"])
    return item


def execution_feedback(conn: sqlite3.Connection, project: str | None = None) -> list[dict]:
    """执行反馈查询：事项、领取人、累计工时、最近进展与当前状态一次取齐。"""
    sql = (
        "SELECT i.item_id,i.project,i.subject,i.status,i.claimed_by,i.impact,"
        " COALESCE((SELECT SUM(hours_spent) FROM progress_entries p"
        "   WHERE p.item_id=i.item_id AND p.superseded=0),0) AS hours_spent,"
        " (SELECT COUNT(*) FROM progress_entries p"
        "   WHERE p.item_id=i.item_id AND p.superseded=0) AS progress_count,"
        " (SELECT recorded_at FROM progress_entries p WHERE p.item_id=i.item_id"
        "   ORDER BY progress_id DESC LIMIT 1) AS last_progress_at,"
        " (SELECT note FROM progress_entries p WHERE p.item_id=i.item_id"
        "   ORDER BY progress_id DESC LIMIT 1) AS last_note"
        " FROM items i"
    )
    params: list = []
    if project:
        sql += " WHERE i.project=?"
        params.append(project)
    sql += " ORDER BY i.project, i.item_id"
    return [dict(r) for r in conn.execute(sql, params).fetchall()]
