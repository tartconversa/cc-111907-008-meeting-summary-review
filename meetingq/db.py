"""SQLite 连接与建表。

设计要点：
- 每个工作目录一个独立 SQLite 文件，互不污染；输入文件保持只读。
- 所有业务事实带外键；状态列带 CHECK 约束；并发去重靠唯一索引兜底。
- 每次人工操作都追加 item_events / 通用审计，状态可回放。
- 写入开启 WAL 与 busy_timeout，配合条件 UPDATE（状态守卫）实现乐观锁。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

SCHEMA = """
PRAGMA foreign_keys = ON;

-- 导入批次 ----------------------------------------------------------------
CREATE TABLE import_runs (
    run_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    input_dir       TEXT NOT NULL,
    manifest_sha    TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    counts_json     TEXT
);

CREATE TABLE import_issues (
    issue_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER REFERENCES import_runs(run_id),
    entity          TEXT NOT NULL,          -- 来源表名
    source_key      TEXT NOT NULL,          -- 来源主键
    kind            TEXT NOT NULL,          -- reinserted/content_conflict/orphan/rejected/...
    detail          TEXT NOT NULL,
    seen_at         TEXT NOT NULL,
    UNIQUE(entity, source_key, kind, detail)
);

-- 来源事实（只追加；同主键重复投递不产生新业务活动） ----------------------
CREATE TABLE meetings (
    meeting         TEXT PRIMARY KEY,
    project         TEXT NOT NULL,
    team            TEXT NOT NULL,
    type            TEXT NOT NULL,
    start_at        TEXT NOT NULL
);

CREATE TABLE summaries (
    summary         TEXT PRIMARY KEY,
    meeting         TEXT NOT NULL REFERENCES meetings(meeting),
    version         INTEGER NOT NULL,
    received_at     TEXT NOT NULL,
    UNIQUE(meeting, version)
);

CREATE TABLE proposals (
    proposal        TEXT PRIMARY KEY,
    summary         TEXT NOT NULL REFERENCES summaries(summary),
    subject         TEXT NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('suggestion','decision')),
    impact          TEXT NOT NULL CHECK (impact IN ('low','high')),
    mentioned_owner TEXT,
    received_at     TEXT NOT NULL
);

CREATE TABLE members (
    user_id         TEXT NOT NULL,
    project         TEXT NOT NULL,
    role            TEXT NOT NULL CHECK (role IN ('owner','executor','reviewer')),
    PRIMARY KEY (user_id, project)
);

-- 随资料导入的人工标注（契约 reviews 表），与应用内复核轮次分开存储
CREATE TABLE source_reviews (
    review_id       TEXT PRIMARY KEY,
    proposal        TEXT NOT NULL REFERENCES proposals(proposal),
    reviewer        TEXT NOT NULL,
    label           TEXT NOT NULL CHECK (label IN
                        ('accepted','extra','missing','duplicate','owner','unknown')),
    received_at     TEXT NOT NULL
);

CREATE TABLE review_policy_versions (
    policy_id       TEXT PRIMARY KEY,
    minutes_available   REAL NOT NULL CHECK (minutes_available > 0),
    minutes_per_review  REAL NOT NULL CHECK (minutes_per_review > 0),
    ordinary_fraction   REAL NOT NULL CHECK (ordinary_fraction BETWEEN 0 AND 1),
    confidence          REAL NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    received_at     TEXT NOT NULL
);

-- 事项（项目真实行动项） ---------------------------------------------------
-- 状态机：
--   draft_proposal      建议型提案，尚未提请负责人确认
--   pending_confirmation 待负责人确认（决定型提案进入系统即在此）
--   confirmed           负责人已确认，可被执行人领取
--   claim_blocked       已确认未领取，但出现来源冲突，待负责人裁定，期间禁止领取
--   in_progress         执行人已领取并保留进度
--   in_progress_impact  执行中出现来源冲突，维持原承诺与进度，等待影响确认
--   done                执行完成（终态）
--   withdrawn           负责人撤销（终态；历史与进度保留）
CREATE TABLE items (
    item_id         TEXT PRIMARY KEY,
    project         TEXT NOT NULL,
    subject         TEXT NOT NULL,
    impact          TEXT NOT NULL CHECK (impact IN ('low','high')),
    status          TEXT NOT NULL CHECK (status IN (
                        'draft_proposal','pending_confirmation','confirmed',
                        'claim_blocked','in_progress','in_progress_impact',
                        'done','withdrawn')),
    origin          TEXT NOT NULL CHECK (origin IN ('source_proposal','review_missing','manual')),
    created_by      TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    confirmed_by    TEXT,
    confirmed_at    TEXT,
    claimed_by      TEXT,
    claimed_at      TEXT,
    row_version     INTEGER NOT NULL DEFAULT 1,   -- 乐观锁
    open_conflict_id INTEGER,
    note            TEXT
);

