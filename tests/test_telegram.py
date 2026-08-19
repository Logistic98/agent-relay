from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from core.config import Settings
from domain.models import AgentKind, Conversation, Run, RunMode, RunStatus
from transports.telegram import HELP_TEXT, TelegramBot, parse_command


class FakeAPI:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, dict[str, Any] | None]] = []
        self.callbacks: list[tuple[str, str | None]] = []

    async def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> int:
        self.sent.append((chat_id, text, reply_markup))
        return len(self.sent)

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        self.callbacks.append((callback_id, text))

    async def edit_message(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
    ) -> None:
        del message_id
        self.sent.append((chat_id, text, reply_markup))


class FakeRelay:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.created: list[tuple[str, AgentKind]] = []
        self.approved: list[tuple[str, str]] = []
        self.active: Conversation | None = None
        self.submitted: list[tuple[str, RunMode, bool]] = []
        self.run: Run | None = None
        self.conversations: list[Conversation] = []

    async def create_conversation(
        self,
        *,
        owner_type: str,
        owner_id: str,
        workspace: str | Path | None,
        agent: AgentKind,
        title: str | None = None,
    ) -> Conversation:
        del title
        self.created.append((owner_id, agent))
        selected_workspace = (
            self.workspace / ".agent-relay" / "general-workspace" if workspace is None else Path(workspace)
        )
        selected_workspace.mkdir(parents=True, exist_ok=True)
        self.active = Conversation(
            f"conv-{len(self.conversations) + 1:04d}",
            owner_type,
            owner_id,
            str(selected_workspace),
            agent,
            "demo",
            1,
            1,
        )
        self.conversations.insert(0, self.active)
        return self.active

    def is_general_workspace(self, workspace: str | Path) -> bool:
        return Path(workspace) == self.workspace / ".agent-relay" / "general-workspace"

    async def list_conversations(self, owner_type: str, owner_id: str) -> list[Conversation]:
        return [item for item in self.conversations if item.owner_type == owner_type and item.owner_id == owner_id]

    async def clear_active_conversation(self, owner_type: str, owner_id: str) -> None:
        if self.active and self.active.owner_type == owner_type and self.active.owner_id == owner_id:
            self.active = None

    async def set_active_conversation(
        self,
        owner_type: str,
        owner_id: str,
        conversation_id: str,
    ) -> Conversation:
        self.active = next(
            item
            for item in self.conversations
            if item.id == conversation_id and item.owner_type == owner_type and item.owner_id == owner_id
        )
        return self.active

    async def find_owned_conversation(
        self,
        owner_type: str,
        owner_id: str,
        prefix: str,
    ) -> Conversation:
        return next(
            item
            for item in self.conversations
            if item.id.startswith(prefix) and item.owner_type == owner_type and item.owner_id == owner_id
        )

    async def get_active_conversation(self, owner_type: str, owner_id: str) -> Conversation | None:
        if self.active and self.active.owner_type == owner_type and self.active.owner_id == owner_id:
            return self.active
        return None

    async def switch_agent(
        self,
        conversation_id: str,
        agent: AgentKind,
        *,
        owner_type: str,
        owner_id: str,
    ) -> Conversation:
        assert self.active and self.active.id == conversation_id
        self.active = Conversation(
            self.active.id,
            owner_type,
            owner_id,
            self.active.workspace,
            agent,
            self.active.title,
            self.active.created_at,
            self.active.updated_at,
        )
        return self.active

    async def submit_run(
        self,
        conversation_id: str,
        prompt: str,
        mode: RunMode,
        *,
        owner_type: str,
        owner_id: str,
        initiator_id: str,
        auto_route: bool = False,
    ) -> Run:
        del owner_type, owner_id
        assert self.active and self.active.id == conversation_id
        self.submitted.append((prompt, mode, auto_route))
        self.run = Run(
            "run-1234",
            conversation_id,
            self.active.active_agent,
            mode,
            RunStatus.COMPLETED,
            prompt,
            initiator_id,
            result="回答完成",
            auto_route=auto_route,
        )
        return self.run

    async def list_events(self, run_id: str, *, after_seq: int = 0) -> list[Any]:
        del run_id, after_seq
        return []

    async def get_run(self, run_id: str) -> Run:
        assert self.run and self.run.id == run_id
        return self.run

    async def get_conversation(self, conversation_id: str) -> Conversation:
        assert self.active and self.active.id == conversation_id
        return self.active

    async def get_approval(self, run_id: str) -> None:
        del run_id

    async def get_active_run(self, conversation_id: str) -> Run | None:
        if (
            self.run
            and self.run.conversation_id == conversation_id
            and self.run.status
            not in {
                RunStatus.COMPLETED,
                RunStatus.CANCELLED,
                RunStatus.FAILED,
                RunStatus.REJECTED,
            }
        ):
            return self.run
        return None

    async def wait_for_events(self, run_id: str, after_seq: int, *, timeout: float) -> list[Any]:
        del run_id, after_seq, timeout
        return []

    async def approve_run(
        self,
        run_id: str,
        user_id: str,
        *,
        owner_type: str,
        owner_id: str,
    ) -> None:
        assert owner_type == "telegram"
        assert owner_id == "10:20"
        self.approved.append((run_id, user_id))

    async def reject_run(
        self,
        run_id: str,
        user_id: str,
        *,
        owner_type: str,
        owner_id: str,
    ) -> None:
        assert owner_type == "telegram"
        assert owner_id == "10:20"
        self.approved.append((f"rejected:{run_id}", user_id))


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        default_workspace=tmp_path,
        workspace_roots=[tmp_path],
        telegram_allowed_chat_ids=["10"],
        telegram_allowed_user_ids=["20"],
        telegram_max_updates_per_minute=2,
    )


