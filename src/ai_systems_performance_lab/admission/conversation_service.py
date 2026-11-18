"""Bounded FIFO execution for user-facing agent conversation turns."""

from __future__ import annotations

import asyncio
import inspect
import logging
import uuid
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass
from math import ceil
from time import perf_counter
from typing import Any

from ai_systems_performance_lab.admission.service import QueueFullError
from ai_systems_performance_lab.agent.workload import (
    AgentTurn,
    UsageBudgetExceededError,
)

logger = logging.getLogger(__name__)


@dataclass
class TurnWorkItem:
    request_id: str
    turn: AgentTurn
    offered_at: float
    future: asyncio.Future[dict[str, Any]]
    progress: Callable[[dict[str, str]], None] | None


class BoundedConversationService:
    """A process-local concurrency and queue boundary for agent turns."""

    def __init__(
        self, agent: Any, *, concurrency: int = 2, queue_capacity: int = 4
    ) -> None:
        if concurrency < 1 or queue_capacity < 0:
            raise ValueError("concurrency must be >= 1 and queue_capacity must be >= 0")
        self.agent = agent
        self.concurrency = concurrency
        self.queue_capacity = queue_capacity
        self.ready: asyncio.Queue[TurnWorkItem | None] = asyncio.Queue()
        self.waiting: deque[TurnWorkItem] = deque()
        self.workers: list[asyncio.Task[None]] = []
        self.reserved = 0
        self.active = 0
        self.counts: Counter[str] = Counter()
        self.end_to_end_ms: list[float] = []
        self.queue_wait_ms: list[float] = []
        self.processing_ms: list[float] = []

    async def start(self) -> None:
        if not self.workers:
            self.workers = [
                asyncio.create_task(self._worker(), name=f"agent-turn-worker-{i}")
                for i in range(self.concurrency)
            ]

    async def stop(self) -> None:
        while self.reserved:
            await asyncio.sleep(0.001)
        for _ in self.workers:
            await self.ready.put(None)
        await asyncio.gather(*self.workers)
        self.workers.clear()

    async def submit(
        self,
        turn: AgentTurn,
        *,
        progress: Callable[[dict[str, str]], None] | None = None,
    ) -> dict[str, Any]:
        self.counts["offered"] += 1
        future: asyncio.Future[dict[str, Any]] = (
            asyncio.get_running_loop().create_future()
        )
        request_id = str(uuid.uuid4())

        def publish(event: dict[str, str]) -> None:
            if progress:
                progress({**event, "request_id": request_id})

        item = TurnWorkItem(
            request_id, turn, perf_counter(), future, publish if progress else None
        )
        if item.progress:
            item.progress({"type": "offered"})
        if self.reserved < self.concurrency:
            self.reserved += 1
            self.ready.put_nowait(item)
        elif len(self.waiting) < self.queue_capacity:
            self.waiting.append(item)
        else:
            self.counts["rejected"] += 1
            if item.progress:
                item.progress({"type": "rejected", "reason": "queue_full"})
            raise QueueFullError
        self.counts["admitted"] += 1
        if item.progress:
            item.progress({"type": "admitted"})
        return await future

    async def _worker(self) -> None:
        while True:
            item = await self.ready.get()
            if item is None:
                self.ready.task_done()
                return
            started = perf_counter()
            self.active += 1
            self.queue_wait_ms.append((started - item.offered_at) * 1000)
            if item.progress:
                item.progress({"type": "agent_started"})
            try:
                parameters = inspect.signature(self.agent.run).parameters
                if "progress" in parameters:
                    result = await self.agent.run(item.turn, progress=item.progress)
                else:
                    result = await self.agent.run(item.turn)
                ended = perf_counter()
                response = {
                    "request_id": item.request_id,
                    "case_id": result.case_id,
                    "conversation_id": result.conversation_id,
                    "summary": result.answer,
                    "evidence": result.evidence,
                    "execution_source": result.execution_source,
                    "model_calls": result.model_calls,
                    "tool_calls": result.tool_calls,
                    "tool_events": [
                        {
                            "name": event.name,
                            "duration_ms": event.duration_ms,
                            "configured_delay_ms": event.configured_delay_ms,
                            "outcome": event.outcome,
                        }
                        for event in result.tool_events
                    ],
                    "timing_ms": {
                        "queue_wait": (started - item.offered_at) * 1000,
                        "processing": (ended - started) * 1000,
                        "end_to_end": (ended - item.offered_at) * 1000,
                    },
                }
                self.counts["completed"] += 1
                self.counts["succeeded"] += 1
                self.processing_ms.append(response["timing_ms"]["processing"])
                self.end_to_end_ms.append(response["timing_ms"]["end_to_end"])
                if not item.future.cancelled():
                    item.future.set_result(response)
                if item.progress:
                    item.progress({"type": "request_completed"})
            except Exception as exc:
                if isinstance(exc, UsageBudgetExceededError):
                    logger.warning(
                        "Agent turn rejected by usage guard",
                        extra={
                            "request_id": item.request_id,
                            "case_id": item.turn.case_id,
                        },
                    )
                else:
                    logger.exception(
                        "Agent conversation turn failed",
                        extra={
                            "request_id": item.request_id,
                            "case_id": item.turn.case_id,
                        },
                    )
                self.counts["completed"] += 1
                self.counts["failed"] += 1
                if not item.future.cancelled():
                    item.future.set_exception(exc)
                if item.progress:
                    item.progress({"type": "request_failed"})
            finally:
                self.active -= 1
                self.reserved -= 1
                if self.waiting:
                    next_item = self.waiting.popleft()
                    self.reserved += 1
                    self.ready.put_nowait(next_item)
                self.ready.task_done()

    def snapshot(self) -> dict[str, Any]:
        return {
            **{
                key: self.counts[key]
                for key in (
                    "offered",
                    "admitted",
                    "rejected",
                    "completed",
                    "succeeded",
                    "failed",
                )
            },
            "in_flight": self.reserved + len(self.waiting),
            "waiting": len(self.waiting),
            "active": self.active,
            "concurrency_limit": self.concurrency,
            "queue_capacity": self.queue_capacity,
            "latency_ms": {
                "queue_wait": _quantiles(self.queue_wait_ms),
                "processing": _quantiles(self.processing_ms),
                "end_to_end": _quantiles(self.end_to_end_ms),
            },
            "scope": "process_local",
        }

    def state(self) -> dict[str, int | str]:
        """Return low-cost instantaneous occupancy without sorting latency samples."""
        return {
            "offered": self.counts["offered"],
            "admitted": self.counts["admitted"],
            "rejected": self.counts["rejected"],
            "completed": self.counts["completed"],
            "in_flight": self.reserved + len(self.waiting),
            "active": self.active,
            "waiting": len(self.waiting),
            "concurrency_limit": self.concurrency,
            "queue_capacity": self.queue_capacity,
            "scope": "process_local",
        }


def _quantiles(samples: list[float]) -> dict[str, float | None]:
    if not samples:
        return {"p50": None, "p95": None, "p99": None}
    ordered = sorted(samples)
    return {
        f"p{percentile}": ordered[max(0, ceil(len(ordered) * percentile / 100) - 1)]
        for percentile in (50, 95, 99)
    }