-- 事项与来源提案的多对多关联：同一事项可被两场会议引用；人工裁定后落账
CREATE TABLE item_links (
    link_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         TEXT NOT NULL REFERENCES items(item_id),
    proposal        TEXT NOT NULL REFERENCES proposals(proposal),
    linkage         TEXT NOT NULL CHECK (linkage IN ('primary','reference','duplicate_merged')),
    decided_by      TEXT,
    decided_at      TEXT,
    note            TEXT,
    UNIQUE(item_id, proposal)
);

CREATE TABLE item_events (
    event_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         TEXT NOT NULL REFERENCES items(item_id),
    event_type      TEXT NOT NULL,
    actor           TEXT NOT NULL,
    occurred_at     TEXT NOT NULL,
    from_status     TEXT,
    to_status       TEXT,
    detail_json     TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE progress_entries (
    progress_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         TEXT NOT NULL REFERENCES items(item_id),
    executor        TEXT NOT NULL,
    recorded_at     TEXT NOT NULL,
    hours_spent     REAL NOT NULL CHECK (hours_spent >= 0),
    note            TEXT NOT NULL,
    -- 已花费工时不可改写：只允许追加
    superseded      INTEGER NOT NULL DEFAULT 0
);

-- 新摘要与已确认事项的冲突 -----------------------------------------------
-- conflict_type:
--   contradict 新来源与现承诺事实矛盾
--   withdraw    新来源撤回/删除了原决定
--   duplicate   疑似重复事项（跨会议/跨摘要），需人工裁定保留哪个
--   ambiguous   语义关联有歧义，系统不自动合并
CREATE TABLE item_conflicts (
    conflict_id     TEXT PRIMARY KEY,
    item_id         TEXT NOT NULL REFERENCES items(item_id),
    new_proposal    TEXT REFERENCES proposals(proposal),
    conflict_type   TEXT NOT NULL CHECK (conflict_type IN
                        ('contradict','withdraw','duplicate','ambiguous')),
    status          TEXT NOT NULL CHECK (status IN ('open','upheld','revoked','merged','dismissed'))
                        DEFAULT 'open',
    detected_at     TEXT NOT NULL,
    decided_by      TEXT,
    decided_at      TEXT,
    decision_note   TEXT,
    kept_item_id    TEXT REFERENCES items(item_id),
    UNIQUE(item_id, new_proposal, conflict_type)
);

-- 负责人裁定流水（复核结论处置与冲突裁定都落这里，防止后到标签覆盖人工决定）
CREATE TABLE lead_decisions (
    decision_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    project         TEXT NOT NULL,
    owner           TEXT NOT NULL,
    decided_at      TEXT NOT NULL,
    subject_type    TEXT NOT NULL CHECK (subject_type IN ('conflict','review_outcome','confirmation')),
    subject_id      TEXT NOT NULL,
    action          TEXT NOT NULL,   -- uphold/revoke/merge/confirm/reject/create_proposal/...
    note            TEXT,
    UNIQUE(subject_type, subject_id, action)
);

-- 质量复核 ----------------------------------------------------------------
CREATE TABLE review_rounds (
    round_id        TEXT PRIMARY KEY,
    policy_id       TEXT NOT NULL REFERENCES review_policy_versions(policy_id),
    created_at      TEXT NOT NULL,
    planned_n       INTEGER NOT NULL,
    ordinary_n      INTEGER NOT NULL,
    targeted_n      INTEGER NOT NULL,
    seed            INTEGER NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('planned','closed')) DEFAULT 'planned',
    closed_at       TEXT,
    scope_json      TEXT NOT NULL    -- 明确记录抽样范围与分层定义
);

