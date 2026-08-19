from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import shlex
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from core.config import Settings
from core.exceptions import RelayError
from core.security import redact_text
from domain.models import TERMINAL_RUN_STATUSES, AgentKind, Conversation, Run, RunMode, RunStatus
from presentation.renderer import (
    agent_keyboard,
    approval_keyboard,
    render_run_card,
    short_id,
    split_message,
    stop_keyboard,
    workspace_label,
)
from services.relay import RelayService
from transports.telegram_api import TelegramAPI, TelegramAPIError

logger = logging.getLogger(__name__)
_MAX_RESULT_MESSAGES = 20

HELP_TEXT = """无需先选项目，直接输入就能开始通用对话。

通用对话保持只读；处理代码时再选择项目。需要修改文件或执行变更命令时，我会先让你确认。

/home  返回主页
/new   开始无项目的新对话
/projects 选择项目
/sessions 查看和切换历史会话
/agent 切换 Codex 或 Claude
/stop  停止当前任务"""


class TelegramBot:
    def __init__(self, settings: Settings, api: TelegramAPI, relay: RelayService) -> None:
        self.settings = settings
        self.api = api
        self.relay = relay
        self._watchers: set[asyncio.Task[None]] = set()
        self._rate_history: dict[str, deque[float]] = defaultdict(deque)

    async def run(self, stop_event: asyncio.Event) -> None:
        with contextlib.suppress(TelegramAPIError):
            await self.api.delete_webhook()
        with contextlib.suppress(TelegramAPIError):
            await self.api.set_commands()
        if self.settings.telegram_webapp_url:
            with contextlib.suppress(TelegramAPIError):
                await self.api.set_menu_button(self.settings.telegram_webapp_url)

        offset: int | None = None
        conflict_reported = False
        logger.info("telegram polling started")
        try:
            while not stop_event.is_set():
                try:
                    updates = await self.api.get_updates(offset, self.settings.telegram_poll_timeout_seconds)
                    conflict_reported = False
                except TelegramAPIError as exc:
                    if exc.status_code == 409:
                        if not conflict_reported:
                            logger.error("telegram polling conflict: another process is using this bot token")
                            conflict_reported = True
                        await _wait_or_stop(stop_event, 30)
                    else:
                        logger.warning(
                            "telegram polling failed",
                            extra={"status_code": exc.status_code, "error_kind": exc.__class__.__name__},
                        )
                        await _wait_or_stop(stop_event, 5)
                    continue

                for update in updates:
                    update_id = update.get("update_id")
                    if not isinstance(update_id, int):
                        continue
                    offset = update_id + 1
                    if not await self.relay.claim_telegram_update(update_id):
                        continue
                    try:
                        await self.handle_update(update)
                    except Exception:
                        logger.exception("telegram update handling failed", extra={"update_id": update_id})
        finally:
            if self._watchers:
                for watcher in self._watchers:
                    watcher.cancel()
                await asyncio.gather(*self._watchers, return_exceptions=True)
            logger.info("telegram polling stopped")

    async def handle_update(self, update: dict[str, Any]) -> None:
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            await self._handle_callback(callback)
            return
        message = update.get("message")
        if isinstance(message, dict):
            await self._handle_message(message)

    async def _handle_message(self, message: dict[str, Any]) -> None:
        identity = _message_identity(message)
        if not identity:
            return
        chat_id, user_id = identity
        if not self._allowed(chat_id, user_id):
            logger.warning("rejected telegram message", extra={"chat_id": chat_id, "user_id": user_id})
            return
        if not self._consume_rate_limit(user_id):
            await self.api.send_message(chat_id, "请求过于频繁，请稍后再试。")
            return
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return

        owner_id = _owner_id(chat_id, user_id)
        command, argument = parse_command(text)
        try:
            if command in {"/start", "/home"}:
                await self._home(chat_id, owner_id)
            elif command == "/help":
                active = await self.relay.get_active_conversation("telegram", owner_id)
                await self.api.send_message(
                    chat_id,
                    HELP_TEXT,
                    reply_markup=_home_keyboard(active is not None, self.settings.telegram_webapp_url),
                )
            elif command == "/new":
                await self._new(chat_id, owner_id, argument)
            elif command == "/projects":
                await self._projects(chat_id, owner_id)
            elif command == "/sessions":
                await self._sessions(chat_id, owner_id)
            elif command == "/use":
                await self._use(chat_id, owner_id, argument)
            elif command in {"/switch", "/agent"}:
                await self._switch(chat_id, owner_id, argument)
            elif command in {"/ask", "/run"}:
                mode = RunMode.ASK if command == "/ask" else RunMode.RUN
                await self._submit(chat_id, user_id, owner_id, argument, mode)
            elif command == "/status":
                await self._status(chat_id, owner_id)
            elif command in {"/approve", "/reject"}:
                await self._decision(chat_id, user_id, owner_id, argument, command == "/approve")
            elif command == "/stop":
                await self._stop(chat_id, user_id, owner_id, argument)
            elif command.startswith("/"):
                await self.api.send_message(chat_id, "没有这个命令。直接发消息即可，或发送 /help 查看用法。")
            else:
                configured_mode = self.settings.telegram_free_text_mode
                auto_route = configured_mode == "auto"
                mode = RunMode.RUN if auto_route else RunMode(configured_mode)
                await self._submit(chat_id, user_id, owner_id, text.strip(), mode, auto_route=auto_route)
        except RelayError as exc:
            await self.api.send_message(chat_id, redact_text(str(exc), limit=1000))
        except ValueError as exc:
            await self.api.send_message(chat_id, redact_text(str(exc), limit=1000))

    async def _handle_callback(self, callback: dict[str, Any]) -> None:
        callback_id = callback.get("id")
        data = callback.get("data")
        message = callback.get("message")
        sender = callback.get("from")
        if not isinstance(callback_id, str) or not isinstance(data, str):
            return
        if not isinstance(message, dict) or not isinstance(sender, dict):
            await self.api.answer_callback(callback_id, "无效审批")
            return
        chat = message.get("chat")
        if not isinstance(chat, dict):
            return
        chat_id, user_id = str(chat.get("id")), str(sender.get("id"))
        if not self._allowed(chat_id, user_id):
            await self.api.answer_callback(callback_id, "无权操作")
            return
        action, separator, value = data.partition(":")
        if separator != ":" or not value:
            await self.api.answer_callback(callback_id, "无效审批")
            return
        owner_id = _owner_id(chat_id, user_id)
        try:
            if action == "approve":
                await self.relay.approve_run(
                    value,
                    user_id,
                    owner_type="telegram",
                    owner_id=owner_id,
                )
                await self.api.answer_callback(callback_id, "已批准，开始执行")
            elif action == "reject":
                await self.relay.reject_run(
                    value,
                    user_id,
                    owner_type="telegram",
                    owner_id=owner_id,
                )
                await self.api.answer_callback(callback_id, "已选择不执行")
            elif action == "stop":
                await self.relay.cancel_run(
                    value,
                    user_id,
                    owner_type="telegram",
                    owner_id=owner_id,
                )
                await self.api.answer_callback(callback_id, "正在停止")
            elif action == "switch":
                conversation = await self._select_agent(owner_id, _agent_kind(value))
                message_id = message.get("message_id")
                if isinstance(message_id, int):
                    await self.api.edit_message(chat_id, message_id, _agent_selected_text(conversation))
                await self.api.answer_callback(callback_id, f"已选择 {value}")
            elif action == "hub":
                await self._handle_hub_callback(chat_id, owner_id, value, message)
                await self.api.answer_callback(callback_id)
            elif action == "project":
                await self._select_project(chat_id, owner_id, value, message)
                await self.api.answer_callback(callback_id, "已进入项目")
            elif action == "use":
                conversation = await self.relay.find_owned_conversation("telegram", owner_id, value)
                await self.relay.set_active_conversation("telegram", owner_id, conversation.id)
                await self._replace_message(chat_id, message, _conversation_selected_text(conversation))
                await self.api.answer_callback(callback_id, "已切换会话")
            else:
                await self.api.answer_callback(callback_id, "无效操作")
        except RelayError as exc:
            await self.api.answer_callback(callback_id, redact_text(str(exc), limit=180))
        except ValueError as exc:
            await self.api.answer_callback(callback_id, redact_text(str(exc), limit=180))

    async def _new(self, chat_id: str, owner_id: str, argument: str) -> None:
        parts = _split_arguments(argument)
        if not parts:
            active = await self.relay.get_active_conversation("telegram", owner_id)
            conversation = await self.relay.create_conversation(
                owner_type="telegram",
                owner_id=owner_id,
                workspace=None,
                agent=active.active_agent if active else AgentKind.CODEX,
            )
            await self.api.send_message(chat_id, _new_conversation_text(conversation))
            return
        active = await self.relay.get_active_conversation("telegram", owner_id)
        agent = _agent_kind(parts[0]) if parts else active.active_agent if active else AgentKind.CODEX
        workspace = (
            Path(" ".join(parts[1:]))
            if len(parts) >= 2
            else None
            if active and self.relay.is_general_workspace(active.workspace)
            else Path(active.workspace)
            if active
            else self.settings.default_workspace
        )
        conversation = await self.relay.create_conversation(
            owner_type="telegram",
            owner_id=owner_id,
            workspace=workspace,
            agent=agent,
        )
        await self.api.send_message(chat_id, _new_conversation_text(conversation))

    async def _home(self, chat_id: str, owner_id: str) -> None:
        active = await self.relay.get_active_conversation("telegram", owner_id)
        if active:
            text = f"主页\n当前项目：{workspace_label(active.workspace)}\nAgent：{_agent_name(active.active_agent)}"
        else:
            text = "主页\n当前不在任何项目中。"
        await self.api.send_message(
            chat_id,
            text,
            reply_markup=_home_keyboard(active is not None, self.settings.telegram_webapp_url),
        )

    async def _projects(self, chat_id: str, owner_id: str) -> None:
        projects = await self._project_candidates(owner_id)
        if not projects:
            await self.api.send_message(chat_id, "没有找到可用项目，请检查 WORKSPACE_ROOTS 配置。")
            return
        await self.api.send_message(
            chat_id,
            "选择项目并开始新对话：",
            reply_markup=_project_keyboard(projects, page=0),
        )

    async def _sessions(self, chat_id: str, owner_id: str) -> None:
        items = await self.relay.list_conversations("telegram", owner_id)
        if not items:
            await self.api.send_message(chat_id, "还没有对话，直接发消息即可开始。")
            return
        active = await self.relay.get_active_conversation("telegram", owner_id)
        await self.api.send_message(
            chat_id,
            "选择要继续的会话：",
            reply_markup=_sessions_keyboard(items, active.id if active else None, page=0),
        )

    async def _use(self, chat_id: str, owner_id: str, argument: str) -> None:
        if not argument.strip():
            raise ValueError("用法：/use 会话短ID")
        conversation = await self.relay.find_owned_conversation("telegram", owner_id, argument.strip())
        await self.relay.set_active_conversation("telegram", owner_id, conversation.id)
        await self.api.send_message(chat_id, _agent_selected_text(conversation))

    async def _switch(self, chat_id: str, owner_id: str, argument: str) -> None:
        if not argument.strip():
            await self.api.send_message(chat_id, "选择本次对话使用的 Agent：", reply_markup=agent_keyboard())
            return
        conversation = await self._select_agent(owner_id, _agent_kind(argument.strip()))
        await self.api.send_message(chat_id, _agent_selected_text(conversation))

    async def _select_agent(self, owner_id: str, agent: AgentKind) -> Conversation:
        conversation = await self.relay.get_active_conversation("telegram", owner_id)
        if conversation is None:
            conversation = await self.relay.create_conversation(
                owner_type="telegram",
                owner_id=owner_id,
                workspace=None,
                agent=agent,
            )
        elif conversation.active_agent != agent:
            conversation = await self.relay.switch_agent(
                conversation.id,
                agent,
                owner_type="telegram",
                owner_id=owner_id,
            )
        return conversation

    async def _submit(
        self,
        chat_id: str,
        user_id: str,
        owner_id: str,
        prompt: str,
        mode: RunMode,
        *,
        auto_route: bool = False,
    ) -> None:
        if not prompt.strip():
            raise ValueError(f"用法：/{mode.value} 任务内容")
        conversation = await self.relay.get_active_conversation("telegram", owner_id)
        if conversation is None:
            conversation = await self.relay.create_conversation(
                owner_type="telegram",
                owner_id=owner_id,
                workspace=None,
                agent=AgentKind.CODEX,
            )
        run = await self.relay.submit_run(
            conversation.id,
            prompt,
            mode,
            owner_type="telegram",
            owner_id=owner_id,
            initiator_id=user_id,
            auto_route=auto_route,
        )
        events = await self.relay.list_events(run.id)
        message_id = await self.api.send_message(chat_id, render_run_card(conversation, run, events))
        watcher = asyncio.create_task(self._watch_run(chat_id, message_id, conversation.id, run.id))
        self._watchers.add(watcher)
        watcher.add_done_callback(self._watchers.discard)

    async def _status(self, chat_id: str, owner_id: str) -> None:
        conversation = await self.relay.get_active_conversation("telegram", owner_id)
        if conversation is None:
            await self.api.send_message(chat_id, "当前没有正在执行的任务。直接发消息即可开始。")
            return
        run = await self.relay.get_active_run(conversation.id)
        if not run:
            await self.api.send_message(chat_id, f"{_agent_name(conversation.active_agent)} 当前空闲。")
            return
        events = await self.relay.list_events(run.id)
        approval = await self.relay.get_approval(run.id)
        keyboard = _run_keyboard(run)
        await self.api.send_message(
            chat_id,
            render_run_card(conversation, run, events, approval),
            reply_markup=keyboard,
        )

    async def _decision(
        self,
        chat_id: str,
        user_id: str,
        owner_id: str,
        argument: str,
        approve: bool,
    ) -> None:
        conversation = await self._active(owner_id)
        run = await self.relay.find_owned_run(conversation.id, user_id, argument.strip() or None)
        if approve:
            await self.relay.approve_run(
                run.id,
                user_id,
                owner_type="telegram",
                owner_id=owner_id,
            )
            text = "已批准，开始执行。"
        else:
            await self.relay.reject_run(
                run.id,
                user_id,
                owner_type="telegram",
                owner_id=owner_id,
            )
            text = "已选择不执行。"
        await self.api.send_message(chat_id, text)

    async def _stop(self, chat_id: str, user_id: str, owner_id: str, argument: str) -> None:
        conversation = await self._active(owner_id)
        run = await self.relay.find_owned_run(conversation.id, user_id, argument.strip() or None)
        await self.relay.cancel_run(
            run.id,
            user_id,
            owner_type="telegram",
            owner_id=owner_id,
        )
        await self.api.send_message(chat_id, "正在停止，请稍候。")

    async def _watch_run(self, chat_id: str, message_id: int, conversation_id: str, run_id: str) -> None:
        seq = 0
        last_text = ""
        events = []
        try:
            while True:
                run = await self.relay.get_run(run_id)
                try:
                    conversation = await self.relay.get_conversation(conversation_id)
                except RelayError:
                    return
                new_events = await self.relay.list_events(run_id, after_seq=seq)
                if new_events:
                    seq = new_events[-1].seq
                    events.extend(new_events)
                    events = events[-1000:]
                approval = await self.relay.get_approval(run_id)
                keyboard = _run_keyboard(run)
                text = render_run_card(conversation, run, events, approval)
                if text != last_text:
                    try:
                        await self.api.edit_message(chat_id, message_id, text, reply_markup=keyboard)
                    except TelegramAPIError as exc:
                        if exc.status_code != 400 or "not modified" not in exc.description.lower():
                            raise
                    last_text = text
                if run.status in TERMINAL_RUN_STATUSES:
                    if run.status == RunStatus.COMPLETED and run.result and len(run.result) > 3700:
                        chunks = split_message(redact_text(run.result), limit=3700)
                        visible_chunks = chunks[:_MAX_RESULT_MESSAGES]
                        for index, chunk in enumerate(visible_chunks, start=1):
                            await self.api.send_message(
                                chat_id,
                                f"回答 {index}/{len(visible_chunks)}\n\n{chunk}",
                            )
                        if len(chunks) > _MAX_RESULT_MESSAGES:
                            await self.api.send_message(
                                chat_id,
                                "结果超过 Telegram 安全分片上限；完整内容仍保存在本机 Relay 数据库中。",
                            )
                    return
                await self.relay.wait_for_events(run_id, seq, timeout=15)
                await asyncio.sleep(self.settings.telegram_edit_interval_seconds)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("telegram run watcher failed", extra={"run_id": run_id, "chat_id": chat_id})

    async def _active(self, owner_id: str) -> Conversation:
        conversation = await self.relay.get_active_conversation("telegram", owner_id)
        if not conversation:
            raise ValueError("还没有当前会话，直接发送消息或使用 /new 开始。")
        return conversation

    async def _handle_hub_callback(
        self,
        chat_id: str,
        owner_id: str,
        action: str,
        message: dict[str, Any],
    ) -> None:
        if action.startswith("projects"):
            page = _callback_page(action)
            projects = await self._project_candidates(owner_id)
            await self._replace_message(
                chat_id,
                message,
                "选择项目并开始新对话：",
                _project_keyboard(projects, page=page),
            )
        elif action.startswith("sessions"):
            page = _callback_page(action)
            items = await self.relay.list_conversations("telegram", owner_id)
            active = await self.relay.get_active_conversation("telegram", owner_id)
            if items:
                await self._replace_message(
                    chat_id,
                    message,
                    "选择要继续的会话：",
                    _sessions_keyboard(items, active.id if active else None, page=page),
                )
            else:
                await self._replace_message(chat_id, message, "还没有历史会话。")
        elif action == "agent":
            await self._replace_message(chat_id, message, "选择 Agent：", agent_keyboard())
        elif action == "leave":
            await self.relay.clear_active_conversation("telegram", owner_id)
            await self._replace_message(
                chat_id,
                message,
                "主页\n当前不在任何项目中。",
                _home_keyboard(False, self.settings.telegram_webapp_url),
            )
        elif action == "home":
            active = await self.relay.get_active_conversation("telegram", owner_id)
            text = (
                f"主页\n当前项目：{workspace_label(active.workspace)}\nAgent：{_agent_name(active.active_agent)}"
                if active
                else "主页\n当前不在任何项目中。"
            )
            await self._replace_message(
                chat_id,
                message,
                text,
                _home_keyboard(active is not None, self.settings.telegram_webapp_url),
            )
        elif action == "status":
            await self._status(chat_id, owner_id)
        else:
            raise ValueError("无效主页操作")

    async def _select_project(
        self,
        chat_id: str,
        owner_id: str,
        project_key: str,
        message: dict[str, Any],
    ) -> None:
        projects = await self._project_candidates(owner_id)
        project = next((item for item in projects if _project_key(item) == project_key), None)
        if project is None:
            raise ValueError("项目列表已过期，请重新打开 /new")
        conversations = await self.relay.list_conversations("telegram", owner_id)
        active = await self.relay.get_active_conversation("telegram", owner_id)
        agent = active.active_agent if active else conversations[0].active_agent if conversations else AgentKind.CODEX
        conversation = await self.relay.create_conversation(
            owner_type="telegram",
            owner_id=owner_id,
            workspace=project,
            agent=agent,
        )
        await self._replace_message(chat_id, message, _new_conversation_text(conversation))

    async def _project_candidates(self, owner_id: str) -> list[Path]:
        candidates: list[Path] = []
        seen: set[Path] = set()

        def add(path: Path) -> None:
            try:
                resolved = path.expanduser().resolve(strict=True)
            except (OSError, RuntimeError):
                return
            if resolved.is_dir() and resolved not in seen:
                seen.add(resolved)
                candidates.append(resolved)

        for conversation in await self.relay.list_conversations("telegram", owner_id):
            if not self.relay.is_general_workspace(conversation.workspace):
                add(Path(conversation.workspace))
        add(self.settings.default_workspace)
        for root in self.settings.workspace_roots:
            try:
                children = sorted(root.expanduser().resolve(strict=True).iterdir(), key=lambda item: item.name.lower())
            except (OSError, RuntimeError):
                continue
            for child in children:
                if not child.name.startswith("."):
                    add(child)
        return candidates[:80]

    async def _replace_message(
        self,
        chat_id: str,
        message: dict[str, Any],
        text: str,
        reply_markup: dict[str, object] | None = None,
    ) -> None:
        message_id = message.get("message_id")
        if isinstance(message_id, int):
            await self.api.edit_message(chat_id, message_id, text, reply_markup=reply_markup)
        else:
            await self.api.send_message(chat_id, text, reply_markup=reply_markup)

    def _allowed(self, chat_id: str, user_id: str) -> bool:
        return chat_id in set(self.settings.telegram_allowed_chat_ids) and user_id in set(
            self.settings.telegram_allowed_user_ids
        )

    def _consume_rate_limit(self, user_id: str) -> bool:
        now = time.monotonic()
        history = self._rate_history[user_id]
        while history and history[0] <= now - 60:
            history.popleft()
        if len(history) >= self.settings.telegram_max_updates_per_minute:
            return False
        history.append(now)
        return True


