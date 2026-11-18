"""Repeatable local SLO sizing study; each repetition is the uncertainty unit."""

from __future__ import annotations

import asyncio
import json
import random
import statistics
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ai_systems_performance_lab.experiments.local_http import (
    Condition,
    _run_once,
    _summarize,
    _write_csv,
)
from ai_systems_performance_lab.experiments.slo import SizingStudyRequest


def _interval(values: list[float], confidence: float, seed: int) -> list[float] | None:
    """Percentile bootstrap over repetition-level values, not request rows."""
    if len(values) < 10:
        return None
    rng = random.Random(seed)
    boot = sorted(statistics.mean(rng.choices(values, k=len(values))) for _ in range(4000))
    tail = (1 - confidence) / 2
    return [boot[int(tail * len(boot))], boot[min(len(boot) - 1, int((1 - tail) * len(boot)))] ]


def _mean_and_interval(values: list[float], confidence: float, seed: int) -> dict[str, Any]:
    return {
        "mean": statistics.mean(values) if values else None,
        "interval": _interval(values, confidence, seed),
        "repetition_count": len(values),
        "uncertainty_unit": "whole repetition",
    }


def _candidate_report(
    rows: list[dict[str, Any]], config_id: str, request: SizingStudyRequest
) -> dict[str, Any]:
    reps = [row for row in rows if row["condition"] == config_id]
    confidence = request.slo.confidence_level
    goodput = [float(row["deadline_compliance_among_offered"]) for row in reps]
    rejected = [float(row["rejection_fraction"]) for row in reps]
    admitted_failure = [float(row["admitted_failure_fraction"]) for row in reps]
    g = _mean_and_interval(goodput, confidence, request.seed + 11)
    r = _mean_and_interval(rejected, confidence, request.seed + 23)
    f = _mean_and_interval(admitted_failure, confidence, request.seed + 37)
    enough = len(reps) >= 10 and all(value["interval"] is not None for value in (g, r, f))
    observed_events = {
        "deadline_misses": sum(max(0, int(row["offered"]) - int(row["success_within_deadline"])) for row in reps),
        "rejections": sum(int(row["rejected"]) for row in reps),
        "admitted_technical_failures": sum(int(row["failed"]) + int(row["timed_out"]) for row in reps),
    }
    rare_event_warning = [f"no_{name}_observed" for name, count in observed_events.items() if count == 0]
    if enough:
        feasible = (
            g["interval"][0] >= request.slo.deadline_goodput_target
            and r["interval"][1] <= request.slo.max_rejection_fraction
            and f["interval"][1] <= request.slo.max_admitted_failure_fraction
        )
        if feasible and rare_event_warning:
            feasible = None
            classification = "inconclusive_rare_event_evidence"
        else:
            classification = "feasible" if feasible else "infeasible"
    else:
        feasible = None
        classification = "insufficient_repetitions"
    summary_means = statistics.mean(row["successful_goodput_rps"] for row in reps) if reps else None
    p95 = [row["successful_end_to_end_ms"]["p95"] for row in reps if row["successful_end_to_end_ms"]["p95"] is not None]
    admitted_failures = sum(row["failed"] + row["timed_out"] for row in reps)
    admitted = sum(row["admitted"] for row in reps)
    return {
        "configuration_id": config_id,
        "concurrency": reps[0]["concurrency"] if reps else None,
        "queue_capacity": reps[0]["queue_capacity"] if reps else None,
        "arrival": reps[0].get("arrival") if reps else None,
        "arrival_rate_rps": reps[0].get("offered_rate_rps") if reps else None,
        "repetitions": len(reps),
        "slo_metrics": {
            "deadline_goodput_fraction": g,
            "rejection_fraction": r,
            "admitted_failure_fraction": f,
        },
        "diagnostics": {
            "mean_successful_goodput_rps": summary_means,
            "mean_of_per_repetition_successful_p95_ms": statistics.mean(p95) if p95 else None,
            "admitted_failure_count": admitted_failures,
            "admitted_count": admitted,
        },
        "feasible": feasible,
        "classification": classification,
        "rare_event_warning": rare_event_warning,
        "observed_adverse_event_counts": observed_events,
        "cost_estimate": None,
    }