CREATE TABLE review_tasks (
    task_id         TEXT PRIMARY KEY,
    round_id        TEXT NOT NULL REFERENCES review_rounds(round_id),
    proposal        TEXT NOT NULL REFERENCES proposals(proposal),
    stratum         TEXT NOT NULL,   -- 例如 project:impact:kind
    sample_kind     TEXT NOT NULL CHECK (sample_kind IN ('probability','targeted')),
    inclusion_prob  REAL,            -- 概率样本必须保存入选概率；定向样本为 NULL
    status          TEXT NOT NULL CHECK (status IN ('assigned','adjudicated','cancelled'))
                        DEFAULT 'assigned',
    final_label     TEXT CHECK (final_label IS NULL OR final_label IN
                        ('accepted','extra','missing','duplicate','owner','unknown','conflict')),
    created_at      TEXT NOT NULL,
    UNIQUE(round_id, proposal)
);

CREATE TABLE review_labels (
    label_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id         TEXT NOT NULL REFERENCES review_tasks(task_id),
    reviewer        TEXT NOT NULL,
    label           TEXT NOT NULL CHECK (label IN
                        ('accepted','extra','missing','duplicate','owner','unknown')),
    received_at     TEXT NOT NULL,
    is_adjudication INTEGER NOT NULL DEFAULT 0,
    UNIQUE(task_id, reviewer)
);

-- 执行人交接通知 -----------------------------------------------------------
CREATE TABLE handoff_notifications (
    notification_id TEXT PRIMARY KEY,
    item_id         TEXT NOT NULL REFERENCES items(item_id),
    conflict_id     TEXT NOT NULL REFERENCES item_conflicts(conflict_id),
    executor        TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('pending','acknowledged')) DEFAULT 'pending',
    acknowledged_at TEXT,
    executor_note   TEXT,
    UNIQUE(conflict_id, executor)
);

-- 通用审计（非事项类操作也可回放） ----------------------------------------
CREATE TABLE audit_log (
    audit_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    actor           TEXT NOT NULL,
    action          TEXT NOT NULL,
    occurred_at     TEXT NOT NULL,
    detail_json     TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX idx_items_status ON items(status);
CREATE INDEX idx_items_project ON items(project);
CREATE INDEX idx_links_proposal ON item_links(proposal);
CREATE INDEX idx_tasks_round ON review_tasks(round_id);
CREATE INDEX idx_labels_task ON review_labels(task_id);
CREATE INDEX idx_conflicts_status ON item_conflicts(status);
CREATE INDEX idx_notifications_status ON handoff_notifications(status);
"""


def connect(workdir: str | Path) -> sqlite3.Connection:
    """打开（必要时创建）工作目录中的状态库。

    check_same_thread=False 允许线程内使用，但所有写操作仍以
    BEGIN IMMEDIATE + busy_timeout 串行化，业务侧另用条件 UPDATE 做状态守卫。
    """
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    db_path = workdir / "meetingq.sqlite3"
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None,
                           check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def initialize(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    existing = conn.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    if existing is not None:
        if int(existing["value"]) != SCHEMA_VERSION:
            raise RuntimeError(
                f"状态库 schema 版本 {existing['value']} 与应用 {SCHEMA_VERSION} 不一致"
            )
        return
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO schema_meta(key,value) VALUES('schema_version',?)",
        (str(SCHEMA_VERSION),),
    )


def record_event(
    conn: sqlite3.Connection,
    item_id: str,
    event_type: str,
    actor: str,
    occurred_at: str,
    from_status: str | None = None,
    to_status: str | None = None,
    detail: dict | None = None,
) -> None:
    import json

    conn.execute(
        "INSERT INTO item_events(item_id,event_type,actor,occurred_at,from_status,to_status,detail_json)"
        " VALUES(?,?,?,?,?,?,?)",
        (
            item_id,
            event_type,
            actor,
            occurred_at,
            from_status,
            to_status,
            json.dumps(detail or {}, ensure_ascii=False),
        ),
    )


def record_audit(
    conn: sqlite3.Connection, actor: str, action: str, occurred_at: str, detail: dict | None = None
) -> None:
    import json

    conn.execute(
        "INSERT INTO audit_log(actor,action,occurred_at,detail_json) VALUES(?,?,?,?)",
        (actor, action, occurred_at, json.dumps(detail or {}, ensure_ascii=False)),
    )
