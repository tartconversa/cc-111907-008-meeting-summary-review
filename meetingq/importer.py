"""资料导入。

契约要点的落地方式：
- 输入文件只读：本模块从不写回 inputs 目录，状态全部写入工作目录的 SQLite。
- 来源主键标识事实：按主键 UPSERT，重复投递不产生新的业务活动，只登记 reinserted。
- 带版本的来源行以来源身份和版本解释：同会议多版本作为独立 summary 保留，
  版本不构成新的业务确认；同主键内容变化登记 content_conflict，保留问题不静默覆盖。
- received 是事实进入系统的时点，随事实原样保存，分析侧按需以时点过滤。
- 一个导入批次在单事务内提交，失败整体回滚。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from .errors import InputContractError
from .timeutil import Clock, parse_utc

SOURCE_FILES = (
    "meetings",
    "summaries",
    "proposals",
    "members",
    "reviews",
    "review_policy",
)

# 各来源表的主键字段
SOURCE_KEY_FIELDS = {
    "meetings": "meeting",
    "summaries": "summary",
    "proposals": "proposal",
    "members": "user",
    "reviews": "id",
    "review_policy": "id",
}


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_json_array(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:  # 不可信输入：显式转业务异常
        raise InputContractError(f"{path.name} 不是合法 JSON: {exc}") from exc
    if not isinstance(data, list) or not all(isinstance(row, dict) for row in data):
        raise InputContractError(f"{path.name} 必须是对象数组")
    return data


def verify_manifest(input_dir: Path) -> dict[str, str]:
    """核对 manifest 中的 sha256 与实际文件字节，返回实际哈希表。"""
    manifest_path = input_dir / "manifest.json"
    if not manifest_path.exists():
        raise InputContractError("缺少 manifest.json")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual: dict[str, str] = {}
    for name, expected in manifest["files"].items():
        path = input_dir / name
        if not path.exists():
            raise InputContractError(f"manifest 引用的文件不存在: {name}")
        digest = _sha256_file(path)
        actual[name] = digest
        if digest != expected:
            raise InputContractError(
                f"{name} 哈希与 manifest 不一致（文件被改动或投递损坏）"
            )
    return actual


def _add_issue(conn, run_id: int, entity: str, key: str, kind: str, detail: str, seen_at: str) -> None:
    # 同一问题重复出现也不产生重复业务活动。
    conn.execute(
        "INSERT OR IGNORE INTO import_issues(run_id,entity,source_key,kind,detail,seen_at)"
        " VALUES(?,?,?,?,?,?)",
        (run_id, entity, key, kind, detail, seen_at),
    )


def _validate_row(row: dict, columns: set[str], name: str, key_field: str) -> None:
    if set(row) != columns:
        raise InputContractError(
            f"{name} 行字段必须恰好为 {sorted(columns)}，实际 {sorted(row)}"
        )
    if not row.get(key_field):
        raise InputContractError(f"{name} 存在空主键 {key_field}")


def import_sources(
    conn: sqlite3.Connection, input_dir: str | Path, clock: Clock | None = None
) -> dict:
    """把一个符合契约的输入目录幂等导入状态库，返回本批次统计。"""
    clock = clock or Clock()
    input_dir = Path(input_dir)
    if not input_dir.is_dir():
        raise InputContractError(f"输入目录不存在: {input_dir}")
    hashes = verify_manifest(input_dir)
    schema = json.loads((input_dir / "schema.json").read_text(encoding="utf-8"))

    rows: dict[str, list[dict]] = {}
    for name in SOURCE_FILES:
        path = input_dir / f"{name}.json"
        if not path.exists():
            raise InputContractError(f"缺少来源文件: {name}.json")
        rows[name] = _load_json_array(path)
        columns = set(schema[name]["columns"])
        key_field = SOURCE_KEY_FIELDS[name]
        for row in rows[name]:
            _validate_row(row, columns, name, key_field)

    now = clock.now()
    conn.execute("BEGIN IMMEDIATE")
    try:
        cur = conn.execute(
            "INSERT INTO import_runs(input_dir,manifest_sha,started_at) VALUES(?,?,?)",
            (str(input_dir), hashes.get("manifest.json", ""), now),
        )
        run_id = cur.lastrowid
        stats = {"inserted": {k: 0 for k in SOURCE_FILES}, "reinserted": {}, "issues": 0}

        # meetings
        for row in rows["meetings"]:
            parse_utc(row["start"])
            exists = conn.execute(
                "SELECT 1 FROM meetings WHERE meeting=?", (row["meeting"],)
            ).fetchone()
            if exists:
                old = conn.execute(
                    "SELECT project,team,type,start_at FROM meetings WHERE meeting=?",
                    (row["meeting"],),
                ).fetchone()
                new_vals = (row["project"], row["team"], row["type"], row["start"])
                if tuple(old) != new_vals:
                    _add_issue(conn, run_id, "meetings", row["meeting"], "content_conflict",
                               f"既有 {tuple(old)} vs 新 {new_vals}", now)
                    stats["issues"] += 1
                stats["reinserted"].setdefault("meetings", 0)
                stats["reinserted"]["meetings"] += 1
                continue
            conn.execute(
                "INSERT INTO meetings(meeting,project,team,type,start_at) VALUES(?,?,?,?,?)",
                (row["meeting"], row["project"], row["team"], row["type"], row["start"]),
            )
            stats["inserted"]["meetings"] += 1

        # summaries（同会议多版本是正常重跑，UNIQUE(meeting,version) 兜底）
        for row in rows["summaries"]:
            parse_utc(row["received"])
            exists = conn.execute(
                "SELECT 1 FROM summaries WHERE summary=?", (row["summary"],)
            ).fetchone()
            if exists:
                old = conn.execute(
                    "SELECT meeting,version,received_at FROM summaries WHERE summary=?",
                    (row["summary"],),
                ).fetchone()
                new_vals = (row["meeting"], row["version"], row["received"])
                if tuple(old) != new_vals:
                    _add_issue(conn, run_id, "summaries", row["summary"], "content_conflict",
                               f"既有 {tuple(old)} vs 新 {new_vals}", now)
                    stats["issues"] += 1
                stats["reinserted"].setdefault("summaries", 0)
                stats["reinserted"]["summaries"] += 1
                continue
            if not conn.execute("SELECT 1 FROM meetings WHERE meeting=?", (row["meeting"],)).fetchone():
                raise InputContractError(f"summary {row['summary']} 引用未知会议 {row['meeting']}")
            conn.execute(
                "INSERT INTO summaries(summary,meeting,version,received_at) VALUES(?,?,?,?)",
                (row["summary"], row["meeting"], row["version"], row["received"]),
            )
            stats["inserted"]["summaries"] += 1

        # proposals（先校验全部引用，再写库，保证孤儿引用整批回滚）
        known_summaries = {
            r["summary"] for r in conn.execute("SELECT summary FROM summaries")
        } | {r["summary"] for r in rows["summaries"]}
        for row in rows["proposals"]:
            if row["summary"] not in known_summaries:
                raise InputContractError(
                    f"proposal {row['proposal']} 引用未知摘要 {row['summary']}"
                )
        summary_received = {
            r["summary"]: r["received"] for r in rows["summaries"]
        }
        for row in rows["proposals"]:
            exists = conn.execute(
                "SELECT 1 FROM proposals WHERE proposal=?", (row["proposal"],)
            ).fetchone()
            if exists:
                old = conn.execute(
                    "SELECT summary,subject,kind,impact,mentioned_owner FROM proposals WHERE proposal=?",
                    (row["proposal"],),
                ).fetchone()
                new_vals = (row["summary"], row["subject"], row["kind"], row["impact"], row["owner"])
                if tuple(old) != new_vals:
                    _add_issue(conn, run_id, "proposals", row["proposal"], "content_conflict",
                               f"既有 {tuple(old)} vs 新 {new_vals}", now)
                    stats["issues"] += 1
                stats["reinserted"].setdefault("proposals", 0)
                stats["reinserted"]["proposals"] += 1
                continue
            received = conn.execute(
                "SELECT received_at FROM summaries WHERE summary=?", (row["summary"],)
            ).fetchone()
            received_at = received["received_at"] if received else summary_received[row["summary"]]
            conn.execute(
                "INSERT INTO proposals(proposal,summary,subject,kind,impact,mentioned_owner,received_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (row["proposal"], row["summary"], row["subject"], row["kind"],
                 row["impact"], row["owner"], received_at),
            )
            stats["inserted"]["proposals"] += 1

        # members
        for row in rows["members"]:
            exists = conn.execute(
                "SELECT 1 FROM members WHERE user_id=? AND project=?",
                (row["user"], row["project"]),
            ).fetchone()
            if exists:
                stats["reinserted"].setdefault("members", 0)
                stats["reinserted"]["members"] += 1
                continue
            conn.execute(
                "INSERT INTO members(user_id,project,role) VALUES(?,?,?)",
                (row["user"], row["project"], row["role"]),
            )
            stats["inserted"]["members"] += 1

        # source reviews（随资料来的人工标注；孤儿引用拒绝整批回滚）
        for row in rows["reviews"]:
            parse_utc(row["received"])
            if not conn.execute("SELECT 1 FROM proposals WHERE proposal=?", (row["proposal"],)).fetchone():
                raise InputContractError(
                    f"review {row['id']} 引用未知提案 {row['proposal']}"
                )
            exists = conn.execute(
                "SELECT 1 FROM source_reviews WHERE review_id=?", (row["id"],)
            ).fetchone()
            if exists:
                stats["reinserted"].setdefault("reviews", 0)
                stats["reinserted"]["reviews"] += 1
                continue
            conn.execute(
                "INSERT INTO source_reviews(review_id,proposal,reviewer,label,received_at)"
                " VALUES(?,?,?,?,?)",
                (row["id"], row["proposal"], row["reviewer"], row["label"], row["received"]),
            )
            stats["inserted"]["reviews"] += 1

        # review policy versions
        for row in rows["review_policy"]:
            exists = conn.execute(
                "SELECT 1 FROM review_policy_versions WHERE policy_id=?", (row["id"],)
            ).fetchone()
            if exists:
                stats["reinserted"].setdefault("review_policy", 0)
                stats["reinserted"]["review_policy"] += 1
                continue
            conn.execute(
                "INSERT INTO review_policy_versions(policy_id,minutes_available,minutes_per_review,"
                "ordinary_fraction,confidence,received_at) VALUES(?,?,?,?,?,?)",
                (row["id"], row["minutes_available"], row["minutes_per_review"],
                 row["ordinary_fraction"], row["confidence"], now),
            )
            stats["inserted"]["review_policy"] += 1

        conn.execute(
            "UPDATE import_runs SET finished_at=?, counts_json=? WHERE run_id=?",
            (clock.now(), json.dumps(stats, ensure_ascii=False), run_id),
        )
        conn.commit()
        stats["run_id"] = run_id
        return stats
    except Exception:
        conn.rollback()
        raise