async def run_sizing_study(
    request: SizingStudyRequest,
    output_root: Path,
    progress: Any,
    cancelled: asyncio.Event,
) -> Path:
    """Run matched candidate configurations and persist auditable artifacts."""
    run_id = "sizing-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    output = output_root / "local" / run_id
    output.mkdir(parents=True, exist_ok=False)
    candidate_values: list[int | str] = {
        "concurrency": request.concurrency_candidates,
        "queue_capacity": request.queue_candidates,
        "rate": request.arrival_rate_candidates,
        "arrival": request.arrival_candidates,
    }[request.focus]
    candidates = []
    for value in candidate_values:
        concurrency = int(value) if request.focus == "concurrency" else request.concurrency
        queue = int(value) if request.focus == "queue_capacity" else request.queue_capacity
        rate = float(value) if request.focus == "rate" else float(request.arrival_rate_rps)
        arrival = str(value) if request.focus == "arrival" else request.arrival
        prefix = {"concurrency": "c", "queue_capacity": "k", "rate": "r", "arrival": "a"}[request.focus]
        config_id = f"{prefix}{value}".replace(".", "p")
        candidates.append(Condition(
            name=config_id,
            groups=("sizing",),
            arrival=arrival,
            rate=rate,
            concurrency=concurrency,
            queue_capacity=queue,
        ))
    summaries: list[dict[str, Any]] = []
    requests: list[dict[str, Any]] = []
    occupancy: list[dict[str, Any]] = []
    total = len(candidates) * request.repetitions
    completed = 0
    for repetition in range(1, request.repetitions + 1):
        # Every candidate sees the same offered arrival process and case order.
        seed = request.seed + repetition - 1
        order_rng = random.Random(seed ^ 0x5A17)
        order = candidates.copy()
        order_rng.shuffle(order)
        for condition in order:
            if cancelled.is_set():
                raise asyncio.CancelledError
            deadline = request.slo.deadline_ms
            rows, samples, info = await _run_once(
                condition,
                request.duration_s,
                seed,
                deadline,
                request.tool_delay_ms,
                request.model_delay_ms,
            )
            summaries.append(_summarize(rows, info["server"], condition, repetition))
            requests.extend({"condition": condition.name, "repetition": repetition, **row} for row in rows)
            occupancy.extend({"condition": condition.name, "repetition": repetition, **sample} for sample in samples)
            completed += 1
            progress({"completed_runs": completed, "total_runs": total, "current": condition.name})
    reports = [_candidate_report(summaries, condition.name, request) for condition in candidates]
    feasible = [item for item in reports if item["feasible"] is True]
    # Recommendations are scoped to the axis actually swept; they are not AWS sizing claims.
    if request.focus == "concurrency":
        recommendation = min(feasible, key=lambda item: item["concurrency"]) if feasible else None
        basis = "lowest tested concurrency meeting all configured targets; queue and workload were held fixed"
    elif request.focus == "queue_capacity":
        recommendation = min(feasible, key=lambda item: item["queue_capacity"]) if feasible else None
        basis = "smallest tested waiting queue meeting all configured targets; concurrency and workload were held fixed"
    elif request.focus == "rate":
        recommendation = max(feasible, key=lambda item: item["arrival_rate_rps"]) if feasible else None
        basis = "highest tested offered arrival rate meeting all configured targets; this estimates a local capacity boundary"
    else:
        recommendation = next((item for item in feasible if item["arrival"] == "poisson"), None)
        basis = "Poisson arrivals are the more variable tested workload; deterministic arrivals provide the comparison"
    report = {
        "study_id": run_id,
        "provenance": "local simulated-agent HTTP measurements",
        "aws_calls": False,
        "bedrock_calls": False,
        "scope": f"one local simulated-agent API process; one-factor sweep of {request.focus}, remaining controls held fixed",
        "focus": request.focus,
        "slo_profile": request.slo.model_dump(),
        "candidate_results": reports,
        "recommendation": {
            "configuration_id": recommendation["configuration_id"] if recommendation else None,
            "basis": basis if recommendation else "no candidate demonstrated SLO feasibility with the current sample and workload",
            "cost_optimized": False,
            "cost_limitation": "No comparable infrastructure or inference price model is supplied; this local sweep cannot claim least-cost AWS sizing.",
        },
        "statistical_method": "Percentile bootstrap resamples whole repetitions (4,000 resamples; deterministic seeds are study seed + 11 for deadline goodput, + 23 for rejection, and + 37 for admitted failures). Requests within one repetition are not treated as independent. At least 10 repetitions are required to classify a candidate; intervals are descriptive and do not guarantee future workload performance.",
    }
    manifest = {
        "run_id": run_id,
        "source": "local simulated-agent HTTP measurements",
        "method": "matched seeded SLO sizing sweep; shuffled candidate execution order within repetition",
        "aws_calls": False,
        "bedrock_calls": False,
        "created_utc": datetime.now(UTC).isoformat(),
        "request": request.model_dump(),
        "conditions": [asdict(condition) for condition in candidates],
        "random_seeds": [request.seed + index for index in range(request.repetitions)],
        "scope": report["scope"],
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (output / "summary.json").write_text(json.dumps({"conditions": summaries, "sizing_study": report}, indent=2), encoding="utf-8")
    _write_csv(output / "requests.csv", requests)
    _write_csv(output / "queue_occupancy.csv", occupancy)
    return output
