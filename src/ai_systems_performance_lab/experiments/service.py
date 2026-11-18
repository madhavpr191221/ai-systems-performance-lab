"""Process-local lifecycle manager for one bounded interactive run."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from ai_systems_performance_lab.experiments.interactive import (
    ComparisonRequest,
    run_comparison,
)


class LocalExperimentManager:
    def __init__(self, output_root: Path) -> None:
        self.output_root = output_root.resolve()
        self.job: dict[str, Any] | None = None
        self._task: asyncio.Task[None] | None = None
        self._cancelled = asyncio.Event()

    def snapshot(self) -> dict[str, Any] | None:
        return self.job.copy() if self.job else None

    def start(self, request: ComparisonRequest) -> dict[str, Any]:
        if self._task is not None and not self._task.done():
            raise RuntimeError("an experiment is already running")
        job_id = uuid.uuid4().hex
        self._cancelled = asyncio.Event()
        self.job = {
            "job_id": job_id,
            "status": "running",
            "completed_runs": 0,
            "total_runs": request.repetitions * 2,
            "current": None,
            "run_id": None,
            "error": None,
            "request": request.model_dump(),
        }

        def progress(update: dict[str, Any]) -> None:
            if self.job and self.job["job_id"] == job_id:
                self.job.update(update)

        async def execute() -> None:
            try:
                path = await run_comparison(
                    request, self.output_root, progress, self._cancelled
                )
                if self.job and self.job["job_id"] == job_id:
                    self.job.update(status="completed", run_id=path.name, current=None)
            except asyncio.CancelledError:
                if self.job and self.job["job_id"] == job_id:
                    self.job.update(status="cancelled", current=None)
            except Exception as exc:  # noqa: BLE001 - safe short error for UI
                if self.job and self.job["job_id"] == job_id:
                    self.job.update(status="failed", error=type(exc).__name__, current=None)

        self._task = asyncio.create_task(execute(), name=f"local-experiment-{job_id}")
        return self.snapshot() or {}

    async def cancel(self) -> dict[str, Any] | None:
        if self._task is not None and not self._task.done():
            self._cancelled.set()
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        return self.snapshot()

    async def close(self) -> None:
        await self.cancel()
