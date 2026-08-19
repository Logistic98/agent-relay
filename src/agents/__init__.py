from agents.base import AgentAdapter, EventCallback
from agents.claude import ClaudeAdapter, ClaudeEventParser
from agents.codex import CodexAdapter, CodexEventParser
from agents.registry import AgentRegistry
from core.config import Settings
from core.preflight import resolve_safe_executable_path
from runtime.process import ProcessRunner


def create_agent_registry(settings: Settings, runner: ProcessRunner | None = None) -> AgentRegistry:
    runtime_settings = settings.model_copy(
        update={
            "codex_executable": str(resolve_safe_executable_path(settings.codex_executable)),
            "claude_executable": str(resolve_safe_executable_path(settings.claude_executable)),
        }
    )
    process_runner = runner or ProcessRunner(
        output_limit_bytes=runtime_settings.subprocess_output_limit_bytes,
        event_line_limit=runtime_settings.subprocess_event_line_limit,
        interrupt_grace_seconds=runtime_settings.process_interrupt_grace_seconds,
        terminate_grace_seconds=runtime_settings.process_terminate_grace_seconds,
    )
    return AgentRegistry(
        [
            CodexAdapter(runtime_settings, process_runner),
            ClaudeAdapter(runtime_settings, process_runner),
        ]
    )


__all__ = [
    "AgentAdapter",
    "AgentRegistry",
    "ClaudeAdapter",
    "ClaudeEventParser",
    "CodexAdapter",
    "CodexEventParser",
    "EventCallback",
    "create_agent_registry",
]