def parse_command(text: str) -> tuple[str, str]:
    stripped = text.strip()
    if not stripped.startswith("/"):
        return "", stripped
    parts = stripped.split(maxsplit=1)
    first = parts[0]
    command = first.split("@", 1)[0].lower()
    return command, parts[1].strip() if len(parts) > 1 else ""


def _split_arguments(value: str) -> list[str]:
    try:
        return shlex.split(value)
    except ValueError as exc:
        raise ValueError("命令参数中的引号不完整") from exc


def _message_identity(message: dict[str, Any]) -> tuple[str, str] | None:
    chat = message.get("chat")
    sender = message.get("from")
    if not isinstance(chat, dict) or not isinstance(sender, dict) or sender.get("is_bot") is True:
        return None
    chat_id = chat.get("id")
    user_id = sender.get("id")
    if chat_id is None or user_id is None:
        return None
    return str(chat_id), str(user_id)


def _owner_id(chat_id: str, user_id: str) -> str:
    return f"{chat_id}:{user_id}"


def _agent_kind(value: str) -> AgentKind:
    try:
        return AgentKind(value.strip().lower())
    except ValueError as exc:
        raise ValueError("Agent 只能是 codex 或 claude") from exc


def _agent_name(agent: AgentKind) -> str:
    return "Codex" if agent == AgentKind.CODEX else "Claude"


