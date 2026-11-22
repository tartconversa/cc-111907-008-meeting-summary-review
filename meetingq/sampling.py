"""分层概率抽样与质量估计。

防误导设计（对应题述要求）：
- 概率样本与定向争议样本严格分开：只有 probability 任务保存 inclusion_prob，
  定向样本 inclusion_prob 为 NULL，报告不得把它伪装成概率样本。
- 普通样本保证 policy.ordinary_fraction 的最低份额，避免“只审争议事项”。
- 错误率只使用已裁定（确定性）标签；unknown 与双人冲突不计正确也不计错误，单列不确定性。
- 按明确的抽样范围（scope）做分层 Horvitz-Thompson 估计并给出置信区间；
  样本不足以支撑时，明确不建议减少复核。
"""

from __future__ import annotations

import math
import random
import sqlite3
from collections import defaultdict

from .errors import SamplingError

# 常用置信水平的正态分位数
Z_TABLE = {0.90: 1.6448536269514722, 0.95: 1.959963984540054, 0.99: 2.5758293035489004}

# 五类可标注错误 → 用于分错误负担统计
ERROR_LABELS = ("extra", "missing", "duplicate", "owner")
CERTAIN_LABELS = ("accepted",) + ERROR_LABELS


def z_for(confidence: float) -> float:
    if confidence in Z_TABLE:
        return Z_TABLE[confidence]
    # 反函数不可用时退化为最接近的标准水平，保持显式而非静默。
    closest = min(Z_TABLE, key=lambda c: abs(c - confidence))
    return Z_TABLE[closest]


# ----------------------------------------------------------- 抽样框 ----
def build_frame(conn: sqlite3.Connection) -> list[dict]:
    """抽样范围 = 已导入的全部提案；每行带分层键与来源上下文。"""
    rows = conn.execute(
        "SELECT p.proposal,p.kind,p.impact,p.subject,m.project,m.type AS meeting_type,"
        "       s.meeting,s.version"
        " FROM proposals p"
        " JOIN summaries s ON s.summary=p.summary"
        " JOIN meetings m ON m.meeting=s.meeting"
        " ORDER BY p.proposal"
    ).fetchall()
    frame = []
    for r in rows:
        frame.append({
            "proposal": r["proposal"],
            "stratum": f"{r['project']}|{r['impact']}|{r['kind']}|{r['meeting_type']}",
            "project": r["project"],
            "impact": r["impact"],
            "kind": r["kind"],
            "meeting_type": r["meeting_type"],
        })
    return frame


def _largest_remainder(weights: dict[str, int], slots: int) -> dict[str, int]:
    """按层规模比例分配整数名额（最大余数法）。"""
    total = sum(weights.values())
    if total == 0:
        return {}
    raw = {k: slots * v / total for k, v in weights.items()}
    alloc = {k: int(math.floor(v)) for k, v in raw.items()}
    leftover = slots - sum(alloc.values())
    for k in sorted(raw, key=lambda x: raw[x] - alloc[x], reverse=True)[:leftover]:
        alloc[k] += 1
    return alloc


