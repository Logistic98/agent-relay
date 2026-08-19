from domain.models import (
    AgentKind,
    Approval,
    ApprovalStatus,
    Conversation,
    EventKind,
    Run,
    RunEvent,
    RunMode,
    RunStatus,
)
from presentation.renderer import (
    agent_keyboard,
    approval_keyboard,
    render_run_card,
    split_message,
    stop_keyboard,
)


def _conversation() -> Conversation:
    return Conversation("c1", "telegram", "1:2", "/tmp/project", AgentKind.CODEX, "demo", 1, 1)


def _run(status: RunStatus) -> Run:
    return Run("run-123456", "c1", AgentKind.CODEX, RunMode.RUN, status, "prompt", "2", plan="edit x")


def test_render_waiting_approval_explains_read_only_boundary() -> None:
    approval = Approval("approval-1", "run-123456", ApprovalStatus.PENDING, 1, 100)
    text = render_run_card(_conversation(), _run(RunStatus.AWAITING_APPROVAL), [], approval)
    assert "尚未修改文件" in text
    assert "准备这样做" in text
    assert "edit x" in text


def test_renderer_never_displays_unrecognized_reasoning_payload() -> None:
    event = RunEvent(1, "run-123456", 1, EventKind.AGENT_STATUS, {"raw_reasoning": "private chain"}, 1)
    text = render_run_card(_conversation(), _run(RunStatus.PLANNING), [event])
    assert "private chain" not in text
    assert "Agent 正在分析" in text


def test_renderer_shows_tool_and_redacts_output() -> None:
    events = [
        RunEvent(1, "run-123456", 1, EventKind.TOOL_STARTED, {"name": "Bash", "summary": "pytest"}, 1),
        RunEvent(2, "run-123456", 2, EventKind.OUTPUT_DELTA, {"text": "token=top-secret"}, 2),
    ]
    text = render_run_card(_conversation(), _run(RunStatus.RUNNING), events)
    assert "Bash pytest" in text
    assert "top-secret" not in text


def test_approval_keyboard_callback_fits_telegram_limit() -> None:
    keyboard = approval_keyboard("00000000-0000-0000-0000-000000000000")
    buttons = keyboard["inline_keyboard"][0]  # type: ignore[index]
    assert all(len(button["callback_data"].encode()) <= 64 for button in buttons)


def test_agent_and_stop_callbacks_fit_telegram_limit() -> None:
    agent_rows = agent_keyboard()["inline_keyboard"]  # type: ignore[index]
    stop_rows = stop_keyboard("00000000-0000-0000-0000-000000000000")["inline_keyboard"]  # type: ignore[index]
    buttons = [button for row in [*agent_rows, *stop_rows] for button in row]
    assert all(len(button["callback_data"].encode()) <= 64 for button in buttons)


def test_completed_card_hides_stale_activity_and_full_workspace() -> None:
    run = _run(RunStatus.COMPLETED)
    run.result = "回答完成"
    event = RunEvent(1, run.id, 1, EventKind.AGENT_STATUS, {"message": "Agent 正在分析"}, 1)
    text = render_run_card(_conversation(), run, [event])
    assert "Agent 正在分析" not in text
    assert "/tmp/project" not in text
    assert "Codex · project" in text
    assert "回答完成" in text


def test_auto_route_looks_like_a_conversation_not_a_task_card() -> None:
    run = _run(RunStatus.PLANNING)
    run.auto_route = True
    partial_json = RunEvent(1, run.id, 1, EventKind.OUTPUT_DELTA, {"text": '{"kind":"answer"'}, 1)
    assert render_run_card(_conversation(), run, [partial_json]) == "Codex 正在处理…"

    run.status = RunStatus.COMPLETED
    run.result = "你好，有什么可以帮你？"
    assert render_run_card(_conversation(), run, [partial_json]) == "你好，有什么可以帮你？"


def test_split_message_preserves_content() -> None:
    original = "a" * 90 + "\n" + "b" * 90 + "\n" + "c" * 90
    chunks = split_message(original, limit=100)
    assert len(chunks) == 3
    assert "".join(chunks).replace("\n", "") == original.replace("\n", "")