def test_parse_command_handles_bot_suffix_and_newline() -> None:
    assert parse_command(" /RUN@RelayBot\nfix the tests ") == ("/run", "fix the tests")
    assert parse_command("plain text") == ("", "plain text")


@pytest.mark.asyncio
async def test_help_is_available_to_allowlisted_identity(tmp_path: Path) -> None:
    api = FakeAPI()
    bot = TelegramBot(_settings(tmp_path), api, FakeRelay(tmp_path))  # type: ignore[arg-type]
    await bot.handle_update({"message": {"chat": {"id": 10}, "from": {"id": 20, "is_bot": False}, "text": "/help"}})
    assert api.sent[0][0:2] == ("10", HELP_TEXT)
    assert api.sent[0][2] is not None


@pytest.mark.asyncio
async def test_unauthorized_chat_or_user_is_silently_rejected(tmp_path: Path) -> None:
    api = FakeAPI()
    bot = TelegramBot(_settings(tmp_path), api, FakeRelay(tmp_path))  # type: ignore[arg-type]
    await bot.handle_update({"message": {"chat": {"id": 999}, "from": {"id": 20, "is_bot": False}, "text": "/help"}})
    await bot.handle_update({"message": {"chat": {"id": 10}, "from": {"id": 999, "is_bot": False}, "text": "/help"}})
    assert api.sent == []


@pytest.mark.asyncio
async def test_new_parses_quoted_workspace_and_selected_agent(tmp_path: Path) -> None:
    workspace = tmp_path / "project with spaces"
    workspace.mkdir()
    api = FakeAPI()
    relay = FakeRelay(workspace)
    bot = TelegramBot(_settings(tmp_path), api, relay)  # type: ignore[arg-type]
    await bot.handle_update(
        {
            "message": {
                "chat": {"id": 10},
                "from": {"id": 20, "is_bot": False},
                "text": f'/new claude "{workspace}"',
            }
        }
    )
    assert relay.created == [("10:20", AgentKind.CLAUDE)]
    assert "Claude" in api.sent[0][1]
    assert str(workspace) not in api.sent[0][1]


@pytest.mark.asyncio
async def test_new_without_arguments_starts_general_conversation(tmp_path: Path) -> None:
    api = FakeAPI()
    relay = FakeRelay(tmp_path)
    bot = TelegramBot(_settings(tmp_path), api, relay)  # type: ignore[arg-type]
    await bot.handle_update({"message": {"chat": {"id": 10}, "from": {"id": 20, "is_bot": False}, "text": "/new"}})

    assert relay.created == [("10:20", AgentKind.CODEX)]
    assert "无项目" in api.sent[-1][1]


@pytest.mark.asyncio
async def test_agent_without_argument_shows_only_agent_choices(tmp_path: Path) -> None:
    api = FakeAPI()
    relay = FakeRelay(tmp_path)
    bot = TelegramBot(_settings(tmp_path), api, relay)  # type: ignore[arg-type]

    await bot.handle_update({"message": {"chat": {"id": 10}, "from": {"id": 20, "is_bot": False}, "text": "/agent"}})

    assert relay.created == []
    assert api.sent == [("10", "选择本次对话使用的 Agent：", api.sent[0][2])]
    assert api.sent[0][2] is not None


