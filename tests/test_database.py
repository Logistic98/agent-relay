from __future__ import annotations

import asyncio
import sqlite3
import stat
from dataclasses import replace
from pathlib import Path

import pytest

from core.exceptions import ApprovalExpiredError, InvalidStateError
from domain.models import (
    AgentKind,
    Approval,
    ApprovalStatus,
    Conversation,
    EventKind,
    PermissionMode,
    Run,
    RunMode,
    RunStatus,
)
from persistence import Database


def conversation(
    conversation_id: str = "conversation-1",
    *,
    owner_id: str = "user-1",
    workspace: str = "/tmp/workspace",
) -> Conversation:
    return Conversation(
        id=conversation_id,
        owner_type="telegram",
        owner_id=owner_id,
        workspace=workspace,
        active_agent=AgentKind.CODEX,
        title="Test conversation",
        created_at=100.0,
        updated_at=100.0,
    )


def run(
    run_id: str,
    *,
    conversation_id: str = "conversation-1",
    status: RunStatus = RunStatus.QUEUED,
    agent: AgentKind = AgentKind.CODEX,
    created_at: float = 101.0,
    prompt: str = "Make a safe change",
) -> Run:
    return Run(
        id=run_id,
        conversation_id=conversation_id,
        agent=agent,
        mode=RunMode.RUN,
        status=status,
        prompt=prompt,
        initiator_id="user-1",
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_initialization_permissions_pragmas_and_conversation_crud(tmp_path: Path) -> None:
    data_dir = tmp_path / "private"
    data_dir.mkdir(mode=0o755)
    database_path = data_dir / "relay.db"

    async with Database(database_path) as database:
        assert await database.ping() is True
        assert stat.S_IMODE(data_dir.stat().st_mode) == 0o755
        assert stat.S_IMODE(database_path.stat().st_mode) == 0o600

        async with database.connection.execute("PRAGMA foreign_keys") as cursor:
            assert (await cursor.fetchone())[0] == 1
        async with database.connection.execute("PRAGMA journal_mode") as cursor:
            assert str((await cursor.fetchone())[0]).lower() == "wal"
        async with database.connection.execute("PRAGMA busy_timeout") as cursor:
            assert (await cursor.fetchone())[0] == 5_000

        created = await database.create_conversation(conversation())
        assert created.active_agent is AgentKind.CODEX
        assert await database.get_conversation(created.id) == created
        assert await database.list_conversations(owner_type="telegram", owner_id="user-1") == [created]

        changed = await database.update_conversation_agent(created.id, AgentKind.CLAUDE, updated_at=110.0)
        assert changed.active_agent is AgentKind.CLAUDE
        assert changed.updated_at == 110.0

        assert await database.get_active_conversation("telegram", "user-1") is None
        active = await database.set_active_conversation("telegram", "user-1", created.id, updated_at=111.0)
        assert active == changed
        assert await database.get_active_conversation_id("telegram", "user-1") == created.id
        assert await database.get_active_conversation("telegram", "user-1") == changed

        await database.create_conversation(conversation("other", owner_id="user-2"))
        with pytest.raises(InvalidStateError):
            await database.set_active_conversation("telegram", "user-1", "other")
        assert await database.set_active_conversation("telegram", "user-1", None) is None
        assert await database.get_active_conversation_id("telegram", "user-1") is None

        assert await database.get_native_session(created.id, AgentKind.CODEX) is None
        await database.set_native_session(created.id, AgentKind.CODEX, "codex-session", updated_at=112.0)
        assert await database.get_native_session(created.id, AgentKind.CODEX) == "codex-session"
        await database.set_native_session(created.id, AgentKind.CODEX, None)
        assert await database.get_native_session(created.id, AgentKind.CODEX) is None

    new_database_path = tmp_path / "new-private" / "relay.db"
    async with Database(new_database_path):
        assert stat.S_IMODE(new_database_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(new_database_path.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_existing_database_adds_and_persists_auto_route_column(tmp_path: Path) -> None:
    path = tmp_path / "data" / "relay.db"
    database = Database(path)
    await database.initialize()
    await database.close()

    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE runs DROP COLUMN auto_route")

    async with Database(path) as reopened:
        await reopened.create_conversation(conversation())
        created = await reopened.create_run(replace(run("auto-run"), auto_route=True))
        loaded = await reopened.get_run(created.id)

    assert loaded is not None
    assert loaded.auto_route is True


@pytest.mark.asyncio
async def test_existing_database_adds_and_persists_permission_mode_column(tmp_path: Path) -> None:
    path = tmp_path / "data" / "relay.db"
    database = Database(path)
    await database.initialize()
    await database.close()

    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE runs DROP COLUMN permission_mode")

    async with Database(path) as reopened:
        await reopened.create_conversation(conversation())
        created = await reopened.create_run(replace(run("full-access-run"), permission_mode=PermissionMode.FULL_ACCESS))
        loaded = await reopened.get_run(created.id)

    assert loaded is not None
    assert loaded.permission_mode is PermissionMode.FULL_ACCESS


@pytest.mark.asyncio
async def test_existing_database_adds_and_persists_run_model_settings(tmp_path: Path) -> None:
    path = tmp_path / "data" / "relay.db"
    database = Database(path)
    await database.initialize()
    await database.close()

    with sqlite3.connect(path) as connection:
        connection.execute("ALTER TABLE runs DROP COLUMN model")
        connection.execute("ALTER TABLE runs DROP COLUMN reasoning_effort")

    async with Database(path) as reopened:
        await reopened.create_conversation(conversation())
        created = await reopened.create_run(
            replace(run("configured-run"), model="gpt-5.6-sol", reasoning_effort="xhigh")
        )
        loaded = await reopened.get_run(created.id)

    assert loaded is not None
    assert loaded.model == "gpt-5.6-sol"
    assert loaded.reasoning_effort == "xhigh"


@pytest.mark.asyncio
async def test_queued_follow_up_survives_restart_and_can_be_claimed(tmp_path: Path) -> None:
    path = tmp_path / "data" / "relay.db"
    async with Database(path) as database:
        await database.create_conversation(conversation())
        queued = await database.create_run(run("queued-follow-up"))
        assert queued.status is RunStatus.QUEUED

    async with Database(path) as reopened:
        assert await reopened.recover_active_runs() == []
        claimed = await reopened.claim_next_queued_run(changed_at=120.0)
        assert claimed is not None
        assert claimed.id == queued.id
        assert claimed.status is RunStatus.PLANNING
        assert claimed.started_at == 120.0


@pytest.mark.asyncio
async def test_concurrent_queued_runs_are_durable_but_only_one_is_claimed(tmp_path: Path) -> None:
    path = tmp_path / "data" / "relay.db"
    first = Database(path)
    second = Database(path)
    await first.initialize()
    await second.initialize()
    try:
        await first.create_conversation(conversation())
        outcomes = await asyncio.gather(
            first.create_run(run("run-a")),
            second.create_run(run("run-b")),
            return_exceptions=True,
        )
        successes = [outcome for outcome in outcomes if isinstance(outcome, Run)]
        assert len(successes) == 2
        claims = await asyncio.gather(first.claim_next_queued_run(), second.claim_next_queued_run())
        claimed = [item for item in claims if item is not None]
        assert len(claimed) == 1
        assert claimed[0].status is RunStatus.PLANNING
        active = await first.find_active_run("conversation-1")
        assert active is not None
        assert active.id == claimed[0].id
    finally:
        await second.close()
        await first.close()


@pytest.mark.asyncio
async def test_workspace_lease_blocks_active_runs_across_conversations(tmp_path: Path) -> None:
    path = tmp_path / "data" / "relay.db"
    async with Database(path) as database:
        await database.create_conversation(conversation("conversation-a", owner_id="alice"))
        await database.create_conversation(conversation("conversation-b", owner_id="bob"))
        first = await database.create_run(run("run-a", conversation_id="conversation-a"))
        second = await database.create_run(run("run-b", conversation_id="conversation-b"))
        claimed = await database.claim_next_queued_run()
        assert claimed is not None and claimed.id == first.id

        assert await database.claim_next_queued_run() is None

        stopped = await database.transition_run_status(
            first.id,
            RunStatus.CANCELLED,
            expected_statuses={RunStatus.PLANNING},
        )
        assert stopped is not None
        claimed_second = await database.claim_next_queued_run()
        assert claimed_second is not None and claimed_second.id == second.id


@pytest.mark.asyncio
async def test_workspace_lease_blocks_ancestor_and_descendant_paths(tmp_path: Path) -> None:
    path = tmp_path / "data" / "relay.db"
    first = Database(path)
    second = Database(path)
    await first.initialize()
    await second.initialize()
    try:
        await first.create_conversation(conversation("conversation-root", owner_id="alice", workspace="/tmp/workspace"))
        await first.create_conversation(
            conversation("conversation-child", owner_id="bob", workspace="/tmp/workspace/subproject")
        )
        outcomes = await asyncio.gather(
            first.create_run(run("run-root", conversation_id="conversation-root")),
            second.create_run(run("run-child", conversation_id="conversation-child")),
        )
        assert len(outcomes) == 2
        claims = await asyncio.gather(first.claim_next_queued_run(), second.claim_next_queued_run())
        assert sum(item is not None for item in claims) == 1
    finally:
        await second.close()
        await first.close()


@pytest.mark.asyncio
async def test_initialization_backfills_workspace_lease_for_existing_active_run(tmp_path: Path) -> None:
    path = tmp_path / "data" / "relay.db"
    database = Database(path)
    await database.initialize()
    await database.create_conversation(conversation("conversation-a", owner_id="alice"))
    await database.create_conversation(conversation("conversation-b", owner_id="bob"))
    await database.create_run(
        run("legacy-awaiting", conversation_id="conversation-a", status=RunStatus.AWAITING_APPROVAL)
    )
    await database.connection.execute("DELETE FROM workspace_leases")
    await database.close()

    async with Database(path) as reopened:
        await reopened.create_run(run("new-run", conversation_id="conversation-b"))
        assert await reopened.claim_next_queued_run() is None


@pytest.mark.asyncio
async def test_initialization_fails_closed_for_legacy_overlapping_active_runs(tmp_path: Path) -> None:
    path = tmp_path / "data" / "relay.db"
    database = Database(path)
    await database.initialize()
    await database.create_conversation(conversation("conversation-root", owner_id="alice", workspace="/tmp/workspace"))
    await database.create_conversation(
        conversation("conversation-child", owner_id="bob", workspace="/tmp/workspace/subproject")
    )
    await database.create_run(
        run("legacy-root", conversation_id="conversation-root", status=RunStatus.AWAITING_APPROVAL)
    )
    await database.connection.execute("DELETE FROM workspace_leases")
    await database.connection.execute(
        """
        INSERT INTO runs (
            id, conversation_id, agent, mode, status, prompt, initiator_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "legacy-child",
            "conversation-child",
            AgentKind.CODEX.value,
            RunMode.RUN.value,
            RunStatus.AWAITING_APPROVAL.value,
            "legacy child task",
            "bob",
            102.0,
        ),
    )
    await database.close()

    reopened = Database(path)
    with pytest.raises(InvalidStateError, match="overlapping workspaces"):
        await reopened.initialize()
    await reopened.close()


@pytest.mark.asyncio
async def test_run_updates_status_cas_recovery_and_bounded_handoff(tmp_path: Path) -> None:
    async with Database(tmp_path / "data" / "relay.db") as database:
        await database.create_conversation(conversation())
        queued = await database.create_run(run("active-run"))
        assert queued.status is RunStatus.QUEUED

        updated = await database.update_run(
            queued.id,
            plan="First inspect the repository",
            native_session_id="native-1",
            exit_code=None,
        )
        assert updated.plan == "First inspect the repository"
        assert updated.native_session_id == "native-1"

        running = await database.transition_run_status(
            queued.id,
            RunStatus.RUNNING,
            expected_statuses={RunStatus.QUEUED},
            changed_at=120.0,
        )
        assert running is not None
        assert running.status is RunStatus.RUNNING
        assert running.started_at == 120.0
        assert (
            await database.transition_run_status(
                queued.id,
                RunStatus.COMPLETED,
                expected_statuses={RunStatus.PLANNING},
                changed_at=121.0,
            )
            is None
        )

        recovered = await database.recover_active_runs(changed_at=130.0)
        assert [item.id for item in recovered] == [queued.id]
        assert recovered[0].status is RunStatus.INTERRUPTED
        assert recovered[0].completed_at == 130.0
        assert await database.find_active_run("conversation-1") is None

        awaiting = await database.create_run(run("awaiting-run", status=RunStatus.AWAITING_APPROVAL, created_at=135.0))
        assert await database.recover_active_runs(changed_at=136.0) == []
        assert await database.find_active_run("conversation-1") == awaiting
        rejected = await database.transition_run_status(
            awaiting.id,
            RunStatus.REJECTED,
            expected_statuses={RunStatus.AWAITING_APPROVAL},
            changed_at=137.0,
        )
        assert rejected is not None

        await database.create_run(
            replace(
                run(
                    "completed-codex",
                    status=RunStatus.COMPLETED,
                    created_at=140.0,
                    prompt="A" * 500,
                ),
                result="Codex result " + "B" * 500,
                completed_at=141.0,
            )
        )
        await database.create_run(
            replace(
                run(
                    "completed-claude",
                    status=RunStatus.COMPLETED,
                    agent=AgentKind.CLAUDE,
                    created_at=150.0,
                ),
                result="Claude result",
                completed_at=151.0,
            )
        )
        runs = await database.list_runs("conversation-1")
        assert [item.id for item in runs] == [
            "completed-claude",
            "completed-codex",
            "awaiting-run",
            "active-run",
        ]

        context = await database.get_handoff_context(
            "conversation-1",
            excluding_agent=AgentKind.CLAUDE,
            before=145.0,
            max_chars=256,
        )
        assert context is not None
        assert len(context) <= 256
        assert "Claude result" not in context


@pytest.mark.asyncio
async def test_concurrent_events_have_gapless_sequence_and_support_cursor(tmp_path: Path) -> None:
    path = tmp_path / "data" / "relay.db"
    first = Database(path)
    second = Database(path)
    await first.initialize()
    await second.initialize()
    try:
        await first.create_conversation(conversation())
        await first.create_run(run("run-events"))
        writes = [
            (first if index % 2 == 0 else second).append_event(
                "run-events",
                EventKind.OUTPUT_DELTA,
                {"index": index},
                created_at=200.0 + index,
            )
            for index in range(20)
        ]
        await asyncio.gather(*writes)
        events = await first.list_events("run-events", limit=100)
        assert [event.seq for event in events] == list(range(1, 21))
        assert {event.payload["index"] for event in events} == set(range(20))
        assert [event.seq for event in await first.list_events("run-events", after_seq=15, limit=3)] == [16, 17, 18]
    finally:
        await second.close()
        await first.close()


@pytest.mark.asyncio
async def test_approval_decision_is_atomic_and_expiry_is_durable(tmp_path: Path) -> None:
    path = tmp_path / "data" / "relay.db"
    first = Database(path)
    second = Database(path)
    await first.initialize()
    await second.initialize()
    try:
        await first.create_conversation(conversation())
        await first.create_run(run("run-approval"))
        await first.create_approval(
            Approval(
                id="approval-1",
                run_id="run-approval",
                status=ApprovalStatus.PENDING,
                requested_at=300.0,
                expires_at=400.0,
            )
        )
        stored_approval = await first.get_approval_for_run("run-approval")
        assert stored_approval is not None
        assert stored_approval.id == "approval-1"
        decisions = await asyncio.gather(
            first.decide_approval(
                "approval-1",
                ApprovalStatus.APPROVED,
                "reviewer-a",
                decided_at=350.0,
            ),
            second.decide_approval(
                "approval-1",
                ApprovalStatus.REJECTED,
                "reviewer-b",
                decided_at=350.0,
            ),
        )
        assert sum(decision is not None for decision in decisions) == 1
        decided = await first.get_approval("approval-1")
        assert decided is not None
        assert decided.status in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}

        transitioned = await first.transition_run_status(
            "run-approval",
            RunStatus.REJECTED,
            expected_statuses={RunStatus.QUEUED},
            changed_at=360.0,
        )
        assert transitioned is not None
        await first.create_run(run("run-expired", created_at=361.0))
        await first.create_approval(
            Approval(
                id="approval-expired",
                run_id="run-expired",
                status=ApprovalStatus.PENDING,
                requested_at=300.0,
                expires_at=320.0,
            )
        )
        with pytest.raises(ApprovalExpiredError):
            await first.decide_approval(
                "approval-expired",
                ApprovalStatus.APPROVED,
                "late-reviewer",
                decided_at=321.0,
            )
        expired = await first.get_approval("approval-expired")
        assert expired is not None
        assert expired.status is ApprovalStatus.EXPIRED
        assert expired.decided_at == 321.0
    finally:
        await second.close()
        await first.close()


@pytest.mark.asyncio
async def test_plan_publication_and_run_decision_are_single_transactions(tmp_path: Path) -> None:
    path = tmp_path / "data" / "relay.db"
    first = Database(path)
    second = Database(path)
    await first.initialize()
    await second.initialize()
    try:
        await first.create_conversation(conversation())
        planning = await first.create_run(run("atomic-run", status=RunStatus.PLANNING))
        approval = Approval(
            id="atomic-approval",
            run_id=planning.id,
            status=ApprovalStatus.PENDING,
            requested_at=10.0,
            expires_at=100.0,
        )

        waiting, pending = await first.publish_run_approval(
            planning.id,
            plan="reviewed plan",
            exit_code=0,
            approval=approval,
        )
        assert waiting.status is RunStatus.AWAITING_APPROVAL
        assert waiting.plan == "reviewed plan"
        assert pending.status is ApprovalStatus.PENDING

        decisions = await asyncio.gather(
            first.decide_run_approval(
                planning.id,
                ApprovalStatus.APPROVED,
                "alice",
                decided_at=50.0,
            ),
            second.decide_run_approval(
                planning.id,
                ApprovalStatus.REJECTED,
                "alice",
                decided_at=50.0,
            ),
        )
        assert sum(decision is not None for decision in decisions) == 1
        final_run = await first.get_run(planning.id)
        final_approval = await first.get_approval(approval.id)
        assert final_run is not None and final_approval is not None
        if final_approval.status is ApprovalStatus.APPROVED:
            assert final_run.status is RunStatus.RUNNING
        else:
            assert final_approval.status is ApprovalStatus.REJECTED
            assert final_run.status is RunStatus.REJECTED
    finally:
        await second.close()
        await first.close()


@pytest.mark.asyncio
async def test_telegram_update_claim_is_idempotent_across_connections(tmp_path: Path) -> None:
    path = tmp_path / "data" / "relay.db"
    first = Database(path)
    second = Database(path)
    await first.initialize()
    await second.initialize()
    try:
        claims = await asyncio.gather(
            first.claim_telegram_update(12345, claimed_at=500.0),
            second.claim_telegram_update(12345, claimed_at=501.0),
        )
        assert sorted(claims) == [False, True]
        assert await first.claim_telegram_update(12345, claimed_at=502.0) is False
        assert await second.claim_telegram_update(12346, claimed_at=503.0) is True
        assert await second.claim_telegram_update(12345, bot_key="rotated-bot", claimed_at=504.0) is True
    finally:
        await second.close()
        await first.close()
