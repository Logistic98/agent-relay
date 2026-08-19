from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
from collections.abc import Collection, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from core.exceptions import (
    ApprovalExpiredError,
    ConversationNotFoundError,
    InvalidStateError,
    RunConflictError,
    RunNotFoundError,
)
from domain.models import (
    ACTIVE_RUN_STATUSES,
    PROCESSING_RUN_STATUSES,
    TERMINAL_RUN_STATUSES,
    AgentEvent,
    AgentKind,
    Approval,
    ApprovalStatus,
    Conversation,
    EventKind,
    PermissionMode,
    Run,
    RunEvent,
    RunMode,
    RunStatus,
)

_UNSET = object()
_DEFAULT_BUSY_TIMEOUT_MS = 5_000
_DEFAULT_LIST_LIMIT = 100
_MAX_LIST_LIMIT = 1_000
_LEASE_STATUS_VALUES = tuple(status.value for status in PROCESSING_RUN_STATUSES)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    owner_type TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    workspace TEXT NOT NULL,
    active_agent TEXT NOT NULL CHECK (active_agent IN ('codex', 'claude')),
    title TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_conversations_owner_updated
    ON conversations(owner_type, owner_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS agent_sessions (
    conversation_id TEXT NOT NULL,
    agent TEXT NOT NULL CHECK (agent IN ('codex', 'claude')),
    native_session_id TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (conversation_id, agent),
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    agent TEXT NOT NULL CHECK (agent IN ('codex', 'claude')),
    mode TEXT NOT NULL CHECK (mode IN ('ask', 'run')),
    status TEXT NOT NULL CHECK (
        status IN (
            'queued', 'planning', 'awaiting_approval', 'running', 'cancel_requested',
            'cancelled', 'completed', 'failed', 'rejected', 'interrupted', 'timed_out'
        )
    ),
    prompt TEXT NOT NULL,
    initiator_id TEXT NOT NULL,
    permission_mode TEXT NOT NULL DEFAULT 'request_approval' CHECK (
        permission_mode IN ('request_approval', 'workspace_auto', 'full_access')
    ),
    model TEXT,
    reasoning_effort TEXT,
    auto_route INTEGER NOT NULL DEFAULT 0 CHECK (auto_route IN (0, 1)),
    native_session_id TEXT,
    plan TEXT,
    result TEXT,
    error TEXT,
    exit_code INTEGER,
    created_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_runs_conversation_created
    ON runs(conversation_id, created_at DESC);

CREATE UNIQUE INDEX IF NOT EXISTS uq_runs_one_active_per_conversation
    ON runs(conversation_id)
    WHERE status IN ('planning', 'awaiting_approval', 'running', 'cancel_requested');

CREATE TABLE IF NOT EXISTS workspace_leases (
    workspace TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    acquired_at REAL NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    seq INTEGER NOT NULL CHECK (seq > 0),
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE (run_id, seq),
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_events_run_seq ON events(run_id, seq);

CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL CHECK (status IN ('pending', 'approved', 'rejected', 'expired')),
    requested_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    decided_at REAL,
    decided_by TEXT,
    FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_approvals_pending_expiry
    ON approvals(status, expires_at);

CREATE TABLE IF NOT EXISTS telegram_update_claims (
    bot_key TEXT NOT NULL,
    update_id INTEGER NOT NULL,
    claimed_at REAL NOT NULL,
    PRIMARY KEY (bot_key, update_id)
);

CREATE TABLE IF NOT EXISTS active_conversations (
    owner_type TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (owner_type, owner_id),
    FOREIGN KEY (conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
);
"""


def _workspaces_overlap(first: str, second: str) -> bool:
    first_path = Path(first)
    second_path = Path(second)
    return first_path == second_path or first_path in second_path.parents or second_path in first_path.parents


async def _rebuild_workspace_leases(connection: aiosqlite.Connection) -> None:
    placeholders = ", ".join("?" for _ in _LEASE_STATUS_VALUES)
    await connection.execute("BEGIN IMMEDIATE")
    try:
        async with connection.execute(
            f"""
            SELECT runs.id AS run_id, conversations.workspace AS workspace, runs.created_at AS acquired_at
            FROM runs
            JOIN conversations ON conversations.id = runs.conversation_id
            WHERE runs.status IN ({placeholders})
            ORDER BY runs.created_at, runs.id
            """,
            _LEASE_STATUS_VALUES,
        ) as cursor:
            active_rows = await cursor.fetchall()

        leases: list[tuple[str, str, float]] = []
        for row in active_rows:
            workspace = str(row["workspace"])
            conflicting = next((item for item in leases if _workspaces_overlap(workspace, item[0])), None)
            if conflicting is not None:
                raise InvalidStateError(
                    "Existing active runs have overlapping workspaces: "
                    f"{workspace!r} conflicts with {conflicting[0]!r}; resolve them before startup"
                )
            leases.append((workspace, str(row["run_id"]), float(row["acquired_at"])))

        await connection.execute("DELETE FROM workspace_leases")
        if leases:
            await connection.executemany(
                "INSERT INTO workspace_leases (workspace, run_id, acquired_at) VALUES (?, ?, ?)",
                leases,
            )
    except BaseException:
        await connection.rollback()
        raise
    else:
        await connection.commit()


async def _migrate_runs_auto_route(connection: aiosqlite.Connection) -> None:
    async with connection.execute("PRAGMA table_info(runs)") as cursor:
        columns = {str(row["name"]) for row in await cursor.fetchall()}
    if "auto_route" in columns:
        return
    await connection.execute("BEGIN IMMEDIATE")
    try:
        await connection.execute(
            "ALTER TABLE runs ADD COLUMN auto_route INTEGER NOT NULL DEFAULT 0 CHECK (auto_route IN (0, 1))"
        )
    except BaseException:
        await connection.rollback()
        raise
    else:
        await connection.commit()


async def _migrate_runs_permission_mode(connection: aiosqlite.Connection) -> None:
    async with connection.execute("PRAGMA table_info(runs)") as cursor:
        columns = {str(row["name"]) for row in await cursor.fetchall()}
    if "permission_mode" in columns:
        return
    await connection.execute("BEGIN IMMEDIATE")
    try:
        await connection.execute(
            "ALTER TABLE runs ADD COLUMN permission_mode TEXT NOT NULL DEFAULT 'request_approval' "
            "CHECK (permission_mode IN ('request_approval', 'workspace_auto', 'full_access'))"
        )
    except BaseException:
        await connection.rollback()
        raise
    else:
        await connection.commit()


async def _migrate_runs_model_settings(connection: aiosqlite.Connection) -> None:
    async with connection.execute("PRAGMA table_info(runs)") as cursor:
        columns = {str(row["name"]) for row in await cursor.fetchall()}
    missing = [column for column in ("model", "reasoning_effort") if column not in columns]
    if not missing:
        return
    await connection.execute("BEGIN IMMEDIATE")
    try:
        for column in missing:
            await connection.execute(f"ALTER TABLE runs ADD COLUMN {column} TEXT")
    except BaseException:
        await connection.rollback()
        raise
    else:
        await connection.commit()


async def _migrate_active_run_index(connection: aiosqlite.Connection) -> None:
    await connection.execute("BEGIN IMMEDIATE")
    try:
        await connection.execute("DROP INDEX IF EXISTS uq_runs_one_active_per_conversation")
        await connection.execute(
            "CREATE UNIQUE INDEX uq_runs_one_active_per_conversation ON runs(conversation_id) "
            "WHERE status IN ('planning', 'awaiting_approval', 'running', 'cancel_requested')"
        )
    except BaseException:
        await connection.rollback()
        raise
    else:
        await connection.commit()


class Database:
    """Async SQLite store with serialized transactions and durable invariants."""

    def __init__(self, path: str | os.PathLike[str], *, busy_timeout_ms: int = _DEFAULT_BUSY_TIMEOUT_MS) -> None:
        if busy_timeout_ms < 0:
            raise ValueError("busy_timeout_ms must not be negative")
        self.path = Path(path).expanduser()
        self.busy_timeout_ms = busy_timeout_ms
        self._connection: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._memory = os.fspath(path) == ":memory:"

    async def initialize(self) -> None:
        """Create the protected database and initialize its schema."""
        async with self._lock:
            if self._connection is not None:
                return

            if not self._memory:
                parent = self.path.parent
                if parent != Path("."):
                    parent_existed = parent.exists()
                    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    if not parent_existed:
                        parent.chmod(0o700)

            connection = await aiosqlite.connect(self.path if not self._memory else ":memory:", isolation_level=None)
            try:
                connection.row_factory = sqlite3.Row
                await connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
                await connection.execute("PRAGMA foreign_keys = ON")
                await connection.execute("PRAGMA journal_mode = WAL")
                await connection.execute("PRAGMA synchronous = NORMAL")
                await connection.executescript(_SCHEMA)
                await _migrate_runs_auto_route(connection)
                await _migrate_runs_permission_mode(connection)
                await _migrate_runs_model_settings(connection)
                await _migrate_active_run_index(connection)
                await _rebuild_workspace_leases(connection)
            except BaseException:
                await connection.close()
                raise

            self._connection = connection
            if not self._memory:
                self.path.chmod(0o600)

    async def close(self) -> None:
        async with self._lock:
            if self._connection is None:
                return
            connection = self._connection
            self._connection = None
            await connection.close()

    async def __aenter__(self) -> Database:
        await self.initialize()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()

    @property
    def connection(self) -> aiosqlite.Connection:
        if self._connection is None:
            raise RuntimeError("Database has not been initialized")
        return self._connection

    @asynccontextmanager
    async def _transaction(self):
        connection = self.connection
        await connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            await connection.rollback()
            raise
        else:
            await connection.commit()

    async def _fetchone_unlocked(self, sql: str, parameters: Sequence[Any] = ()) -> sqlite3.Row | None:
        async with self.connection.execute(sql, parameters) as cursor:
            return await cursor.fetchone()

    async def _fetchall_unlocked(self, sql: str, parameters: Sequence[Any] = ()) -> list[sqlite3.Row]:
        async with self.connection.execute(sql, parameters) as cursor:
            return await cursor.fetchall()

    async def ping(self) -> bool:
        async with self._lock:
            row = await self._fetchone_unlocked("SELECT 1 AS healthy")
            return bool(row and row["healthy"] == 1)

    async def create_conversation(self, conversation: Conversation) -> Conversation:
        created_at = conversation.created_at or time.time()
        updated_at = conversation.updated_at or created_at
        async with self._lock, self._transaction():
            try:
                await self.connection.execute(
                    """
                    INSERT INTO conversations (
                        id, owner_type, owner_id, workspace, active_agent, title, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        conversation.id,
                        conversation.owner_type,
                        conversation.owner_id,
                        conversation.workspace,
                        conversation.active_agent.value,
                        conversation.title,
                        created_at,
                        updated_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise InvalidStateError(f"Conversation {conversation.id!r} already exists or is invalid") from exc
            row = await self._fetchone_unlocked("SELECT * FROM conversations WHERE id = ?", (conversation.id,))
        assert row is not None
        return _conversation_from_row(row)

    async def get_conversation(self, conversation_id: str) -> Conversation | None:
        async with self._lock:
            row = await self._fetchone_unlocked("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        return _conversation_from_row(row) if row is not None else None

    async def list_conversations(
        self,
        *,
        owner_type: str | None = None,
        owner_id: str | None = None,
        limit: int = _DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> list[Conversation]:
        limit = _validate_limit(limit)
        if offset < 0:
            raise ValueError("offset must not be negative")
        clauses: list[str] = []
        parameters: list[Any] = []
        if owner_type is not None:
            clauses.append("owner_type = ?")
            parameters.append(owner_type)
        if owner_id is not None:
            clauses.append("owner_id = ?")
            parameters.append(owner_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.extend((limit, offset))
        async with self._lock:
            rows = await self._fetchall_unlocked(
                f"SELECT * FROM conversations {where} ORDER BY updated_at DESC, id LIMIT ? OFFSET ?",
                parameters,
            )
        return [_conversation_from_row(row) for row in rows]

    async def update_conversation_agent(
        self,
        conversation_id: str,
        agent: AgentKind,
        *,
        updated_at: float | None = None,
    ) -> Conversation:
        changed_at = time.time() if updated_at is None else updated_at
        async with self._lock, self._transaction():
            cursor = await self.connection.execute(
                "UPDATE conversations SET active_agent = ?, updated_at = ? WHERE id = ?",
                (AgentKind(agent).value, changed_at, conversation_id),
            )
            if cursor.rowcount != 1:
                raise ConversationNotFoundError(f"Conversation {conversation_id!r} was not found")
            row = await self._fetchone_unlocked("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        assert row is not None
        return _conversation_from_row(row)

    async def switch_conversation_agent_if_idle(
        self,
        conversation_id: str,
        agent: AgentKind,
        *,
        owner_type: str,
        owner_id: str,
        updated_at: float | None = None,
    ) -> Conversation:
        changed_at = time.time() if updated_at is None else updated_at
        async with self._lock, self._transaction():
            row = await self._fetchone_unlocked("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
            if row is None or row["owner_type"] != owner_type or row["owner_id"] != owner_id:
                raise ConversationNotFoundError(f"Conversation {conversation_id!r} was not found")
            active = await self._fetchone_unlocked(
                """
                SELECT id FROM runs
                WHERE conversation_id = ?
                  AND status IN ('queued', 'planning', 'awaiting_approval', 'running', 'cancel_requested')
                LIMIT 1
                """,
                (conversation_id,),
            )
            if active is not None:
                raise RunConflictError("The conversation has an active run")
            await self.connection.execute(
                "UPDATE conversations SET active_agent = ?, updated_at = ? WHERE id = ?",
                (AgentKind(agent).value, changed_at, conversation_id),
            )
            updated = await self._fetchone_unlocked("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
        assert updated is not None
        return _conversation_from_row(updated)

    async def get_active_conversation_id(self, owner_type: str, owner_id: str) -> str | None:
        async with self._lock:
            row = await self._fetchone_unlocked(
                "SELECT conversation_id FROM active_conversations WHERE owner_type = ? AND owner_id = ?",
                (owner_type, owner_id),
            )
        return str(row["conversation_id"]) if row is not None else None

    async def get_active_conversation(self, owner_type: str, owner_id: str) -> Conversation | None:
        async with self._lock:
            row = await self._fetchone_unlocked(
                """
                SELECT conversations.*
                FROM active_conversations
                JOIN conversations ON conversations.id = active_conversations.conversation_id
                WHERE active_conversations.owner_type = ? AND active_conversations.owner_id = ?
                """,
                (owner_type, owner_id),
            )
        return _conversation_from_row(row) if row is not None else None

    async def set_active_conversation(
        self,
        owner_type: str,
        owner_id: str,
        conversation_id: str | None,
        *,
        updated_at: float | None = None,
    ) -> Conversation | None:
        changed_at = time.time() if updated_at is None else updated_at
        async with self._lock, self._transaction():
            if conversation_id is None:
                await self.connection.execute(
                    "DELETE FROM active_conversations WHERE owner_type = ? AND owner_id = ?",
                    (owner_type, owner_id),
                )
                return None

            row = await self._fetchone_unlocked("SELECT * FROM conversations WHERE id = ?", (conversation_id,))
            if row is None:
                raise ConversationNotFoundError(f"Conversation {conversation_id!r} was not found")
            if row["owner_type"] != owner_type or row["owner_id"] != owner_id:
                raise InvalidStateError("Active conversation must belong to the requested owner")
            await self.connection.execute(
                """
                INSERT INTO active_conversations (owner_type, owner_id, conversation_id, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(owner_type, owner_id) DO UPDATE SET
                    conversation_id = excluded.conversation_id,
                    updated_at = excluded.updated_at
                """,
                (owner_type, owner_id, conversation_id, changed_at),
            )
        return _conversation_from_row(row)

    async def get_native_session(self, conversation_id: str, agent: AgentKind) -> str | None:
        async with self._lock:
            row = await self._fetchone_unlocked(
                "SELECT native_session_id FROM agent_sessions WHERE conversation_id = ? AND agent = ?",
                (conversation_id, AgentKind(agent).value),
            )
        return str(row["native_session_id"]) if row is not None else None

    async def set_native_session(
        self,
        conversation_id: str,
        agent: AgentKind,
        native_session_id: str | None,
        *,
        updated_at: float | None = None,
    ) -> None:
        changed_at = time.time() if updated_at is None else updated_at
        agent_value = AgentKind(agent).value
        async with self._lock, self._transaction():
            conversation = await self._fetchone_unlocked(
                "SELECT id FROM conversations WHERE id = ?",
                (conversation_id,),
            )
            if conversation is None:
                raise ConversationNotFoundError(f"Conversation {conversation_id!r} was not found")
            if native_session_id is None:
                await self.connection.execute(
                    "DELETE FROM agent_sessions WHERE conversation_id = ? AND agent = ?",
                    (conversation_id, agent_value),
                )
            else:
                await self.connection.execute(
                    """
                    INSERT INTO agent_sessions (conversation_id, agent, native_session_id, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(conversation_id, agent) DO UPDATE SET
                        native_session_id = excluded.native_session_id,
                        updated_at = excluded.updated_at
                    """,
                    (conversation_id, agent_value, native_session_id, changed_at),
                )

    async def create_run(self, run: Run) -> Run:
        created_at = run.created_at or time.time()
        async with self._lock, self._transaction():
            conversation = await self._fetchone_unlocked(
                "SELECT id, workspace, active_agent FROM conversations WHERE id = ?",
                (run.conversation_id,),
            )
            if conversation is None:
                raise ConversationNotFoundError(f"Conversation {run.conversation_id!r} was not found")
            if run.status in ACTIVE_RUN_STATUSES and AgentKind(conversation["active_agent"]) is not run.agent:
                raise RunConflictError("Conversation agent changed before the run was created; retry the request")
            try:
                await self.connection.execute(
                    """
                    INSERT INTO runs (
                        id, conversation_id, agent, mode, status, prompt, initiator_id, permission_mode, auto_route,
                        model, reasoning_effort, native_session_id, plan, result, error, exit_code, created_at,
                        started_at, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run.id,
                        run.conversation_id,
                        run.agent.value,
                        run.mode.value,
                        run.status.value,
                        run.prompt,
                        run.initiator_id,
                        run.permission_mode.value,
                        int(run.auto_route),
                        run.model,
                        run.reasoning_effort,
                        run.native_session_id,
                        run.plan,
                        run.result,
                        run.error,
                        run.exit_code,
                        created_at,
                        run.started_at,
                        run.completed_at,
                    ),
                )
                if run.status in PROCESSING_RUN_STATUSES:
                    await self.connection.execute(
                        "INSERT INTO workspace_leases (workspace, run_id, acquired_at) VALUES (?, ?, ?)",
                        (conversation["workspace"], run.id, created_at),
                    )
            except sqlite3.IntegrityError as exc:
                if run.status in PROCESSING_RUN_STATUSES:
                    raise RunConflictError(
                        f"Conversation {run.conversation_id!r} or workspace {conversation['workspace']!r} "
                        "already has an active run"
                    ) from exc
                raise InvalidStateError(f"Run {run.id!r} already exists or is invalid") from exc
            row = await self._fetchone_unlocked("SELECT * FROM runs WHERE id = ?", (run.id,))
        assert row is not None
        return _run_from_row(row)

    async def claim_next_queued_run(self, *, changed_at: float | None = None) -> Run | None:
        now = time.time() if changed_at is None else changed_at
        async with self._lock, self._transaction():
            queued = await self._fetchall_unlocked(
                """
                SELECT runs.*, conversations.workspace
                FROM runs
                JOIN conversations ON conversations.id = runs.conversation_id
                WHERE runs.status = 'queued'
                ORDER BY runs.created_at, runs.id
                """
            )
            leases = await self._fetchall_unlocked("SELECT workspace FROM workspace_leases")
            for row in queued:
                processing = await self._fetchone_unlocked(
                    """
                    SELECT id FROM runs
                    WHERE conversation_id = ?
                      AND status IN ('planning', 'awaiting_approval', 'running', 'cancel_requested')
                    LIMIT 1
                    """,
                    (row["conversation_id"],),
                )
                if processing is not None:
                    continue
                workspace = str(row["workspace"])
                if any(_workspaces_overlap(workspace, str(lease["workspace"])) for lease in leases):
                    continue
                cursor = await self.connection.execute(
                    "UPDATE runs SET status = ?, started_at = COALESCE(started_at, ?) WHERE id = ? AND status = ?",
                    (RunStatus.PLANNING.value, now, row["id"], RunStatus.QUEUED.value),
                )
                if cursor.rowcount != 1:
                    continue
                await self.connection.execute(
                    "INSERT INTO workspace_leases (workspace, run_id, acquired_at) VALUES (?, ?, ?)",
                    (workspace, row["id"], now),
                )
                claimed = await self._fetchone_unlocked("SELECT * FROM runs WHERE id = ?", (row["id"],))
                assert claimed is not None
                return _run_from_row(claimed)
            return None

    async def get_run(self, run_id: str) -> Run | None:
        async with self._lock:
            row = await self._fetchone_unlocked("SELECT * FROM runs WHERE id = ?", (run_id,))
        return _run_from_row(row) if row is not None else None

    async def list_runs(
        self,
        conversation_id: str,
        *,
        limit: int = _DEFAULT_LIST_LIMIT,
        offset: int = 0,
    ) -> list[Run]:
        limit = _validate_limit(limit)
        if offset < 0:
            raise ValueError("offset must not be negative")
        async with self._lock:
            rows = await self._fetchall_unlocked(
                """
                SELECT * FROM runs
                WHERE conversation_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                (conversation_id, limit, offset),
            )
        return [_run_from_row(row) for row in rows]

    async def update_run(
        self,
        run_id: str,
        *,
        plan: str | None | object = _UNSET,
        result: str | None | object = _UNSET,
        error: str | None | object = _UNSET,
        native_session_id: str | None | object = _UNSET,
        exit_code: int | None | object = _UNSET,
        started_at: float | None | object = _UNSET,
        completed_at: float | None | object = _UNSET,
    ) -> Run:
        values = {
            "plan": plan,
            "result": result,
            "error": error,
            "native_session_id": native_session_id,
            "exit_code": exit_code,
            "started_at": started_at,
            "completed_at": completed_at,
        }
        changes = [(column, value) for column, value in values.items() if value is not _UNSET]
        if not changes:
            run = await self.get_run(run_id)
            if run is None:
                raise RunNotFoundError(f"Run {run_id!r} was not found")
            return run

        assignments = ", ".join(f"{column} = ?" for column, _ in changes)
        parameters = [value for _, value in changes]
        parameters.append(run_id)
        async with self._lock, self._transaction():
            cursor = await self.connection.execute(
                f"UPDATE runs SET {assignments} WHERE id = ?",
                parameters,
            )
            if cursor.rowcount != 1:
                raise RunNotFoundError(f"Run {run_id!r} was not found")
            row = await self._fetchone_unlocked("SELECT * FROM runs WHERE id = ?", (run_id,))
        assert row is not None
        return _run_from_row(row)

    async def transition_run_status(
        self,
        run_id: str,
        new_status: RunStatus,
        *,
        expected_statuses: Collection[RunStatus] | None = None,
        changed_at: float | None = None,
        result: str | None | object = _UNSET,
        error: str | None | object = _UNSET,
        exit_code: int | None | object = _UNSET,
        plan: str | None | object = _UNSET,
    ) -> Run | None:
        target = RunStatus(new_status)
        expected = {RunStatus(status) for status in expected_statuses} if expected_statuses is not None else None
        now = time.time() if changed_at is None else changed_at

        async with self._lock, self._transaction():
            row = await self._fetchone_unlocked("SELECT * FROM runs WHERE id = ?", (run_id,))
            if row is None:
                raise RunNotFoundError(f"Run {run_id!r} was not found")
            current = RunStatus(row["status"])
            if expected is not None and current not in expected:
                return None
            if current == target:
                return _run_from_row(row)
            if not _can_transition(current, target):
                raise InvalidStateError(f"Run {run_id!r} cannot transition from {current.value!r} to {target.value!r}")

            assignments: list[str] = ["status = ?"]
            parameters: list[Any] = [target.value]
            if target in {RunStatus.PLANNING, RunStatus.RUNNING} and row["started_at"] is None:
                assignments.append("started_at = ?")
                parameters.append(now)
            if target in TERMINAL_RUN_STATUSES:
                assignments.append("completed_at = ?")
                parameters.append(now)
            for column, value in (("result", result), ("error", error), ("exit_code", exit_code), ("plan", plan)):
                if value is not _UNSET:
                    assignments.append(f"{column} = ?")
                    parameters.append(value)
            parameters.extend((run_id, current.value))
            try:
                cursor = await self.connection.execute(
                    f"UPDATE runs SET {', '.join(assignments)} WHERE id = ? AND status = ?",
                    parameters,
                )
            except sqlite3.IntegrityError as exc:
                raise RunConflictError(f"Conversation {row['conversation_id']!r} already has an active run") from exc
            if cursor.rowcount != 1:
                return None
            if target in TERMINAL_RUN_STATUSES:
                await self.connection.execute("DELETE FROM workspace_leases WHERE run_id = ?", (run_id,))
            updated = await self._fetchone_unlocked("SELECT * FROM runs WHERE id = ?", (run_id,))
        assert updated is not None
        return _run_from_row(updated)

    async def find_active_run(self, conversation_id: str) -> Run | None:
        async with self._lock:
            row = await self._fetchone_unlocked(
                """
                SELECT * FROM runs
                WHERE conversation_id = ?
                  AND status IN ('queued', 'planning', 'awaiting_approval', 'running', 'cancel_requested')
                ORDER BY CASE WHEN status = 'queued' THEN 1 ELSE 0 END, created_at, id
                LIMIT 1
                """,
                (conversation_id,),
            )
        return _run_from_row(row) if row is not None else None

    async def recover_active_runs(
        self,
        *,
        changed_at: float | None = None,
        conversation_id: str | None = None,
    ) -> list[Run]:
        now = time.time() if changed_at is None else changed_at
        where_conversation = " AND conversation_id = ?" if conversation_id is not None else ""
        parameters: tuple[Any, ...] = (conversation_id,) if conversation_id is not None else ()
        async with self._lock, self._transaction():
            rows = await self._fetchall_unlocked(
                """
                SELECT id FROM runs
                WHERE status IN ('planning', 'running', 'cancel_requested')
                """
                + where_conversation
                + " ORDER BY created_at, id",
                parameters,
            )
            run_ids = [str(row["id"]) for row in rows]
            if not run_ids:
                return []
            placeholders = ", ".join("?" for _ in run_ids)
            await self.connection.execute(
                f"""
                UPDATE runs
                SET status = ?, completed_at = ?, error = COALESCE(error, ?)
                WHERE id IN ({placeholders})
                """,
                (
                    RunStatus.INTERRUPTED.value,
                    now,
                    "Relay restarted before the run finished",
                    *run_ids,
                ),
            )
            await self.connection.execute(
                f"DELETE FROM workspace_leases WHERE run_id IN ({placeholders})",
                run_ids,
            )
            recovered_rows = await self._fetchall_unlocked(
                f"SELECT * FROM runs WHERE id IN ({placeholders}) ORDER BY created_at, id",
                run_ids,
            )
        return [_run_from_row(row) for row in recovered_rows]

    async def append_event(
        self,
        run_id: str,
        kind: EventKind | AgentEvent,
        payload: Mapping[str, Any] | None = None,
        *,
        created_at: float | None = None,
    ) -> RunEvent:
        if isinstance(kind, AgentEvent):
            if payload is not None:
                raise ValueError("payload must be omitted when appending an AgentEvent")
            payload = kind.payload
            event_kind = kind.kind
        else:
            event_kind = EventKind(kind)
        payload_json = json.dumps(dict(payload or {}), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        timestamp = time.time() if created_at is None else created_at

        async with self._lock, self._transaction():
            run = await self._fetchone_unlocked("SELECT id FROM runs WHERE id = ?", (run_id,))
            if run is None:
                raise RunNotFoundError(f"Run {run_id!r} was not found")
            row = await self._fetchone_unlocked(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM events WHERE run_id = ?",
                (run_id,),
            )
            assert row is not None
            seq = int(row["next_seq"])
            cursor = await self.connection.execute(
                "INSERT INTO events (run_id, seq, kind, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                (run_id, seq, event_kind.value, payload_json, timestamp),
            )
            event_id = int(cursor.lastrowid)
        return RunEvent(
            id=event_id,
            run_id=run_id,
            seq=seq,
            kind=event_kind,
            payload=json.loads(payload_json),
            created_at=timestamp,
        )

    async def list_events(
        self,
        run_id: str,
        *,
        after_seq: int = 0,
        limit: int = _DEFAULT_LIST_LIMIT,
    ) -> list[RunEvent]:
        if after_seq < 0:
            raise ValueError("after_seq must not be negative")
        limit = _validate_limit(limit)
        async with self._lock:
            rows = await self._fetchall_unlocked(
                """
                SELECT * FROM events
                WHERE run_id = ? AND seq > ?
                ORDER BY seq
                LIMIT ?
                """,
                (run_id, after_seq, limit),
            )
        return [_event_from_row(row) for row in rows]

    async def create_approval(self, approval: Approval) -> Approval:
        async with self._lock, self._transaction():
            run = await self._fetchone_unlocked("SELECT id FROM runs WHERE id = ?", (approval.run_id,))
            if run is None:
                raise RunNotFoundError(f"Run {approval.run_id!r} was not found")
            try:
                await self.connection.execute(
                    """
                    INSERT INTO approvals (
                        id, run_id, status, requested_at, expires_at, decided_at, decided_by
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        approval.id,
                        approval.run_id,
                        approval.status.value,
                        approval.requested_at,
                        approval.expires_at,
                        approval.decided_at,
                        approval.decided_by,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise InvalidStateError(f"Approval {approval.id!r} already exists or is invalid") from exc
            row = await self._fetchone_unlocked("SELECT * FROM approvals WHERE id = ?", (approval.id,))
        assert row is not None
        return _approval_from_row(row)

    async def publish_run_approval(
        self,
        run_id: str,
        *,
        plan: str,
        exit_code: int | None,
        approval: Approval,
    ) -> tuple[Run, Approval]:
        """Atomically publish the reviewed plan and its pending approval."""
        if approval.run_id != run_id or approval.status is not ApprovalStatus.PENDING:
            raise ValueError("approval must be pending and belong to the run")
        async with self._lock, self._transaction():
            run_row = await self._fetchone_unlocked("SELECT * FROM runs WHERE id = ?", (run_id,))
            if run_row is None:
                raise RunNotFoundError(f"Run {run_id!r} was not found")
            if RunStatus(run_row["status"]) is not RunStatus.PLANNING:
                raise InvalidStateError(f"Run {run_id!r} is not in the planning state")
            try:
                await self.connection.execute(
                    "UPDATE runs SET status = ?, plan = ?, exit_code = ? WHERE id = ? AND status = ?",
                    (
                        RunStatus.AWAITING_APPROVAL.value,
                        plan,
                        exit_code,
                        run_id,
                        RunStatus.PLANNING.value,
                    ),
                )
                await self.connection.execute(
                    """
                    INSERT INTO approvals (
                        id, run_id, status, requested_at, expires_at, decided_at, decided_by
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        approval.id,
                        approval.run_id,
                        approval.status.value,
                        approval.requested_at,
                        approval.expires_at,
                        approval.decided_at,
                        approval.decided_by,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise InvalidStateError(f"Approval for run {run_id!r} already exists or is invalid") from exc
            updated_run = await self._fetchone_unlocked("SELECT * FROM runs WHERE id = ?", (run_id,))
            updated_approval = await self._fetchone_unlocked("SELECT * FROM approvals WHERE id = ?", (approval.id,))
        assert updated_run is not None and updated_approval is not None
        return _run_from_row(updated_run), _approval_from_row(updated_approval)

    async def decide_run_approval(
        self,
        run_id: str,
        decision: ApprovalStatus,
        decided_by: str,
        *,
        decided_at: float | None = None,
    ) -> tuple[Run, Approval] | None:
        """Atomically decide an approval and move the run to its matching state."""
        target = ApprovalStatus(decision)
        if target not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
            raise ValueError("Approval decision must be approved or rejected")
        now = time.time() if decided_at is None else decided_at
        expired = False
        updated_run: sqlite3.Row | None = None
        updated_approval: sqlite3.Row | None = None
        async with self._lock, self._transaction():
            run_row = await self._fetchone_unlocked("SELECT * FROM runs WHERE id = ?", (run_id,))
            if run_row is None:
                raise RunNotFoundError(f"Run {run_id!r} was not found")
            approval_row = await self._fetchone_unlocked("SELECT * FROM approvals WHERE run_id = ?", (run_id,))
            if approval_row is None:
                return None
            if (
                RunStatus(run_row["status"]) is not RunStatus.AWAITING_APPROVAL
                or ApprovalStatus(approval_row["status"]) is not ApprovalStatus.PENDING
            ):
                return None

            approval_id = str(approval_row["id"])
            if float(approval_row["expires_at"]) <= now:
                expired = True
                await self.connection.execute(
                    "UPDATE approvals SET status = ?, decided_at = ? WHERE id = ? AND status = ?",
                    (ApprovalStatus.EXPIRED.value, now, approval_id, ApprovalStatus.PENDING.value),
                )
                await self.connection.execute(
                    """
                    UPDATE runs
                    SET status = ?, completed_at = ?, error = ?
                    WHERE id = ? AND status = ?
                    """,
                    (
                        RunStatus.REJECTED.value,
                        now,
                        "Approval expired",
                        run_id,
                        RunStatus.AWAITING_APPROVAL.value,
                    ),
                )
                await self.connection.execute("DELETE FROM workspace_leases WHERE run_id = ?", (run_id,))
            else:
                await self.connection.execute(
                    """
                    UPDATE approvals
                    SET status = ?, decided_at = ?, decided_by = ?
                    WHERE id = ? AND status = ?
                    """,
                    (target.value, now, decided_by, approval_id, ApprovalStatus.PENDING.value),
                )
                if target is ApprovalStatus.APPROVED:
                    await self.connection.execute(
                        """
                        UPDATE runs
                        SET status = ?, started_at = COALESCE(started_at, ?)
                        WHERE id = ? AND status = ?
                        """,
                        (
                            RunStatus.RUNNING.value,
                            now,
                            run_id,
                            RunStatus.AWAITING_APPROVAL.value,
                        ),
                    )
                else:
                    await self.connection.execute(
                        """
                        UPDATE runs SET status = ?, completed_at = ?
                        WHERE id = ? AND status = ?
                        """,
                        (
                            RunStatus.REJECTED.value,
                            now,
                            run_id,
                            RunStatus.AWAITING_APPROVAL.value,
                        ),
                    )
                    await self.connection.execute("DELETE FROM workspace_leases WHERE run_id = ?", (run_id,))
            updated_run = await self._fetchone_unlocked("SELECT * FROM runs WHERE id = ?", (run_id,))
            updated_approval = await self._fetchone_unlocked("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        if expired:
            raise ApprovalExpiredError(f"Approval for run {run_id!r} has expired")
        assert updated_run is not None and updated_approval is not None
        return _run_from_row(updated_run), _approval_from_row(updated_approval)

    async def cancel_awaiting_run(
        self,
        run_id: str,
        decided_by: str,
        *,
        changed_at: float | None = None,
    ) -> tuple[Run, Approval] | None:
        """Atomically reject a pending approval and cancel its run."""
        now = time.time() if changed_at is None else changed_at
        async with self._lock, self._transaction():
            run_row = await self._fetchone_unlocked("SELECT * FROM runs WHERE id = ?", (run_id,))
            if run_row is None:
                raise RunNotFoundError(f"Run {run_id!r} was not found")
            approval_row = await self._fetchone_unlocked("SELECT * FROM approvals WHERE run_id = ?", (run_id,))
            if (
                RunStatus(run_row["status"]) is not RunStatus.AWAITING_APPROVAL
                or approval_row is None
                or ApprovalStatus(approval_row["status"]) is not ApprovalStatus.PENDING
            ):
                return None
            approval_id = str(approval_row["id"])
            await self.connection.execute(
                """
                UPDATE approvals
                SET status = ?, decided_at = ?, decided_by = ?
                WHERE id = ? AND status = ?
                """,
                (
                    ApprovalStatus.REJECTED.value,
                    now,
                    decided_by,
                    approval_id,
                    ApprovalStatus.PENDING.value,
                ),
            )
            await self.connection.execute(
                "UPDATE runs SET status = ?, completed_at = ? WHERE id = ? AND status = ?",
                (
                    RunStatus.CANCELLED.value,
                    now,
                    run_id,
                    RunStatus.AWAITING_APPROVAL.value,
                ),
            )
            await self.connection.execute("DELETE FROM workspace_leases WHERE run_id = ?", (run_id,))
            updated_run = await self._fetchone_unlocked("SELECT * FROM runs WHERE id = ?", (run_id,))
            updated_approval = await self._fetchone_unlocked("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        assert updated_run is not None and updated_approval is not None
        return _run_from_row(updated_run), _approval_from_row(updated_approval)

    async def get_approval(self, approval_id: str) -> Approval | None:
        async with self._lock:
            row = await self._fetchone_unlocked("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        return _approval_from_row(row) if row is not None else None

    async def get_approval_for_run(self, run_id: str) -> Approval | None:
        async with self._lock:
            row = await self._fetchone_unlocked("SELECT * FROM approvals WHERE run_id = ?", (run_id,))
        return _approval_from_row(row) if row is not None else None

    async def decide_approval(
        self,
        approval_id: str,
        decision: ApprovalStatus,
        decided_by: str,
        *,
        decided_at: float | None = None,
    ) -> Approval | None:
        target = ApprovalStatus(decision)
        if target not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
            raise ValueError("Approval decision must be approved or rejected")
        now = time.time() if decided_at is None else decided_at
        expired = False
        updated: sqlite3.Row | None = None
        async with self._lock, self._transaction():
            row = await self._fetchone_unlocked("SELECT * FROM approvals WHERE id = ?", (approval_id,))
            if row is None:
                return None
            current = ApprovalStatus(row["status"])
            if current is not ApprovalStatus.PENDING:
                return None
            if float(row["expires_at"]) <= now:
                await self.connection.execute(
                    "UPDATE approvals SET status = ?, decided_at = ? WHERE id = ? AND status = ?",
                    (ApprovalStatus.EXPIRED.value, now, approval_id, ApprovalStatus.PENDING.value),
                )
                expired = True
            else:
                cursor = await self.connection.execute(
                    """
                    UPDATE approvals
                    SET status = ?, decided_at = ?, decided_by = ?
                    WHERE id = ? AND status = ?
                    """,
                    (target.value, now, decided_by, approval_id, ApprovalStatus.PENDING.value),
                )
                if cursor.rowcount != 1:
                    return None
                updated = await self._fetchone_unlocked("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        if expired:
            raise ApprovalExpiredError(f"Approval {approval_id!r} has expired")
        assert updated is not None
        return _approval_from_row(updated)

    async def expire_approval(self, approval_id: str, *, now: float | None = None) -> Approval:
        timestamp = time.time() if now is None else now
        async with self._lock, self._transaction():
            row = await self._fetchone_unlocked("SELECT * FROM approvals WHERE id = ?", (approval_id,))
            if row is None:
                raise InvalidStateError(f"Approval {approval_id!r} was not found")
            current = ApprovalStatus(row["status"])
            if current is not ApprovalStatus.PENDING:
                return _approval_from_row(row)
            if float(row["expires_at"]) > timestamp:
                raise InvalidStateError(f"Approval {approval_id!r} has not expired")
            await self.connection.execute(
                "UPDATE approvals SET status = ?, decided_at = ? WHERE id = ? AND status = ?",
                (ApprovalStatus.EXPIRED.value, timestamp, approval_id, ApprovalStatus.PENDING.value),
            )
            updated = await self._fetchone_unlocked("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        assert updated is not None
        return _approval_from_row(updated)

    async def expire_approvals(self, *, now: float | None = None) -> list[Approval]:
        timestamp = time.time() if now is None else now
        async with self._lock, self._transaction():
            rows = await self._fetchall_unlocked(
                "SELECT id FROM approvals WHERE status = ? AND expires_at <= ? ORDER BY expires_at, id",
                (ApprovalStatus.PENDING.value, timestamp),
            )
            approval_ids = [str(row["id"]) for row in rows]
            if not approval_ids:
                return []
            placeholders = ", ".join("?" for _ in approval_ids)
            await self.connection.execute(
                f"UPDATE approvals SET status = ?, decided_at = ? WHERE id IN ({placeholders}) AND status = ?",
                (ApprovalStatus.EXPIRED.value, timestamp, *approval_ids, ApprovalStatus.PENDING.value),
            )
            run_rows = await self._fetchall_unlocked(
                f"""
                SELECT approvals.run_id
                FROM approvals
                JOIN runs ON runs.id = approvals.run_id
                WHERE approvals.id IN ({placeholders}) AND runs.status = ?
                """,
                (*approval_ids, RunStatus.AWAITING_APPROVAL.value),
            )
            run_ids = [str(row["run_id"]) for row in run_rows]
            if run_ids:
                run_placeholders = ", ".join("?" for _ in run_ids)
                await self.connection.execute(
                    f"""
                    UPDATE runs
                    SET status = ?, completed_at = ?, error = ?
                    WHERE id IN ({run_placeholders}) AND status = ?
                    """,
                    (
                        RunStatus.REJECTED.value,
                        timestamp,
                        "Approval expired",
                        *run_ids,
                        RunStatus.AWAITING_APPROVAL.value,
                    ),
                )
                await self.connection.execute(
                    f"DELETE FROM workspace_leases WHERE run_id IN ({run_placeholders})",
                    run_ids,
                )
            expired_rows = await self._fetchall_unlocked(
                f"SELECT * FROM approvals WHERE id IN ({placeholders}) ORDER BY expires_at, id",
                approval_ids,
            )
        return [_approval_from_row(row) for row in expired_rows]

    async def claim_telegram_update(
        self,
        update_id: int,
        *,
        bot_key: str = "default",
        claimed_at: float | None = None,
    ) -> bool:
        if not bot_key:
            raise ValueError("bot_key must not be empty")
        timestamp = time.time() if claimed_at is None else claimed_at
        async with self._lock, self._transaction():
            cursor = await self.connection.execute(
                """
                INSERT OR IGNORE INTO telegram_update_claims (bot_key, update_id, claimed_at)
                VALUES (?, ?, ?)
                """,
                (bot_key, update_id, timestamp),
            )
            return cursor.rowcount == 1

    async def get_handoff_context(
        self,
        conversation_id: str,
        *,
        excluding_agent: AgentKind | None = None,
        before: float | None = None,
        limit_runs: int = 3,
        max_chars: int = 8_000,
    ) -> str | None:
        if not 1 <= limit_runs <= 20:
            raise ValueError("limit_runs must be between 1 and 20")
        if not 256 <= max_chars <= 100_000:
            raise ValueError("max_chars must be between 256 and 100000")
        clauses = ["conversation_id = ?", "status = ?", "result IS NOT NULL"]
        filters: list[Any] = [conversation_id, RunStatus.COMPLETED.value]
        if excluding_agent is not None:
            clauses.append("agent != ?")
            filters.append(AgentKind(excluding_agent).value)
        if before is not None:
            clauses.append("COALESCE(completed_at, created_at) < ?")
            filters.append(before)
        parameters = [max_chars, max_chars, *filters, limit_runs]
        async with self._lock:
            rows = await self._fetchall_unlocked(
                f"""
                SELECT agent, substr(prompt, 1, ?) AS prompt, substr(result, 1, ?) AS result,
                       created_at, completed_at
                FROM runs
                WHERE {" AND ".join(clauses)}
                ORDER BY COALESCE(completed_at, created_at) DESC, id DESC
                LIMIT ?
                """,
                parameters,
            )
        if not rows:
            return None
        blocks = [
            f"Agent: {row['agent']}\nUser request:\n{row['prompt']}\nAgent result:\n{row['result']}"
            for row in reversed(rows)
        ]
        context = "\n\n---\n\n".join(blocks)
        if len(context) > max_chars:
            marker = "[Earlier handoff context truncated]\n"
            context = marker + context[-(max_chars - len(marker)) :]
        return context


def _validate_limit(limit: int) -> int:
    if not 1 <= limit <= _MAX_LIST_LIMIT:
        raise ValueError(f"limit must be between 1 and {_MAX_LIST_LIMIT}")
    return limit


def _can_transition(current: RunStatus, target: RunStatus) -> bool:
    if current in TERMINAL_RUN_STATUSES:
        return False
    if target in TERMINAL_RUN_STATUSES:
        return True
    active_transitions = {
        RunStatus.QUEUED: {
            RunStatus.PLANNING,
            RunStatus.AWAITING_APPROVAL,
            RunStatus.RUNNING,
            RunStatus.CANCEL_REQUESTED,
        },
        RunStatus.PLANNING: {
            RunStatus.AWAITING_APPROVAL,
            RunStatus.RUNNING,
            RunStatus.CANCEL_REQUESTED,
        },
        RunStatus.AWAITING_APPROVAL: {RunStatus.RUNNING, RunStatus.CANCEL_REQUESTED},
        RunStatus.RUNNING: {RunStatus.AWAITING_APPROVAL, RunStatus.CANCEL_REQUESTED},
        RunStatus.CANCEL_REQUESTED: set(),
    }
    return target in active_transitions[current]


def _conversation_from_row(row: sqlite3.Row) -> Conversation:
    return Conversation(
        id=str(row["id"]),
        owner_type=str(row["owner_type"]),
        owner_id=str(row["owner_id"]),
        workspace=str(row["workspace"]),
        active_agent=AgentKind(row["active_agent"]),
        title=str(row["title"]),
        created_at=float(row["created_at"]),
        updated_at=float(row["updated_at"]),
    )


def _run_from_row(row: sqlite3.Row) -> Run:
    return Run(
        id=str(row["id"]),
        conversation_id=str(row["conversation_id"]),
        agent=AgentKind(row["agent"]),
        mode=RunMode(row["mode"]),
        status=RunStatus(row["status"]),
        prompt=str(row["prompt"]),
        initiator_id=str(row["initiator_id"]),
        permission_mode=PermissionMode(row["permission_mode"]),
        model=row["model"],
        reasoning_effort=row["reasoning_effort"],
        native_session_id=row["native_session_id"],
        plan=row["plan"],
        result=row["result"],
        error=row["error"],
        exit_code=row["exit_code"],
        created_at=float(row["created_at"]),
        started_at=float(row["started_at"]) if row["started_at"] is not None else None,
        completed_at=float(row["completed_at"]) if row["completed_at"] is not None else None,
        auto_route=bool(row["auto_route"]),
    )


def _event_from_row(row: sqlite3.Row) -> RunEvent:
    payload = json.loads(row["payload_json"])
    if not isinstance(payload, dict):
        raise InvalidStateError(f"Event {row['id']} has an invalid payload")
    return RunEvent(
        id=int(row["id"]),
        run_id=str(row["run_id"]),
        seq=int(row["seq"]),
        kind=EventKind(row["kind"]),
        payload=payload,
        created_at=float(row["created_at"]),
    )


def _approval_from_row(row: sqlite3.Row) -> Approval:
    return Approval(
        id=str(row["id"]),
        run_id=str(row["run_id"]),
        status=ApprovalStatus(row["status"]),
        requested_at=float(row["requested_at"]),
        expires_at=float(row["expires_at"]),
        decided_at=float(row["decided_at"]) if row["decided_at"] is not None else None,
        decided_by=str(row["decided_by"]) if row["decided_by"] is not None else None,
    )