def plan_sample(
    conn: sqlite3.Connection,
    policy: sqlite3.Row,
    seed: int,
    targeted_proposals: list[str] | None = None,
    round_id: str | None = None,
) -> dict:
    """生成本轮复核计划（不落库），返回任务清单与抽样范围说明。"""
    capacity = int(policy["minutes_available"] // policy["minutes_per_review"])
    if capacity <= 0:
        raise SamplingError("可用分钟不足以完成任何一次复核")
    frame = build_frame(conn)
    if not frame:
        raise SamplingError("抽样框为空：请先导入资料")
    targeted = set(targeted_proposals or [])
    known = {u["proposal"] for u in frame}
    unknown_targets = targeted - known
    if unknown_targets:
        raise SamplingError(f"定向提案不在抽样框: {sorted(unknown_targets)}")
    if len(targeted) >= capacity:
        raise SamplingError(
            f"定向任务 {len(targeted)} 个已占满/超过容量 {capacity}，无法保证普通概率样本"
        )

    ordinary_slots = max(1, math.ceil(capacity * policy["ordinary_fraction"]))
    targeted_slots = min(len(targeted), capacity - ordinary_slots)
    probability_slots = capacity - targeted_slots

    # 概率抽样框排除已被定向占用的单元
    prob_frame = [u for u in frame if u["proposal"] not in targeted]
    strata: dict[str, list[dict]] = defaultdict(list)
    for u in prob_frame:
        strata[u["stratum"]].append(u)
    alloc = _largest_remainder({k: len(v) for k, v in strata.items()}, probability_slots)

    rng = random.Random(seed)
    tasks: list[dict] = []
    inclusion: dict[str, float] = {}
    for stratum, units in sorted(strata.items()):
        n_h = min(alloc.get(stratum, 0), len(units))
        chosen = rng.sample(units, n_h) if n_h else []
        prob = n_h / len(units) if units else 0.0
        for u in chosen:
            tasks.append({**u, "sample_kind": "probability", "inclusion_prob": prob})
            inclusion[u["proposal"]] = prob
    # 定向任务：无入选概率，单列
    for u in frame:
        if u["proposal"] in targeted:
            tasks.append({**u, "sample_kind": "targeted", "inclusion_prob": None})

    if len(tasks) > capacity:
        raise SamplingError("内部错误：计划任务超过容量")
    scope = {
        "frame_size": len(frame),
        "strata": {k: len(v) for k, v in sorted(strata.items())},
        "capacity": capacity,
        "ordinary_slots": ordinary_slots,
        "probability_slots": len([t for t in tasks if t["sample_kind"] == "probability"]),
        "targeted_slots": targeted_slots,
        "seed": seed,
        "policy_id": policy["policy_id"],
    }
    return {"round_id": round_id, "tasks": tasks, "scope": scope}


# ----------------------------------------------------------- 估计 ----
def _label_groups(conn: sqlite3.Connection, where: str, params: list) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT t.task_id,t.proposal,t.stratum,t.sample_kind,t.inclusion_prob,t.final_label"
        f" FROM review_tasks t WHERE {where}", params,
    ).fetchall()


def estimate_round(conn: sqlite3.Connection, round_id: str) -> dict:
    """基于一轮中已裁定的概率任务估计错误负担与不确定性。"""
    tasks = _label_groups(conn, "t.round_id=? AND t.status='adjudicated'", [round_id])
    return _estimate(tasks, conn)


def estimate_cumulative(conn: sqlite3.Connection) -> dict:
    """跨轮累计估计：同一提案取最近一轮的裁定，避免重复计入。"""
    tasks = conn.execute(
        "SELECT t.task_id,t.proposal,t.stratum,t.sample_kind,t.inclusion_prob,t.final_label,"
        "       r.created_at"
        " FROM review_tasks t JOIN review_rounds r ON r.round_id=t.round_id"
        " WHERE t.status='adjudicated' ORDER BY r.created_at"
    ).fetchall()
    latest: dict[str, sqlite3.Row] = {}
    for t in tasks:
        latest[t["proposal"]] = t  # 后出现的轮次更新
    return _estimate(list(latest.values()), conn)


def _estimate(tasks: list[sqlite3.Row], conn: sqlite3.Connection) -> dict:
    prob = [t for t in tasks if t["sample_kind"] == "probability"]
    targeted = [t for t in tasks if t["sample_kind"] == "targeted"]
    frame = {u["proposal"]: u for u in build_frame(conn)}

    strata_rows: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for t in prob:
        strata_rows[t["stratum"]].append(t)
    # 各层总体规模 N_h
    n_h_total: dict[str, int] = defaultdict(int)
    for u in frame.values():
        n_h_total[u["stratum"]] += 1

    def indicator(label: str, bucket: str) -> int:
        if bucket == "any_error":
            return 1 if label in ERROR_LABELS else 0
        return 1 if label == bucket else 0

    buckets = ("any_error",) + ERROR_LABELS
    estimates: dict[str, dict] = {}
    confidence = conn.execute(
        "SELECT confidence FROM review_policy_versions ORDER BY rowid DESC LIMIT 1"
    ).fetchone()
    z = z_for(confidence["confidence"] if confidence else 0.95)

    n_certain = sum(1 for t in prob if t["final_label"] in CERTAIN_LABELS)
    n_uncertain = sum(
        1 for t in prob if t["final_label"] in ("unknown", "conflict") or t["final_label"] is None
    )

    for bucket in buckets:
        total = 0.0
        var = 0.0
        for stratum, rows in strata_rows.items():
            N_h = n_h_total[stratum]
            certain = [t for t in rows if t["final_label"] in CERTAIN_LABELS]
            n_h = len(certain)
            if n_h == 0:
                continue
            mean_h = sum(indicator(t["final_label"], bucket) for t in certain) / n_h
            # 层内 SRS 方差（有限总体修正）
            s2 = sum((indicator(t["final_label"], bucket) - mean_h) ** 2 for t in certain)
            s2 = s2 / (n_h - 1) if n_h > 1 else 0.0
            total += N_h * mean_h
            var += (N_h ** 2) * (1 - n_h / N_h) * s2 / n_h
        se = math.sqrt(var)
        rate = total / sum(n_h_total.values()) if n_h_total else 0.0
        se_rate = se / sum(n_h_total.values()) if n_h_total else 0.0
        estimates[bucket] = {
            "estimated_count": round(total, 2),
            "rate": round(rate, 6),
            "se": round(se_rate, 6),
            "ci_low": round(max(0.0, rate - z * se_rate), 6),
            "ci_high": round(min(1.0, rate + z * se_rate), 6),
        }

    # 不确定性负担：unknown/冲突占已裁定概率任务比例（不当正确）
    uncertain_rate = n_uncertain / len(prob) if prob else None
    return {
        "basis": "probability_only",
        "probability_adjudicated": len(prob),
        "certain_labels": n_certain,
        "uncertain_labels": n_uncertain,
        "uncertain_rate": uncertain_rate,
        "targeted_adjudicated": len(targeted),
        "targeted_excluded_from_estimation": True,
        "estimates": estimates,
        "strata_observed": {k: len(v) for k, v in sorted(strata_rows.items())},
    }


