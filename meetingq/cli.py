"""命令行入口（本地离线，仅标准库）。

通用参数：
  --workdir   工作目录，状态库与报告写入其中，与输入目录完全分离（默认 ./workdir）
  --as        操作人 user id，权限按 members 表中的项目角色校验
  --at        覆盖当前 UTC 时间（演练/测试可重复），格式 2026-11-23T10:00:00Z
  --json      以 JSON 输出，便于脚本消费

示例：
  python -m meetingq.cli import --input inputs
  python -m meetingq.cli items-create --as U1 --proposal A1
  python -m meetingq.cli review-plan --as Q1 --seed 7 --target A1
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import conflicts, importer, items, reports, reviews, sampling, workflow
from .db import connect, initialize
from .errors import MeetingQError
from .timeutil import Clock


def _clock(args: argparse.Namespace) -> Clock:
    return Clock.fixed(args.at) if args.at else Clock()


def _print(args: argparse.Namespace, payload) -> None:
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    if isinstance(payload, list):
        if not payload:
            print("（无记录）")
            return
        cols = list(payload[0].keys())
        print("\t".join(cols))
        for row in payload:
            print("\t".join(str(row.get(c, "")) for c in cols))
    elif isinstance(payload, dict):
        for k, v in payload.items():
            print(f"{k}: {v}")
    else:
        print(payload)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="meetingq", description="会议摘要质量与事项落实本地应用")
    p.add_argument("--workdir", default="workdir", help="本地工作目录（默认 ./workdir）")
    p.add_argument("--as", dest="actor", help="操作人 user id")
    p.add_argument("--at", help="覆盖当前 UTC 时间，如 2026-11-23T10:00:00Z")
    p.add_argument("--json", action="store_true", help="JSON 输出")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("import", help="幂等导入符合契约的输入目录")
    s.add_argument("--input", required=True, help="输入目录（只读，如 inputs）")

    s = sub.add_parser("demo", help="合成资料端到端演练")
    s.add_argument("--input", default="inputs")

    # 事项 ----------------------------------------------------------------
    s = sub.add_parser("items-create", help="从提案建立事项（建议→草稿，决定→待确认）")
    s.add_argument("--proposal", required=True)

    s = sub.add_parser("items-request", help="建议草稿提请负责人确认")
    s.add_argument("--item", required=True)

    s = sub.add_parser("items-confirm", help="负责人确认事项")
    s.add_argument("--item", required=True)
    s.add_argument("--note", default=None)

    s = sub.add_parser("items-reject", help="负责人拒绝/撤销事项")
    s.add_argument("--item", required=True)
    s.add_argument("--note", required=True)

    s = sub.add_parser("items-claim", help="执行人领取事项")
    s.add_argument("--item", required=True)
    s.add_argument("--expected-version", type=int, default=None, help="乐观锁版本号")

    s = sub.add_parser("items-progress", help="执行人登记进展（工时只追加）")
    s.add_argument("--item", required=True)
    s.add_argument("--hours", type=float, required=True)
    s.add_argument("--note", required=True)

    s = sub.add_parser("items-complete", help="执行人登记执行结果并完成")
    s.add_argument("--item", required=True)
    s.add_argument("--note", required=True)

    s = sub.add_parser("items-link", help="人工关联另一提案到既有事项（不自动合并）")
    s.add_argument("--item", required=True)
    s.add_argument("--proposal", required=True)
    s.add_argument("--linkage", choices=["reference", "duplicate_merged"], default="reference")
    s.add_argument("--note", default=None)

    s = sub.add_parser("items-merge", help="人工裁定重复事项归并，保留工作记录")
    s.add_argument("--keep", required=True)
    s.add_argument("--duplicate", required=True)
    s.add_argument("--note", required=True)

    s = sub.add_parser("items-list", help="查询事项")
    s.add_argument("--project", default=None)
    s.add_argument("--status", default=None)

    s = sub.add_parser("items-detail", help="事项全链路回查")
    s.add_argument("--item", required=True)

    s = sub.add_parser("execution-feedback", help="执行人进展与工时反馈查询")
    s.add_argument("--project", default=None)

    s = sub.add_parser("version-diff", help="同一会议摘要版本间变化")
    s.add_argument("--meeting", required=True)

    # 冲突与交接 ----------------------------------------------------------
    s = sub.add_parser("conflicts-detect", help="扫描新摘要与事项的待裁定关系")

    s = sub.add_parser("conflicts-list", help="负责人待裁定冲突")
    s.add_argument("--project", default=None)

    s = sub.add_parser("conflicts-decide", help="负责人裁定冲突")
    s.add_argument("--conflict", required=True)
    s.add_argument("--action", choices=["uphold", "revoke", "merge", "dismiss"], required=True)
    s.add_argument("--note", required=True)
    s.add_argument("--keep-item", default=None)

    s = sub.add_parser("handoff-queue", help="执行人交接通知队列")
    s.add_argument("--executor", default=None)

    s = sub.add_parser("handoff-ack", help="执行人确认收到变更")
    s.add_argument("--notification", required=True)
    s.add_argument("--note", default="")

    # 复核 ----------------------------------------------------------------
    s = sub.add_parser("review-plan", help="按策略分层抽样创建复核轮次")
    s.add_argument("--seed", type=int, required=True)
    s.add_argument("--target", action="append", default=[], help="定向争议提案，可重复")
    s.add_argument("--policy", default=None)

    s = sub.add_parser("review-tasks", help="列出轮次任务")
    s.add_argument("--round", required=True)
    s.add_argument("--status", default=None)

    s = sub.add_parser("review-label", help="复核员提交标签")
    s.add_argument("--task", required=True)
    s.add_argument("--label", required=True,
                    choices=["accepted", "extra", "missing", "duplicate", "owner", "unknown"])

    s = sub.add_parser("review-adjudicate", help="双人冲突第三人裁定")
    s.add_argument("--task", required=True)
    s.add_argument("--label", required=True,
                    choices=["accepted", "extra", "missing", "duplicate", "owner", "unknown"])
    s.add_argument("--note", required=True)

    s = sub.add_parser("review-import-labels", help="导入随资料来的人工标注到轮次")
    s.add_argument("--round", required=True)

    s = sub.add_parser("review-consensus", help="把双人一致任务置为已裁定")
    s.add_argument("--round", required=True)

    s = sub.add_parser("review-close", help="关闭复核轮次")
    s.add_argument("--round", required=True)

    s = sub.add_parser("review-apply", help="已裁定标签落业务侧（不越权承诺）")
    s.add_argument("--task", required=True)
    s.add_argument("--note", default=None)

    s = sub.add_parser("lead-queue", help="负责人待裁定队列")
    s.add_argument("--project", default=None)

    # 报告 ----------------------------------------------------------------
    s = sub.add_parser("report", help="质量报告（错误负担、置信区间、降配建议）")
    s.add_argument("--round", default=None)
    s.add_argument("--out", default=None, help="同时写入工作目录下的报告文件")

    s = sub.add_parser("trace-item", help="事项→来源/复核/冲突/交接回查")
    s.add_argument("--item", required=True)

    s = sub.add_parser("trace-review", help="复核任务→提案/事项反向回查")
    s.add_argument("--task", required=True)

    s = sub.add_parser("dashboard", help="负责人工作台计数")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    conn = connect(args.workdir)
    initialize(conn)
    try:
        clock = _clock(args)
        cmd = args.cmd

        if cmd == "import":
            _print(args, importer.import_sources(conn, args.input, clock=clock))
        elif cmd == "demo":
            result = workflow.run_demo(conn, args.input, clock)
            for line in result["log"]:
                print(line)
            if args.json:
                print(json.dumps(result["report"], ensure_ascii=False, indent=2, default=str))

        elif cmd == "items-create":
            _print(args, items.create_item_from_proposal(conn, args.actor, args.proposal, clock))
        elif cmd == "items-request":
            _print(args, items.request_confirmation(conn, args.actor, args.item, clock))
        elif cmd == "items-confirm":
            _print(args, items.confirm_item(conn, args.actor, args.item, clock, args.note))
        elif cmd == "items-reject":
            _print(args, items.reject_item(conn, args.actor, args.item, args.note, clock))
        elif cmd == "items-claim":
            _print(args, items.claim_item(conn, args.actor, args.item, clock,
                                          args.expected_version))
        elif cmd == "items-progress":
            _print(args, items.record_progress(conn, args.actor, args.item,
                                               args.hours, args.note, clock))
        elif cmd == "items-complete":
            _print(args, items.complete_item(conn, args.actor, args.item, args.note, clock))
        elif cmd == "items-link":
            _print(args, items.link_proposal(conn, args.actor, args.item, args.proposal,
                                             args.linkage, args.note, clock))
        elif cmd == "items-merge":
            _print(args, items.merge_items(conn, args.actor, args.keep,
                                           args.duplicate, args.note, clock))
        elif cmd == "items-list":
            _print(args, items.list_items(conn, args.project, args.status))
        elif cmd == "items-detail":
            _print(args, items.item_detail(conn, args.item))
        elif cmd == "execution-feedback":
            _print(args, items.execution_feedback(conn, args.project))
        elif cmd == "version-diff":
            _print(args, sampling.diff_summary_versions(conn, args.meeting))

        elif cmd == "conflicts-detect":
            _print(args, conflicts.detect_conflicts(conn, clock))
        elif cmd == "conflicts-list":
            _print(args, conflicts.list_open_conflicts(conn, args.project))
        elif cmd == "conflicts-decide":
            _print(args, conflicts.decide_conflict(conn, args.actor, args.conflict,
                                                   args.action, args.note, clock,
                                                   args.keep_item))
        elif cmd == "handoff-queue":
            _print(args, conflicts.handoff_queue(conn, args.executor))
        elif cmd == "handoff-ack":
            _print(args, conflicts.acknowledge_notification(conn, args.actor,
                                                            args.notification, args.note, clock))

        elif cmd == "review-plan":
            _print(args, reviews.create_round(conn, args.actor, args.seed,
                                              args.target, args.policy, clock))
        elif cmd == "review-tasks":
            _print(args, reviews.list_tasks(conn, args.round, args.status))
        elif cmd == "review-label":
            _print(args, reviews.submit_label(conn, args.actor, args.task, args.label, clock))
        elif cmd == "review-adjudicate":
            _print(args, reviews.adjudicate_label(conn, args.actor, args.task,
                                                  args.label, args.note, clock))
        elif cmd == "review-import-labels":
            _print(args, reviews.import_source_reviews_to_round(conn, args.actor,
                                                                args.round, clock))
        elif cmd == "review-consensus":
            _print(args, {"adjudicated":
                          reviews.auto_adjudicate_consensus(conn, args.round)})
        elif cmd == "review-close":
            _print(args, reviews.close_round(conn, args.actor, args.round, clock))
        elif cmd == "review-apply":
            _print(args, reviews.apply_adjudicated_outcome(conn, args.actor,
                                                            args.task, clock, args.note))
        elif cmd == "lead-queue":
            _print(args, reviews.pending_lead_decisions(conn, args.project))

        elif cmd == "report":
            report = reports.quality_report(conn, args.round)
            _print(args, report)
            if args.out:
                path = reports.write_report(conn, Path(args.workdir) / args.out, args.round)
                print(f"报告已写入: {path}")
        elif cmd == "trace-item":
            _print(args, reports.item_traceability(conn, args.item))
        elif cmd == "trace-review":
            _print(args, reports.review_traceability(conn, args.task))
        elif cmd == "dashboard":
            _print(args, reports.workload_dashboard(conn))
        else:  # pragma: no cover
            print(f"未知命令: {cmd}", file=sys.stderr)
            return 2
        return 0
    except MeetingQError as exc:
        print(f"操作被拒绝: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
