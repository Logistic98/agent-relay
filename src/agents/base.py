from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from typing import Protocol, runtime_checkable

from core.exceptions import AgentUnavailableError
from domain.models import AgentEvent, AgentKind, AgentRequest, AgentResult

EventCallback = Callable[[AgentEvent], Awaitable[None]]


@runtime_checkable
class AgentAdapter(Protocol):
    kind: AgentKind

    async def run(self, request: AgentRequest, on_event: EventCallback) -> AgentResult: ...

    async def cancel(self, run_id: str) -> bool: ...


class AgentRegistry:
    def __init__(self, adapters: Iterable[AgentAdapter] = ()) -> None:
        self._adapters: dict[AgentKind, AgentAdapter] = {}
        for adapter in adapters:
            self.register(adapter)

    def register(self, adapter: AgentAdapter) -> None:
        if adapter.kind in self._adapters:
            raise ValueError(f"adapter already registered for {adapter.kind.value}")
        self._adapters[adapter.kind] = adapter

    def get(self, kind: AgentKind) -> AgentAdapter:
        try:
            return self._adapters[kind]
        except KeyError as exc:
            raise AgentUnavailableError(f"agent adapter is unavailable: {kind.value}") from exc

    @property
    def kinds(self) -> tuple[AgentKind, ...]:
        return tuple(self._adapters)
