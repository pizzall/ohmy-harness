"""主控使用的任务账本命令行入口。

用法：
    oma --root . task create --id order-paging --intent "订单列表增加游标分页"
    oma task dispatch --id order-paging --session worker-1
    oma task submit --id order-paging --candidate path --summary "接口与服务端"
    oma task verify --id order-paging --outcome pass --check "跨页无遗漏"
    oma status
    oma progress --id order-paging
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .coord import Coordinator
from .core.lifecycle import Limits, TaskStatus
from .store import commands as C


def default_state(root: str) -> Path:
    return Path(root) / ".superagent"


def _coordinator(args: argparse.Namespace) -> Coordinator:
    return Coordinator(default_state(args.root))


def _emit(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def _read_json(path: str) -> dict:
    def unique_fields(pairs: list[tuple]) -> dict:
        fields = dict(pairs)
        if len(fields) != len(pairs):
            raise ValueError(f"{path} 含重复字段，无法确定完整审查范围")
        return fields

    return json.loads(Path(path).read_text(encoding="utf-8-sig"), object_pairs_hook=unique_fields)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="oma", description="ohmy-harness 任务账本")
    parser.add_argument("--root", default=".", help="任务的项目根目录")

    sub = parser.add_subparsers(dest="group", required=True)

    task = sub.add_parser("task", help="任务操作")
    actions = task.add_subparsers(dest="action", required=True)

    p = actions.add_parser("create")
    p.add_argument("--id", required=True)
    p.add_argument("--intent", required=True)
    p.add_argument("--route", default="")
    p.add_argument("--role", default="")
    p.add_argument("--review-scope", help="冻结范围的 JSON 文件，审查任务必填")
    p.add_argument("--input", action="append", default=[])
    p.add_argument("--output", action="append", default=[])
    p.add_argument("--write-scope", action="append", default=[])
    p.add_argument("--dep", action="append", default=[])

    p = actions.add_parser("dispatch")
    p.add_argument("--id", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--workspace", default="")
    p.add_argument("--model", default="")
    p.add_argument("--effort", default="")
    p.add_argument("--max-attempts", type=int, default=3)
    p.add_argument("--force", action="store_true", help="跳过并发写范围检查")

    p = actions.add_parser("begin", help="在原任务上开始下一次尝试（返工）")
    p.add_argument("--id", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--workspace", default="")
    p.add_argument("--model", default="")
    p.add_argument("--effort", default="")
    p.add_argument("--max-attempts", type=int, default=3)

    p = actions.add_parser("reconcile", help="超时后核对原会话/进程/工作区")
    p.add_argument("--id", required=True)
    p.add_argument("--attempt", type=int, required=True)
    p.add_argument("--verdict", required=True, choices=["unknown", "running", "dead", "recovered"])
    p.add_argument("--by", default="main")
    p.add_argument("--candidate", default="", help="收回的候选路径")
    p.add_argument("--evidence", default="", help="关联原尝试、日志和产物版本的收件凭据 JSON")

    p = actions.add_parser("redispatch", help="确认旧尝试结束后重派")
    p.add_argument("--id", required=True)
    p.add_argument("--session", required=True)
    p.add_argument("--workspace", default="")
    p.add_argument("--model", default="")
    p.add_argument("--effort", default="")

    p = actions.add_parser("submit")
    p.add_argument("--id", required=True)
    p.add_argument("--attempt", type=int, default=0)
    p.add_argument("--candidate", required=True)
    p.add_argument("--summary", default="")
    p.add_argument("--evidence", default="")

    p = actions.add_parser("verify")
    p.add_argument("--id", required=True)
    p.add_argument("--outcome", required=True)
    p.add_argument("--check", default="")
    p.add_argument("--review-evidence", action="append", default=[], help="逐项审查证据 JSON，可以分批重复提供")
    p.add_argument("--evidence", default="")
    p.add_argument("--notes", default="")

    p = actions.add_parser("scope", help="重读首次登记的审查范围")
    p.add_argument("--id", required=True)

    p = actions.add_parser("cancel")
    p.add_argument("--id", required=True)
    p.add_argument("--reason", default="")

    p = actions.add_parser("resume")
    p.add_argument("--id", required=True)
    p.add_argument("--reason", default="")

    sub.add_parser("init", help="冷启动时注册并实测本项目的审查 hook")
    sub.add_parser("status", help="项目进度")
    sub.add_parser("reconcile", help="重放并自检日志")
    sub.add_parser("scan", help="重启后核对：已派出但还没有结果的尝试")
    sub.add_parser("capability", help="探测本机各执行者可用性（不联网）")

    progress = sub.add_parser("progress", help="单个任务的进度")
    progress.add_argument("--id", required=True)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    coord = _coordinator(args)

    try:
        if args.group == "init":
            receipt = coord.initialize()
            _emit(receipt)
            return 0 if receipt["status"] == "ready" else 1
        if args.group == "status":
            from .report.progress import project_progress

            _emit(project_progress(coord))
            return 0
        if args.group == "reconcile":
            _emit(coord.reconcile())
            return 0
        if args.group == "progress":
            from .report.progress import task_progress

            _emit(task_progress(coord, args.id))
            return 0
        if args.group == "scan":
            _emit(coord.scan())
            return 0
        if args.group == "capability":
            from .capability import probe, write

            report = probe(Path(args.root))
            write(report, default_state(args.root))
            _emit(report)
            return 0

        action = args.action
        if action == "scope":
            record = coord.record(args.id)
            if record is None or "review_scope" not in record.constraints:
                raise ValueError(f"{args.id} 缺少冻结的审查范围")
            _emit(record.constraints["review_scope"])
            return 0
        if action == "create":
            cmd = C.CreateTask(
                task_id=args.id,
                intent=args.intent,
                project_root=str(Path(args.root).resolve()),
                route=args.route,
                role=args.role,
                constraints={"review_scope": _read_json(args.review_scope)} if args.review_scope else None,
                inputs=tuple(args.input),
                outputs=tuple(args.output),
                write_scope=tuple(args.write_scope),
                deps=tuple(args.dep),
            )
        elif action == "dispatch":
            cmd = C.DispatchTask(
                task_id=args.id,
                session=args.session,
                workspace=args.workspace,
                model=args.model,
                effort=args.effort,
                limits=Limits(max_attempts=args.max_attempts),
                force=args.force,
            )
        elif action == "begin":
            cmd = C.BeginAttempt(
                task_id=args.id,
                session=args.session,
                workspace=args.workspace or "",
                model=args.model or "",
                effort=args.effort or "",
                limits=Limits(max_attempts=args.max_attempts),
            )
        elif action == "reconcile":
            cmd = C.ReconcileAttempt(
                task_id=args.id,
                attempt=args.attempt,
                verdict=args.verdict,
                by=args.by,
                candidate=args.candidate,
                evidence=args.evidence,
            )
        elif action == "redispatch":
            cmd = C.Redispatch(
                task_id=args.id,
                session=args.session,
                workspace=args.workspace or "",
                model=args.model or "",
                effort=args.effort or "",
            )
        elif action == "submit":
            record = coord.record(args.id)
            attempt = args.attempt or (record.active_attempt if record else 0)
            cmd = C.SubmitResult(
                task_id=args.id,
                attempt=attempt,
                candidate=args.candidate,
                summary=args.summary,
                evidence=args.evidence,
            )
        elif action == "verify":
            cmd = C.Verify(
                task_id=args.id,
                outcome=args.outcome,
                check=args.check,
                evidence=args.evidence,
                notes=args.notes,
                review_evidence=tuple(map(_read_json, args.review_evidence)),
            )
        elif action == "cancel":
            cmd = C.CancelTask(task_id=args.id, reason=args.reason)
        elif action == "resume":
            cmd = C.ResumeTask(task_id=args.id, reason=args.reason)
        else:  # pragma: no cover
            parser.error(f"未知操作 {action}")
            return 2

        event = coord.execute(cmd)
        _emit({"seq": event.seq, "kind": event.kind, "task": event.task_id})
        return 0
    except Exception as exc:  # noqa: BLE001 - CLI 需要统一错误出口
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