def _agent_selected_text(conversation: Conversation) -> str:
    return f"已切换到 {_agent_name(conversation.active_agent)}。"


def _new_conversation_text(conversation: Conversation) -> str:
    return f"新对话已开始 · {_agent_name(conversation.active_agent)} · {workspace_label(conversation.workspace)}"


def _conversation_selected_text(conversation: Conversation) -> str:
    return f"已继续会话 · {_agent_name(conversation.active_agent)} · {workspace_label(conversation.workspace)}"


def _project_key(path: Path) -> str:
    return hashlib.sha256(str(path).encode()).hexdigest()[:16]


def _home_keyboard(has_active: bool, webapp_url: str | None = None) -> dict[str, object]:
    rows: list[list[dict[str, object]]] = []
    if webapp_url:
        rows.append([{"text": "打开 Codex 工作台", "web_app": {"url": webapp_url}}])
    rows.extend(
        [
            [
                {"text": "选择项目", "callback_data": "hub:projects"},
                {"text": "会话列表", "callback_data": "hub:sessions"},
            ],
            [{"text": "切换 Agent", "callback_data": "hub:agent"}],
        ]
    )
    if has_active:
        rows.append(
            [
                {"text": "当前状态", "callback_data": "hub:status"},
                {"text": "退出当前会话", "callback_data": "hub:leave"},
            ]
        )
    return {"inline_keyboard": rows}


