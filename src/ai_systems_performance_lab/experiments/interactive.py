"""Bounded paired local experiments for the browser dashboard."""

from __future__ import annotations

import asyncio
import json
import random
import statistics
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from ai_systems_performance_lab.experiments.local_http import (
    Condition,
    _run_once,
    _summarize,
    _write_csv,
)


class ComparisonRequest(BaseModel):
    factor: Literal["arrival", "rate", "concurrency", "queue_capacity"] = "queue_capacity"
    baseline_value: str | float | int = 0
    candidate_value: str | float | int = 16
    arrival: Literal["deterministic", "poisson"] = "poisson"
    rate_rps: int = Field(default=20, ge=1, le=50)
    concurrency: int = Field(default=2, ge=1, le=16)
    queue_capacity: int = Field(default=4, ge=0, le=128)
    duration_s: int = Field(default=5, ge=1, le=30)
    repetitions: int = Field(default=10, ge=1, le=10)
    seed: int = Field(default=20261118, ge=0, le=2_147_483_647)
    deadline_ms: float = Field(default=500, gt=0, allow_inf_nan=False)
    tool_delay_ms: float = Field(default=20, ge=0, le=500)
    model_delay_ms: float = Field(default=30, ge=0, le=500)

    @model_validator(mode="after")
    def validate_factor_values(self) -> ComparisonRequest:
        if self.baseline_value == self.candidate_value:
            raise ValueError("baseline_value and candidate_value must differ")
        if self.duration_s * self.repetitions * 2 > 600:
            raise ValueError("Total offered experiment duration must not exceed 600 seconds")
        if self.factor == "arrival":
            allowed = {"deterministic", "poisson"}
            if self.baseline_value not in allowed or self.candidate_value not in allowed:
                raise ValueError("arrival comparison values must be deterministic or poisson")
        elif self.factor == "rate":
            self._integer_values(1, 50)
        elif self.factor == "concurrency":
            self._integer_values(1, 16)
        else:
            self._integer_values(0, 128)
        return self

    def _integer_values(self, lower: int, upper: int) -> None:
        values = (self.baseline_value, self.candidate_value)
        if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
            raise ValueError("comparison values must be integers")
        if any(value < lower or value > upper for value in values):
            raise ValueError(f"comparison values must be between {lower} and {upper}")


def conditions_for(request: ComparisonRequest) -> tuple[Condition, Condition]:
    values = (request.baseline_value, request.candidate_value)
    common: dict[str, Any] = {
        "arrival": request.arrival,
        "rate": request.rate_rps,
        "concurrency": request.concurrency,
        "queue_capacity": request.queue_capacity,
    }
    conditions: list[Condition] = []
    for name, value in zip(("baseline", "candidate"), values, strict=True):
        config = common.copy()
        if request.factor == "arrival":
            config["arrival"] = str(value)
        elif request.factor == "rate":
            config["rate"] = float(value)
        elif request.factor == "concurrency":
            config["concurrency"] = int(value)
        else:
            config["queue_capacity"] = int(value)
        conditions.append(Condition(name, (request.factor,), **config))
    return conditions[0], conditions[1]


def _paired_statistics(summaries: list[dict[str, Any]], seed: int) -> dict[str, Any]:
    by_rep: dict[int, dict[str, dict[str, Any]]] = {}
    for item in summaries:
        by_rep.setdefault(int(item["repetition"]), {})[item["condition"]] = item
    metrics = {
        "successful_goodput_rps": lambda row: row["successful_goodput_rps"],
        "rejection_fraction": lambda row: row["rejection_fraction"],
        "deadline_compliance_among_offered": lambda row: row["deadline_compliance_among_offered"],
        "successful_end_to_end_p95_ms": lambda row: row["successful_end_to_end_ms"]["p95"],
        "queue_wait_p95_ms": lambda row: row["successful_stage_ms"]["queue_wait"]["p95"],
    }
    result: dict[str, Any] = {}
    rng = random.Random(seed)
    for metric, getter in metrics.items():
        differences: list[float] = []
        for pair in by_rep.values():
            if "baseline" not in pair or "candidate" not in pair:
                continue
            baseline = getter(pair["baseline"])
            candidate = getter(pair["candidate"])
            if baseline is not None and candidate is not None:
                differences.append(float(candidate) - float(baseline))
        mean_difference = statistics.mean(differences) if differences else None
        interval: list[float] | None = None
        if len(differences) >= 10:
            boot_means = sorted(
                statistics.mean(rng.choices(differences, k=len(differences)))
                for _ in range(10_000)
            )
            interval = [boot_means[249], boot_means[9749]]
        result[metric] = {
            "candidate_minus_baseline_by_repetition": differences,
            "mean_paired_difference": mean_difference,
            "bootstrap_95_percent_interval": interval,
            "paired_repetitions": len(differences),
            "interval_method": "paired run-level percentile bootstrap; descriptive with 10 pairs",
        }
    return result


async def run_comparison(
    request: ComparisonRequest,
    output_root: Path,
    progress: Any,
    cancelled: asyncio.Event,
) -> Path:
    conditions = conditions_for(request)
    run_id = "comparison-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    output = output_root / "local" / run_id
    output.mkdir(parents=True, exist_ok=False)
    summaries: list[dict[str, Any]] = []
    requests: list[dict[str, Any]] = []
    occupancy: list[dict[str, Any]] = []
    baseline_rows: list[dict[str, Any]] = []
    total = request.repetitions * 2
    current = 0
    for repetition in range(1, request.repetitions + 1):
        # Matched seed pairs use the same arrival schedule and synthetic case order.
        paired_seed = request.seed + repetition - 1
        for condition in conditions:
            if cancelled.is_set():
                raise asyncio.CancelledError
            rows, samples, info = await _run_once(
                condition,
                request.duration_s,
                paired_seed,
                request.deadline_ms,
                request.tool_delay_ms,
                request.model_delay_ms,
            )
            summaries.append(_summarize(rows, info["server"], condition, repetition))
            requests.extend(
                {"condition": condition.name, "repetition": repetition, **row}
                for row in rows
            )
            occupancy.extend(
                {"condition": condition.name, "repetition": repetition, **row}
                for row in samples
            )
            if condition.name == "baseline":
                baseline_rows.extend(
                    {"repetition": repetition, **row} for row in info["baseline"]
                )
            current += 1
            progress({"completed_runs": current, "total_runs": total, "current": condition.name})
    manifest = {
        "run_id": run_id,
        "source": "local simulated-agent HTTP measurements",
        "aws_calls": False,
        "bedrock_calls": False,
        "created_utc": datetime.now(UTC).isoformat(),
        "method": "paired one-factor comparison with matched repetition seeds; common Poisson schedule when arrival type is held fixed",
        "request": request.model_dump(),
        "conditions": [asdict(condition) for condition in conditions],
        "random_seeds": [request.seed + index for index in range(request.repetitions)],
        "scope": "single local simulated-agent API process per run; not multi-replica AWS capacity",
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (output / "summary.json").write_text(
        json.dumps({"conditions": summaries, "paired_statistics": _paired_statistics(summaries, request.seed)}, indent=2),
        encoding="utf-8",
    )
    _write_csv(output / "requests.csv", requests)
    _write_csv(output / "queue_occupancy.csv", occupancy)
    _write_csv(output / "baseline_requests.csv", baseline_rows)
    return output