def reduction_advice(conn: sqlite3.Connection, estimate: dict, min_certain: int = 30) -> dict:
    """样本不能支撑时不建议减少复核。

    条件（全部满足才允许讨论降配）：
    - 确定性标签数达到 min_certain；
    - 总错误率 95% 置信区间半宽不超过 0.10；
    - 不存在未消化的 unknown/双人冲突；
    - 不存在未裁定的 open 冲突与待确认交接。
    """
    reasons: list[str] = []
    if estimate["certain_labels"] < min_certain:
        reasons.append(
            f"确定性标签 {estimate['certain_labels']} < {min_certain}，样本量不足以稳定估计"
        )
    any_err = estimate["estimates"]["any_error"]
    half_width = (any_err["ci_high"] - any_err["ci_low"]) / 2
    if half_width > 0.10:
        reasons.append(f"总错误率置信区间半宽 {half_width:.3f} > 0.10，精度不足")
    if estimate["uncertain_labels"] > 0:
        reasons.append("存在 unknown 或双人冲突未裁定，不确定性尚未消化")
    open_conflicts = conn.execute(
        "SELECT COUNT(*) AS c FROM item_conflicts WHERE status='open'"
    ).fetchone()["c"]
    if open_conflicts:
        reasons.append(f"存在 {open_conflicts} 个未裁定来源冲突")
    pending_ack = conn.execute(
        "SELECT COUNT(*) AS c FROM handoff_notifications WHERE status='pending'"
    ).fetchone()["c"]
    if pending_ack:
        reasons.append(f"存在 {pending_ack} 条执行人未确认的交接通知")
    advise = "hold" if reasons else "may_reduce"
    return {
        "advice": advise,
        "recommendation": "不建议减少复核，维持当前抽样容量" if advise == "hold"
        else "精度与闭环条件满足，可评估下一轮适度减少复核容量",
        "reasons": reasons,
    }


# ----------------------------------------------------- 版本间变化 ----
def diff_summary_versions(conn: sqlite3.Connection, meeting: str) -> dict:
    """同一会议摘要版本间的提案变化（只陈述事实，不自动合并/替换）。"""
    versions = conn.execute(
        "SELECT s.summary,s.version,s.received_at FROM summaries s"
        " WHERE s.meeting=? ORDER BY s.version", (meeting,),
    ).fetchall()
    out = {"meeting": meeting, "versions": []}
    for v in versions:
        props = conn.execute(
            "SELECT proposal,subject,kind,impact,mentioned_owner FROM proposals"
            " WHERE summary=? ORDER BY proposal", (v["summary"],),
        ).fetchall()
        linked = []
        for p in props:
            link = conn.execute(
                "SELECT item_id,linkage FROM item_links WHERE proposal=?", (p["proposal"],)
            ).fetchone()
            linked.append({**dict(p), "item_id": link["item_id"] if link else None,
                           "linkage": link["linkage"] if link else None})
        out["versions"].append({
            "summary": v["summary"], "version": v["version"],
            "received_at": v["received_at"], "proposals": linked,
        })
    return out
