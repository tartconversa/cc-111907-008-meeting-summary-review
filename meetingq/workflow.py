"""端到端演练脚本：仅使用合成资料，演示完整闭环。

业务线（与 members 角色对齐）：
- P1：owner=U1，executor=U3，reviewer=Q1/Q2；
- P2：owner=U2，executor=U4，reviewer=Q3。

覆盖：
导入 → 建议不自动成承诺 → 负责人确认 → 执行人领取与工时 →
同主题跨会议人工关联/归并（不自动合并）→ 重跑摘要冲突分流
（未领取阻断 / 执行中通知且维持进度）→ 负责人裁定 → 执行人确认 →
分层抽样复核 → 双人冲突裁定 → 复核结论落账不越权 → 质量报告与降配建议。
"""

from __future__ import annotations

import sqlite3

from . import conflicts, importer, items, reports, reviews
from .errors import MeetingQError
from .timeutil import Clock


def run_demo(conn: sqlite3.Connection, input_dir: str, clock: Clock) -> dict:
    log: list[str] = []

    def emit(msg: str) -> None:
        log.append(msg)

    stats = importer.import_sources(conn, input_dir, clock=clock)
    emit(f"导入完成：新增 {stats['inserted']}，重复投递 {stats['reinserted']}，问题 {stats['issues']}")

    # ---- P1 业务线：会议决定 → 确认 → 领取 → 工时 ---------------------
    a1 = items.create_item_from_proposal(conn, "U1", "A1", clock=clock)
    emit(f"A1 会议决定 → 事项 {a1['item_id']} 状态 {a1['status']}（待负责人确认，尚不是承诺）")
    confirmed = items.confirm_item(conn, "U1", a1["item_id"], clock=clock,
                                   note="负责人确认，依据 M1 会议决定")
    emit(f"负责人 U1 确认 {a1['item_id']}，依赖 {len(confirmed['dependencies'])} 条会议链路")
    items.claim_item(conn, "U3", a1["item_id"], clock=clock)
    items.record_progress(conn, "U3", a1["item_id"], 2.5, "完成资料核对初稿", clock=clock)
    emit("执行人 U3 领取并登记 2.5 工时（只追加，重跑摘要不能抹掉）")

    # ---- P2 业务线：建议型提案不自动成承诺 -----------------------------
    a0 = items.create_item_from_proposal(conn, "U2", "A0", clock=clock)
    emit(f"A0 建议型提案 → 事项 {a0['item_id']} 状态 {a0['status']}（有人提议≠承诺）")
    items.request_confirmation(conn, "U2", a0["item_id"], clock=clock)
    items.confirm_item(conn, "U2", a0["item_id"], clock=clock,
                       note="负责人评估后确认，未确认前不可领取")
    # 保持未领取，用于演示 claim_blocked 分流

    # ---- 同主题跨会议：人工关联与人工归并，系统不按标题合并 -----------
    a4 = items.create_item_from_proposal(conn, "U2", "A4", clock=clock)
    items.confirm_item(conn, "U2", a4["item_id"], clock=clock, note="M4 决定确认")
    items.link_proposal(conn, "U2", a4["item_id"], "A8", "reference",
                        note="人工核对：M4/M8 引用同一交付事项", clock=clock)
    emit(f"A8 人工关联到事项 {a4['item_id']}（未做标题相似度自动合并）")
    a12 = items.create_item_from_proposal(conn, "U2", "A12", clock=clock)
    items.request_confirmation(conn, "U2", a12["item_id"], clock=clock)
    items.confirm_item(conn, "U2", a12["item_id"], clock=clock, note="M12 误建的重复事项")
    merged = items.merge_items(conn, "U2", a4["item_id"], a12["item_id"],
                               "人工裁定为同一真实事项，保留 A4 工作线", clock=clock)
    emit(f"重复事项归并：保留 {merged['kept_item_id']}，撤销 {merged['merged_item_id']}，来源链路已并入")

    # ---- 重跑版本 S0b/A0b 与跨会议重复的冲突检测 -----------------------
    found = conflicts.detect_conflicts(conn, clock=clock)
    emit(f"冲突检测发现 {len(found)} 个待人工裁定关系（均不自动删除/替换）")
    blocked = items.list_items(conn, status="claim_blocked")
    emit(f"未领取阻断 {len(blocked)} 个；执行中事项暂不受来源重跑影响")

    # ---- 负责人裁定来源冲突 --------------------------------------------
    for cfl in conflicts.list_open_conflicts(conn, project="P2"):
        action = "dismiss" if cfl["conflict_type"] in ("ambiguous", "duplicate") else "uphold"
        conflicts.decide_conflict(conn, "U2", cfl["conflict_id"], action,
                                  "维持原承诺；新建议仅作参考，不替换原承诺", clock=clock)
    for cfl in conflicts.list_open_conflicts(conn, project="P1"):
        conflicts.decide_conflict(conn, "U1", cfl["conflict_id"], "uphold",
                                  "维持原承诺与实际进度", clock=clock)
    emit("负责人完成来源冲突裁定；任务未被自动删除，新增建议未替换原承诺")

    # ---- 质量复核：分层概率抽样 + 定向争议样本 --------------------------
    rnd = reviews.create_round(conn, "Q1", seed=20261123,
                               targeted_proposals=["A1"], clock=clock)
    n_prob = len([t for t in rnd["tasks"] if t["sample_kind"] == "probability"])
    emit(f"复核轮次 {rnd['round_id']}：概率样本 {n_prob} 个（含入选概率），定向样本 1 个（不可外推）")
    reviews.import_source_reviews_to_round(conn, "Q1", rnd["round_id"], clock=clock)

    # 导入标注：A1 上 accepted/extra 分歧 → conflict，负责人终局裁定
    conflict_tasks = [t for t in reviews.list_tasks(conn, rnd["round_id"])
                      if t["final_label"] == "conflict"]
    for t in conflict_tasks:
        reviews.adjudicate_label(conn, "U1", t["task_id"], "extra",
                                 "负责人终局裁定：摘要把提议写成了承诺", clock=clock)

    # A2 只有一个 unknown：不能当正确；P2 仅一名复核员，负责人调阅记录后裁定
    for t in reviews.list_tasks(conn, rnd["round_id"], status="assigned"):
        if t["proposal"] == "A2":
            reviews.adjudicate_label(conn, "U2", t["task_id"], "accepted",
                                     "负责人调阅 M2 记录后裁定，unknown 不视为正确", clock=clock)

    # 其余零标签任务：P1 双人一致；P2 单人标注后负责人落锤
    for t in reviews.list_tasks(conn, rnd["round_id"], status="assigned"):
        existing = conn.execute(
            "SELECT COUNT(*) AS c FROM review_labels WHERE task_id=? AND is_adjudication=0",
            (t["task_id"],),
        ).fetchone()["c"]
        if existing != 0:
            continue
        if t["project"] == "P1":
            reviews.submit_label(conn, "Q1", t["task_id"], "accepted", clock=clock)
            reviews.submit_label(conn, "Q2", t["task_id"], "accepted", clock=clock)
        else:
            reviews.submit_label(conn, "Q3", t["task_id"], "accepted", clock=clock)
    reviews.auto_adjudicate_consensus(conn, rnd["round_id"])
    for t in reviews.list_tasks(conn, rnd["round_id"], status="assigned"):
        if t["final_label"] == "accepted" or t["final_label"] is None:
            lab = conn.execute(
                "SELECT label FROM review_labels WHERE task_id=? AND is_adjudication=0",
                (t["task_id"],),
            ).fetchone()
            if lab and lab["label"] == "accepted":
                adjudicator = "U2" if t["project"] == "P2" else "U1"
                reviews.adjudicate_label(conn, adjudicator, t["task_id"], "accepted",
                                         "单人标注任务由负责人复核落锤", clock=clock)
    reviews.close_round(conn, "Q1", rnd["round_id"], clock=clock)
    emit("双人一致任务自动确定；分歧任务经裁定；unknown 未被当作正确")

    # ---- 复核结论落账：质量标签不越权 -----------------------------------
    applied = 0
    extra_conflict_id = None
    for t in reviews.list_tasks(conn, rnd["round_id"], status="adjudicated"):
        if t["final_label"] in ("extra", "missing", "duplicate", "owner"):
            outcome = reviews.apply_adjudicated_outcome(conn, "Q1", t["task_id"], clock=clock)
            extra_conflict_id = outcome.get("conflict_id", extra_conflict_id)
            applied += 1
    emit(f"{applied} 个错误标签落业务侧：撤销/归并/归属全部进入负责人队列，质量员未代行承诺")

    # ---- 执行中事项的影响通知：维持进度，执行人显式确认 ----------------
    impact_items = items.list_items(conn, status="in_progress_impact")
    emit(f"执行中受影响事项 {len(impact_items)} 个：维持原承诺与工时，等待影响确认")
    queue = conflicts.handoff_queue(conn)
    emit(f"交接队列待确认 {len(queue)} 条（报表更新不会让其消失）")
    for n in queue:
        conflicts.acknowledge_notification(
            conn, n["executor"], n["notification_id"],
            "已知悉多报争议，保留已完成工时并等待负责人最终裁定", clock=clock)
    emit(f"交接通知 {len(queue)} 条均由执行人显式确认")

    # 已发生工时的事项不能直接撤销：业务守卫拒绝 revoke，只能 uphold+补说明
    if extra_conflict_id:
        try:
            conflicts.decide_conflict(conn, "U1", extra_conflict_id, "revoke",
                                      "尝试撤销（应被拒绝：已花费工时）", clock=clock)
        except MeetingQError as exc:
            emit(f"带工时撤销被系统拒绝：{exc}")
        conflicts.decide_conflict(
            conn, "U1", extra_conflict_id, "uphold",
            "保留事项并补充说明：摘要措辞纠正，承诺范围以会议决定为准", clock=clock)
        emit("负责人改判 uphold 并补说明，事项与工时完整保留")

    report = reports.quality_report(conn, rnd["round_id"])
    advice = report["reduction_advice"]
    any_err = report["estimate"]["estimates"]["any_error"]
    emit(f"质量报告：总错误率 {any_err['rate']}（95%CI {any_err['ci_low']}~{any_err['ci_high']}），"
         f"降配建议={advice['advice']}")
    for reason in advice["reasons"]:
        emit(f"  · {reason}")
    emit(f"负责人工作台：{reports.workload_dashboard(conn)}")

    return {"log": log, "round_id": rnd["round_id"], "report": report}
