"""Process-local FIFO admission and bounded worker pool."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass
from math import ceil
from time import perf_counter
from typing import Any

from ai_systems_performance_lab.agent.workload import CreditCase, ReviewResult


class QueueFullError(Exception):
    """Raised when all waiting positions are occupied."""


@dataclass
class WorkItem:
    request_id: str
    case: CreditCase
    offered_at: float
    future: asyncio.Future[dict[str, Any]]
    progress: Callable[[dict[str, str]], None] | None = None


class BoundedReviewService:
    """A single-process queue with c workers and K waiting positions."""

    def __init__(
        self, agent: Any, concurrency: int = 2, queue_capacity: int = 4
    ) -> None:
        if concurrency < 1 or queue_capacity < 0:
            raise ValueError("concurrency must be >= 1 and queue_capacity must be >= 0")
        self.agent = agent
        self.concurrency = concurrency
        self.queue_capacity = queue_capacity
        self.ready: asyncio.Queue[WorkItem | None] = asyncio.Queue()
        self.waiting: deque[WorkItem] = deque()
        self.workers: list[asyncio.Task[None]] = []
        self.reserved = 0
        self.active = 0
        self.counts: Counter[str] = Counter()
        self.end_to_end_ms: list[float] = []
        self.queue_wait_ms: list[float] = []
        self.processing_ms: list[float] = []

    async def start(self) -> None:
        if self.workers:
            return
        self.workers = [
            asyncio.create_task(self._worker(), name=f"review-worker-{i}")
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
        case: CreditCase,
        *,
        progress: Callable[[dict[str, str]], None] | None = None,
    ) -> dict[str, Any]:
        self.counts["offered"] += 1
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        request_id = str(uuid.uuid4())

        def publish(event: dict[str, str]) -> None:
            if progress:
                progress({**event, "request_id": request_id})

        item = WorkItem(
            request_id, case, perf_counter(), future, publish if progress else None
        )
        if progress:
            item.progress({"type": "offered"})
        if self.reserved < self.concurrency:
            self.reserved += 1
            self.ready.put_nowait(item)
        elif len(self.waiting) < self.queue_capacity:
            self.waiting.append(item)
        else:
            self.counts["rejected"] += 1
            if progress:
                item.progress({"type": "rejected", "reason": "queue_full"})
            raise QueueFullError
        self.counts["admitted"] += 1
        if progress:
            item.progress({"type": "admitted"})
        return await future

    async def _worker(self) -> None:
        while True:
            item = await self.ready.get()
            if item is None:  # shutdown sentinel
                self.ready.task_done()
                return
            started = perf_counter()
            self.active += 1
            self.queue_wait_ms.append((started - item.offered_at) * 1000)
            if item.progress:
                item.progress({"type": "agent_started", "request_id": item.request_id})
            try:
                run_parameters = inspect.signature(self.agent.run).parameters
                if "progress" in run_parameters:
                    result: ReviewResult = await self.agent.run(
                        item.case, progress=item.progress
                    )
                else:
                    result = await self.agent.run(item.case)
                ended = perf_counter()
                response = {
                    "request_id": item.request_id,
                    "case_id": result.case_id,
                    "recommendation": result.recommendation,
                    "dscr": result.dscr,
                    "stressed_dscr": result.stressed_dscr,
                    "evidence": result.evidence,
                    "summary": result.summary,
                    "execution_source": result.execution_source,
                    "model_calls": result.model_calls,
                    "tool_calls": result.tool_calls,
                    "tool_events": [
                        {
                            "name": e.name,
                            "duration_ms": e.duration_ms,
                            "configured_delay_ms": e.configured_delay_ms,
                            "outcome": e.outcome,
                        }
                        for e in result.tool_events
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
                    item.progress(
                        {"type": "request_completed", "request_id": item.request_id}
                    )
            except Exception as exc:  # noqa: BLE001 - any agent error is a terminal request failure
                self.counts["completed"] += 1
                self.counts["failed"] += 1
                if not item.future.cancelled():
                    item.future.set_exception(exc)
                if item.progress:
                    item.progress(
                        {"type": "request_failed", "request_id": item.request_id}
                    )
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


def _quantiles(samples: list[float]) -> dict[str, float | None]:
    if not samples:
        return {"p50": None, "p95": None, "p99": None}
    ordered = sorted(samples)

    def nearest_rank(p: float) -> float:
        return ordered[max(0, ceil(len(ordered) * p) - 1)]

    return {
        "p50": nearest_rank(0.50),
        "p95": nearest_rank(0.95),
        "p99": nearest_rank(0.99),
    }
