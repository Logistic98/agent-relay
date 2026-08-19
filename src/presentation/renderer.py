from __future__ import annotations

from pathlib import Path

from core.security import redact_text
from domain.models import (
    APPROVAL_PLAN_MAX_CHARS,
    Approval,
    Conversation,
    EventKind,
    Run,
    RunEvent,
    RunStatus,
)

_STATUS_LABELS = {
    RunStatus.QUEUED: "准备中",
    RunStatus.PLANNING: "只读分析中",
    RunStatus.AWAITING_APPROVAL: "等待你的确认",
    RunStatus.RUNNING: "正在执行已批准的计划",
    RunStatus.CANCEL_REQUESTED: "正在停止",
    RunStatus.CANCELLED: "已停止",
    RunStatus.COMPLETED: "已完成",
    RunStatus.FAILED: "失败",
    RunStatus.REJECTED: "已拒绝",
    RunStatus.INTERRUPTED: "因服务重启中断",
    RunStatus.TIMED_OUT: "已超时",
}


def render_run_card(
    conversation: Conversation,
    run: Run,
    events: list[RunEvent],
    approval: Approval | None = None,
) -> str:
    agent = "Codex" if run.agent.value == "codex" else "Claude"
    if run.status == RunStatus.COMPLETED and (run.auto_route or run.mode.value == "ask"):
        if len(run.result or "") > 3700:
            return f"{agent} 已完成，回答较长，正在分段发送…"
        return redact_text(run.result or "（没有文本结果）", limit=3800)

    if run.auto_route and run.status in {RunStatus.QUEUED, RunStatus.PLANNING}:
        return f"{agent} 正在处理…"

    lines = [
        f"{agent} · {workspace_label(conversation.workspace)}",
        _STATUS_LABELS[run.status],
    ]

    if run.status == RunStatus.AWAITING_APPROVAL:
        lines.extend(
            [
                "",
                "我准备这样做（尚未修改文件）：",
                redact_text(run.plan or "（Agent 未返回可显示的计划）", limit=APPROVAL_PLAN_MAX_CHARS),
                "",
                "是否继续执行？",
            ]
        )
    elif run.status == RunStatus.COMPLETED:
        lines.extend(["", redact_text(run.result or "（没有文本结果）", limit=3000)])
    elif run.status in {RunStatus.FAILED, RunStatus.TIMED_OUT, RunStatus.INTERRUPTED}:
        lines.extend(["", redact_text(run.error or _STATUS_LABELS[run.status], limit=2200)])
    elif run.status == RunStatus.REJECTED:
        lines.extend(["", "已拒绝该计划，没有进入写文件或执行命令的阶段。"])
    elif run.status == RunStatus.CANCELLED:
        lines.extend(
            [
                "",
                "停止不会自动回滚此前已经写入的文件，请检查项目差异。",
            ]
        )
    else:
        current = _current_activity(events)
        if current:
            lines.append(current)
        output = "" if run.auto_route else _output_tail(events, limit=1500)
        if output:
            lines.extend(["", output])

    return redact_text("\n".join(lines), limit=3800)


def approval_keyboard(run_id: str) -> dict[str, object]:
    return {
        "inline_keyboard": [
            [
                {"text": "批准并执行", "callback_data": f"approve:{run_id}"},
                {"text": "不执行", "callback_data": f"reject:{run_id}"},
            ]
        ]
    }


def agent_keyboard() -> dict[str, object]:
    return {
        "inline_keyboard": [
            [
                {"text": "使用 Codex", "callback_data": "switch:codex"},
                {"text": "使用 Claude", "callback_data": "switch:claude"},
            ]
        ]
    }


def stop_keyboard(run_id: str) -> dict[str, object]:
    return {"inline_keyboard": [[{"text": "停止任务", "callback_data": f"stop:{run_id}"}]]}


def split_message(text: str, limit: int = 3800) -> list[str]:
    if limit < 100:
        raise ValueError("message limit is too small")
    if not text:
        return [""]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        boundary = remaining.rfind("\n", 0, limit + 1)
        if boundary < limit // 2:
            boundary = limit
        chunks.append(remaining[:boundary].rstrip())
        remaining = remaining[boundary:].lstrip("\n")
    if remaining or not chunks:
        chunks.append(remaining)
    return chunks


def short_id(value: str) -> str:
    return value.split("-", 1)[0][:8]


def workspace_label(workspace: str) -> str:
    path = Path(workspace)
    if path.name == "general-workspace" and path.parent.name == ".agent-relay":
        return "无项目"
    return redact_text(path.name or str(path), limit=80)


def _current_activity(events: list[RunEvent]) -> str | None:
    for event in reversed(events):
        if event.kind == EventKind.AGENT_STATUS:
            return redact_text(str(event.payload.get("message") or "Agent 正在分析"), limit=240)
        if event.kind == EventKind.TOOL_STARTED:
            name = redact_text(str(event.payload.get("name") or event.payload.get("tool") or "tool"), limit=60)
            summary = redact_text(
                str(event.payload.get("summary") or event.payload.get("command") or ""),
                limit=160,
            )
            return f"{name} {summary}".rstrip()
        if event.kind == EventKind.TOOL_COMPLETED:
            name = redact_text(str(event.payload.get("name") or event.payload.get("tool") or "tool"), limit=60)
            return f"{name} 已完成"
    return None


def _output_tail(events: list[RunEvent], limit: int) -> str:
    output = "".join(str(event.payload.get("text") or "") for event in events if event.kind == EventKind.OUTPUT_DELTA)
    output = redact_text(output)
    return ("…" + output[-limit:]) if len(output) > limit else output