@pytest.mark.asyncio
async def test_plain_text_auto_creates_conversational_codex_run(tmp_path: Path) -> None:
    api = FakeAPI()
    relay = FakeRelay(tmp_path)
    bot = TelegramBot(_settings(tmp_path), api, relay)  # type: ignore[arg-type]
    await relay.create_conversation(
        owner_type="telegram",
        owner_id="10:20",
        workspace=tmp_path,
        agent=AgentKind.CODEX,
    )
    await bot.handle_update({"message": {"chat": {"id": 10}, "from": {"id": 20, "is_bot": False}, "text": "你好"}})
    watchers = list(bot._watchers)
    if watchers:
        await asyncio.gather(*watchers)

    assert relay.created == [("10:20", AgentKind.CODEX)]
    assert relay.submitted == [("你好", RunMode.RUN, True)]
    assert api.sent[-1][1] == "回答完成"


@pytest.mark.asyncio
async def test_plain_text_outside_project_starts_general_conversation(tmp_path: Path) -> None:
    api = FakeAPI()
    relay = FakeRelay(tmp_path)
    bot = TelegramBot(_settings(tmp_path), api, relay)  # type: ignore[arg-type]

    await bot.handle_update({"message": {"chat": {"id": 10}, "from": {"id": 20}, "text": "你好"}})
    watchers = list(bot._watchers)
    if watchers:
        await asyncio.gather(*watchers)

    assert relay.created == [("10:20", AgentKind.CODEX)]
    assert relay.submitted == [("你好", RunMode.RUN, True)]
    assert api.sent[-1][1] == "回答完成"


@pytest.mark.asyncio
async def test_home_can_leave_project_and_sessions_are_clickable(tmp_path: Path) -> None:
    api = FakeAPI()
    relay = FakeRelay(tmp_path)
    conversation = await relay.create_conversation(
        owner_type="telegram",
        owner_id="10:20",
        workspace=tmp_path,
        agent=AgentKind.CODEX,
    )
    bot = TelegramBot(_settings(tmp_path), api, relay)  # type: ignore[arg-type]

    await bot.handle_update({"message": {"chat": {"id": 10}, "from": {"id": 20}, "text": "/sessions"}})
    keyboard = api.sent[-1][2]
    assert keyboard is not None
    assert keyboard["inline_keyboard"][0][0]["callback_data"] == f"use:{conversation.id}"  # type: ignore[index]

    await bot.handle_update(
        {
            "callback_query": {
                "id": "leave",
                "data": "hub:leave",
                "message": {"message_id": 7, "chat": {"id": 10}},
                "from": {"id": 20},
            }
        }
    )
    assert relay.active is None
    assert "当前不在任何项目中" in api.sent[-1][1]


@pytest.mark.asyncio
async def test_agent_button_creates_default_conversation(tmp_path: Path) -> None:
    api = FakeAPI()
    relay = FakeRelay(tmp_path)
    bot = TelegramBot(_settings(tmp_path), api, relay)  # type: ignore[arg-type]
    await bot.handle_update(
        {
            "callback_query": {
                "id": "cb-agent",
                "data": "switch:claude",
                "message": {"chat": {"id": 10}},
                "from": {"id": 20},
            }
        }
    )

    assert relay.created == [("10:20", AgentKind.CLAUDE)]
    assert api.callbacks == [("cb-agent", "已选择 claude")]


@pytest.mark.asyncio
async def test_callback_approval_is_bound_to_allowlisted_user(tmp_path: Path) -> None:
    api = FakeAPI()
    relay = FakeRelay(tmp_path)
    bot = TelegramBot(_settings(tmp_path), api, relay)  # type: ignore[arg-type]
    await bot.handle_update(
        {
            "callback_query": {
                "id": "cb1",
                "data": "approve:run-1",
                "message": {"chat": {"id": 10}},
                "from": {"id": 20},
            }
        }
    )
    assert relay.approved == [("run-1", "20")]
    assert api.callbacks == [("cb1", "已批准，开始执行")]


@pytest.mark.asyncio
async def test_per_user_rate_limit_rejects_excess_updates(tmp_path: Path) -> None:
    api = FakeAPI()
    bot = TelegramBot(_settings(tmp_path), api, FakeRelay(tmp_path))  # type: ignore[arg-type]
    update = {"message": {"chat": {"id": 10}, "from": {"id": 20}, "text": "/help"}}
    await bot.handle_update(update)
    await bot.handle_update(update)
    await bot.handle_update(update)
    assert api.sent[-1][1] == "请求过于频繁，请稍后再试。"