def _project_keyboard(projects: list[Path], *, page: int, page_size: int = 8) -> dict[str, object]:
    page_count = max(1, (len(projects) + page_size - 1) // page_size)
    page = min(max(0, page), page_count - 1)
    visible = projects[page * page_size : (page + 1) * page_size]
    rows = [
        [
            {
                "text": f"{project.name} · {project.parent.name}",
                "callback_data": f"project:{_project_key(project)}",
            }
        ]
        for project in visible
    ]
    navigation: list[dict[str, str]] = []
    if page > 0:
        navigation.append({"text": "上一页", "callback_data": f"hub:projects:{page - 1}"})
    if page + 1 < page_count:
        navigation.append({"text": "下一页", "callback_data": f"hub:projects:{page + 1}"})
    if navigation:
        rows.append(navigation)
    rows.append([{"text": "返回主页", "callback_data": "hub:home"}])
    return {"inline_keyboard": rows}


def _sessions_keyboard(
    conversations: list[Conversation],
    active_id: str | None,
    *,
    page: int,
    page_size: int = 8,
) -> dict[str, object]:
    page_count = max(1, (len(conversations) + page_size - 1) // page_size)
    page = min(max(0, page), page_count - 1)
    visible = conversations[page * page_size : (page + 1) * page_size]
    rows = []
    for conversation in visible:
        marker = "当前 · " if conversation.id == active_id else ""
        rows.append(
            [
                {
                    "text": (
                        f"{marker}{workspace_label(conversation.workspace)} · "
                        f"{_agent_name(conversation.active_agent)} · {short_id(conversation.id)}"
                    ),
                    "callback_data": f"use:{conversation.id}",
                }
            ]
        )
    navigation: list[dict[str, str]] = []
    if page > 0:
        navigation.append({"text": "上一页", "callback_data": f"hub:sessions:{page - 1}"})
    if page + 1 < page_count:
        navigation.append({"text": "下一页", "callback_data": f"hub:sessions:{page + 1}"})
    if navigation:
        rows.append(navigation)
    rows.append(
        [
            {"text": "选择新项目", "callback_data": "hub:projects"},
            {"text": "返回主页", "callback_data": "hub:home"},
        ]
    )
    return {"inline_keyboard": rows}


def _callback_page(value: str) -> int:
    _, separator, raw_page = value.partition(":")
    if not separator:
        return 0
    try:
        return max(0, int(raw_page))
    except ValueError as exc:
        raise ValueError("页码无效") from exc


def _run_keyboard(run: Run) -> dict[str, object] | None:
    if run.status == RunStatus.AWAITING_APPROVAL:
        return approval_keyboard(run.id)
    if run.status in TERMINAL_RUN_STATUSES or run.status == RunStatus.CANCEL_REQUESTED:
        return None
    return stop_keyboard(run.id)


async def _wait_or_stop(stop_event: asyncio.Event, timeout: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop_event.wait(), timeout=timeout)
