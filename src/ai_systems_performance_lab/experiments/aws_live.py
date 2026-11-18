"""Bounded open-loop workload against an already deployed Bedrock agent API.

This command never creates or changes AWS infrastructure. Without ``--execute``
it only prints a token-based cost scenario. Live requests require both
``--execute`` and an explicit cost ceiling.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import random
import re
import statistics
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit
from uuid import uuid4

import httpx

MODEL_ID = "us.amazon.nova-2-lite-v1:0"
INPUT_PRICE_PER_MILLION = 0.33
OUTPUT_PRICE_PER_MILLION = 2.75
FARGATE_VCPU_USD_PER_VCPU_SECOND = 0.000011244
FARGATE_MEMORY_USD_PER_GB_SECOND = 0.000001235
PUBLIC_IPV4_USD_PER_HOUR = 0.005
PROMPTS = {
    "short": "What is the annual revenue in this synthetic case? Cite the source.",
    "ordinary": "Summarize the synthetic case and cite the supporting evidence.",
    "tool_heavy": (
        "Retrieve the case facts, calculate DSCR, apply the moderate stress scenario, "
        "and explain the synthetic policy result with citations."
    ),
}
CASE_IDS = {
    "short": "synthetic-case-000001",
    "ordinary": "synthetic-case-000002",
    "tool_heavy": "synthetic-case-000003",
}


@dataclass(frozen=True)
class PlannedTurn:
    repetition: int
    rate: float
    arrival: str
    index: int
    offset_s: float
    task_class: str


def arrival_offsets(
    kind: str, rate: float, duration_s: float, seed: int
) -> list[float]:
    if (
        not math.isfinite(rate)
        or rate <= 0
        or not math.isfinite(duration_s)
        or duration_s <= 0
    ):
        raise ValueError("rate and duration must be finite and positive")
    if kind == "deterministic":
        return [
            i / rate
            for i in range(math.ceil(rate * duration_s))
            if i / rate < duration_s
        ]
    if kind != "poisson":
        raise ValueError("arrival must be deterministic or poisson")
    rng = random.Random(seed)
    offsets: list[float] = []
    current = 0.0
    while True:
        current += rng.expovariate(rate)
        if current >= duration_s:
            return offsets
        offsets.append(current)


def workload_class(index: int) -> str:
    """Fixed 2:2:1 mix of short, ordinary, and tool-heavy questions."""
    return ("short", "ordinary", "short", "ordinary", "tool_heavy")[index % 5]


def plan_turns(
    *,
    rates: list[float],
    arrivals: list[str],
    duration_s: float,
    repetitions: int,
    seed: int,
) -> list[PlannedTurn]:
    turns: list[PlannedTurn] = []
    for arrival in arrivals:
        for rate in rates:
            for repetition in range(repetitions):
                offsets = arrival_offsets(arrival, rate, duration_s, seed + repetition)
                turns.extend(
                    PlannedTurn(
                        repetition + 1, rate, arrival, i, offset, workload_class(i)
                    )
                    for i, offset in enumerate(offsets)
                )
    return turns


def estimate_cost(
    *,
    turn_count: int,
    max_model_calls: int,
    input_tokens_per_call: int,
    output_tokens_per_call: int,
    input_price_per_million: float,
    output_price_per_million: float,
    task_runtime_hours: float = 2.0,
    hosting: str = "fargate",
) -> dict[str, float | int]:
    values = (input_price_per_million, output_price_per_million)
    if (
        turn_count < 0
        or max_model_calls < 1
        or input_tokens_per_call < 1
        or output_tokens_per_call < 1
    ):
        raise ValueError("turn and token bounds must be positive")
    if hosting not in {"local", "fargate"}:
        raise ValueError("hosting must be 'local' or 'fargate'")
    if (
        not math.isfinite(task_runtime_hours)
        or task_runtime_hours < 0
        or (hosting == "fargate" and task_runtime_hours == 0)
    ):
        raise ValueError("Fargate task runtime must be positive")
    if any(not math.isfinite(value) or value < 0 for value in values):
        raise ValueError("token prices must be finite and nonnegative")
    calls = turn_count * max_model_calls
    input_tokens = calls * input_tokens_per_call
    output_tokens = calls * output_tokens_per_call
    input_cost = input_tokens * input_price_per_million / 1_000_000
    output_cost = output_tokens * output_price_per_million / 1_000_000
    fargate_cost = (
        task_runtime_hours
        * (
            0.5 * 3600 * FARGATE_VCPU_USD_PER_VCPU_SECOND
            + 1.0 * 3600 * FARGATE_MEMORY_USD_PER_GB_SECOND
            + PUBLIC_IPV4_USD_PER_HOUR
        )
        if hosting == "fargate"
        else 0.0
    )
    return {
        "planned_turns": turn_count,
        "maximum_model_calls": calls,
        "scenario_input_tokens": input_tokens,
        "scenario_output_tokens": output_tokens,
        "scenario_bedrock_usd": input_cost + output_cost,
        "scenario_fargate_and_ipv4_usd": fargate_cost,
        "scenario_subtotal_usd": input_cost + output_cost + fargate_cost,
        "task_runtime_hours": task_runtime_hours,
    }


def summarize_repetition(
    rows: list[dict[str, Any]], *, deadline_s: float
) -> dict[str, Any]:
    offered = len(rows)
    counts = {
        outcome: sum(row["outcome"] == outcome for row in rows)
        for outcome in ("success", "rejected", "failed", "timed_out", "unfinished")
    }
    admitted = sum(bool(row.get("admitted")) for row in rows)
    admitted_failures = sum(
        bool(row.get("admitted")) and row["outcome"] in {"failed", "timed_out"}
        for row in rows
    )
    completed = [
        row
        for row in rows
        if row["outcome"] in {"success", "rejected", "failed", "timed_out"}
    ]
    latencies = [
        row["end_to_end_ms"]
        for row in rows
        if row["outcome"] == "success" and row["end_to_end_ms"] is not None
    ]
    on_time = sum(
        row["outcome"] == "success"
        and row["end_to_end_ms"] is not None
        and row["end_to_end_ms"] <= deadline_s * 1000
        for row in rows
    )
    return {
        "offered": offered,
        "admitted": admitted,
        **counts,
        "unfinished": counts["unfinished"] + offered - len(completed),
        "on_time_successes": on_time,
        "on_time_success_fraction_offered": on_time / offered if offered else None,
        "rejection_fraction_offered": counts["rejected"] / offered if offered else None,
        "technical_failure_fraction_admitted": admitted_failures / admitted
        if admitted
        else None,
        "successful_end_to_end_ms": {
            "mean": statistics.mean(latencies) if latencies else None,
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
        },
        "observed_input_tokens": sum(row.get("input_tokens") or 0 for row in rows),
        "observed_output_tokens": sum(row.get("output_tokens") or 0 for row in rows),
        "observed_model_calls": sum(row.get("model_calls") or 0 for row in rows),
        "observed_tool_calls": sum(row.get("tool_calls") or 0 for row in rows),
    }


def _bootstrap_mean_interval(
    values: list[float], *, seed: int
) -> dict[str, float | int | None]:
    if not values:
        return {"n_repetitions": 0, "mean": None, "lower_95": None, "upper_95": None}
    mean = statistics.mean(values)
    if len(values) < 2:
        return {
            "n_repetitions": len(values),
            "mean": mean,
            "lower_95": None,
            "upper_95": None,
        }
    rng = random.Random(seed)
    bootstrap_means = sorted(
        statistics.mean(rng.choices(values, k=len(values))) for _ in range(10_000)
    )
    return {
        "n_repetitions": len(values),
        "mean": mean,
        "lower_95": percentile(bootstrap_means, 2.5),
        "upper_95": percentile(bootstrap_means, 97.5),
    }


def aggregate_study(
    repetitions: list[dict[str, Any]],
    requests: list[dict[str, Any]],
    *,
    deadline_s: float,
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, float], list[dict[str, Any]]] = {}
    for item in repetitions:
        groups.setdefault((item["arrival"], item["rate_per_s"]), []).append(item)
    output: list[dict[str, Any]] = []
    for (arrival, rate), runs in sorted(groups.items()):
        selected = [
            row
            for row in requests
            if row["arrival"] == arrival and row["rate_per_s"] == rate
        ]
        metrics = {
            "on_time_success_fraction_offered": "on_time_success_fraction_offered",
            "rejection_fraction_offered": "rejection_fraction_offered",
            "technical_failure_fraction_admitted": "technical_failure_fraction_admitted",
        }
        intervals = {
            name: _bootstrap_mean_interval(
                [float(run[key]) for run in runs if run.get(key) is not None],
                seed=20261118 + len(output),
            )
            for name, key in metrics.items()
        }
        targets = {
            "on_time_success_fraction_offered": (0.95, "minimum"),
            "rejection_fraction_offered": (0.01, "maximum"),
            "technical_failure_fraction_admitted": (0.01, "maximum"),
        }
        observed_adverse_events = {
            "late_or_unsuccessful_offered": sum(
                max(0, int(run["offered"]) - int(run["on_time_successes"]))
                for run in runs
            ),
            "rejections": sum(int(run["rejected"]) for run in runs),
            "admitted_technical_failures": sum(
                int(run["failed"]) + int(run["timed_out"]) for run in runs
            ),
        }
        rare_event_for_metric = {
            "on_time_success_fraction_offered": "late_or_unsuccessful_offered",
            "rejection_fraction_offered": "rejections",
            "technical_failure_fraction_admitted": "admitted_technical_failures",
        }
        classifications: dict[str, str] = {}
        for name, (target, direction) in targets.items():
            interval = intervals[name]
            if interval["n_repetitions"] < 10 or interval["lower_95"] is None:
                state = "inconclusive_insufficient_repetitions"
            elif direction == "minimum":
                state = (
                    "supported_by_run_level_interval"
                    if float(interval["lower_95"]) >= target
                    else "below_target_in_sample"
                    if float(interval["upper_95"]) < target
                    else "inconclusive_interval_overlaps_target"
                )
            else:
                state = (
                    "supported_by_run_level_interval"
                    if float(interval["upper_95"]) <= target
                    else "above_target_in_sample"
                    if float(interval["lower_95"]) > target
                    else "inconclusive_interval_overlaps_target"
                )
            if (
                state == "supported_by_run_level_interval"
                and observed_adverse_events[rare_event_for_metric[name]] == 0
            ):
                state = "inconclusive_rare_event_evidence"
            classifications[name] = state
        if any(
            value == "above_target_in_sample" or value == "below_target_in_sample"
            for value in classifications.values()
        ):
            overall = "does_not_meet_all_targets_in_sample"
        elif all(
            value == "supported_by_run_level_interval"
            for value in classifications.values()
        ):
            overall = "targets_supported_by_run_level_intervals"
        elif "inconclusive_rare_event_evidence" in classifications.values():
            overall = "inconclusive_rare_event_evidence"
        else:
            overall = "inconclusive"
        successful_ms = [
            float(row["end_to_end_ms"])
            for row in selected
            if row["outcome"] == "success" and row["end_to_end_ms"] is not None
        ]
        output.append(
            {
                "arrival": arrival,
                "rate_per_s": rate,
                "n_repetitions": len(runs),
                "total_offered": sum(run["offered"] for run in runs),
                "intervals_across_repetition_level_metrics": intervals,
                "slo_metric_classification": classifications,
                "observed_adverse_event_counts": observed_adverse_events,
                "rare_event_note": "A zero observed adverse-event count is not treated as evidence that a 1% rare-event target is met.",
                "overall_slo_classification": overall,
                "pooled_successful_request_count": len(successful_ms),
                "pooled_successful_request_end_to_end_p95_ms": percentile(
                    successful_ms, 95
                ),
                "tail_estimate_note": "P95 pools requests only as a point estimate; repetitions, not requests, are the uncertainty unit.",
                "deadline_seconds": deadline_s,
            }
        )
    return output


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(len(ordered) * p / 100) - 1))]


async def _create_session(
    client: httpx.AsyncClient, base_url: str, case_id: str
) -> str:
    response = await client.post(
        urljoin(base_url.rstrip("/") + "/", "agent/sessions"), json={"case_id": case_id}
    )
    response.raise_for_status()
    return str(response.json()["conversation_id"])


async def _send_turn(
    client: httpx.AsyncClient,
    base_url: str,
    turn: PlannedTurn,
    conversation_id: str,
    origin: float,
    deadline_s: float,
    experiment_id: str,
) -> dict[str, Any]:
    scheduled = origin + turn.offset_s
    await asyncio.sleep(max(0.0, scheduled - time.perf_counter()))
    dispatched = time.perf_counter()
    outcome, reason = "failed", "no_result_event"
    result: dict[str, Any] = {}
    token_usage: dict[str, int] = {}
    admitted = False
    url = urljoin(
        base_url.rstrip("/") + "/", f"agent/sessions/{conversation_id}/turns/stream"
    )
    try:
        async with client.stream(
            "POST",
            url,
            json={
                "case_id": CASE_IDS[turn.task_class],
                "question": PROMPTS[turn.task_class],
            },
            headers={"X-Experiment-Id": experiment_id},
            timeout=httpx.Timeout(240.0),
        ) as response:
            if response.status_code != 200:
                outcome, reason = (
                    ("rejected", f"http_{response.status_code}")
                    if response.status_code == 429
                    else ("failed", f"http_{response.status_code}")
                )
            else:
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    event = json.loads(line[6:])
                    kind = event.get("type")
                    if kind == "admitted":
                        admitted = True
                    elif kind == "token_usage":
                        token_usage = {
                            "input_tokens": int(event.get("input_tokens", 0)),
                            "output_tokens": int(event.get("output_tokens", 0)),
                        }
                    elif kind == "result":
                        result = event["data"]
                        outcome, reason = "success", ""
                    elif kind == "error":
                        reason = str(
                            event.get("data", {}).get("reason", "agent_failure")
                        )
                        outcome = "rejected" if reason == "queue_full" else "failed"
    except httpx.TimeoutException:
        outcome, reason = "timed_out", "client_timeout"
    except httpx.HTTPError as exc:
        outcome, reason = "failed", type(exc).__name__
    completed = time.perf_counter()
    timing = result.get("timing_ms", {})
    return {
        "repetition": turn.repetition,
        "arrival": turn.arrival,
        "rate_per_s": turn.rate,
        "scheduled_offset_s": turn.offset_s,
        "dispatch_offset_s": dispatched - origin,
        "scheduler_lag_ms": max(0.0, (dispatched - scheduled) * 1000),
        "task_class": turn.task_class,
        "case_id": CASE_IDS[turn.task_class],
        "request_id": result.get("request_id"),
        "outcome": outcome,
        "reason": reason,
        "admitted": admitted or outcome == "success",
        "end_to_end_ms": (completed - scheduled) * 1000
        if outcome != "timed_out"
        else None,
        "queue_wait_ms": timing.get("queue_wait"),
        "processing_ms": timing.get("processing"),
        "model_calls": result.get("model_calls", 0),
        "tool_calls": result.get("tool_calls", 0),
        "input_tokens": result.get("input_tokens", token_usage.get("input_tokens")),
        "output_tokens": result.get("output_tokens", token_usage.get("output_tokens")),
        "within_deadline": outcome == "success"
        and (completed - scheduled) <= deadline_s,
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


async def run(args: argparse.Namespace) -> Path | None:
    base_url = args.base_url.rstrip("/")
    block_id = getattr(args, "block_id", None)
    order_in_block = getattr(args, "order_in_block", None)
    if block_id is not None and block_id < 1:
        raise ValueError("--block-id must be positive")
    if order_in_block is not None and order_in_block < 1:
        raise ValueError("--order-in-block must be positive")
    if args.hosting == "local" and urlsplit(base_url).hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        raise ValueError(
            "--hosting local requires a loopback URL such as http://127.0.0.1:8003"
        )
    turns = plan_turns(
        rates=args.rates,
        arrivals=args.arrivals,
        duration_s=args.duration_s,
        repetitions=args.repetitions,
        seed=args.seed,
    )
    if len(turns) > args.max_agent_turns:
        raise ValueError(
            f"Planned {len(turns)} turns exceeds --max-agent-turns {args.max_agent_turns}; "
            "reduce rates/duration/repetitions or explicitly raise the cap after cost review."
        )
    estimate = estimate_cost(
        turn_count=len(turns),
        max_model_calls=args.max_model_calls,
        input_tokens_per_call=args.input_tokens_per_call,
        output_tokens_per_call=args.output_tokens_per_call,
        input_price_per_million=args.input_price_per_million,
        output_price_per_million=args.output_price_per_million,
        task_runtime_hours=(args.task_runtime_hours if args.hosting == "fargate" else 0.0),
        hosting=args.hosting,
    )
    print(
        json.dumps(
            {
                "cost_scenario": estimate,
                "warning": "Scenario estimate, not a guaranteed bill cap; input tokens and actual call counts can vary.",
            },
            indent=2,
        )
    )
    if not args.execute:
        print(
            "Preflight only. No requests were sent. Add --execute --approve-cost-usd <ceiling> after reviewing the estimate."
        )
        return None
    if (
        args.approve_cost_usd is None
        or not math.isfinite(args.approve_cost_usd)
        or args.approve_cost_usd < float(estimate["scenario_subtotal_usd"])
    ):
        raise ValueError(
            "--execute requires --approve-cost-usd at least as large as the displayed scenario subtotal"
        )
    run_id = (
        args.experiment_id
        or f"aws-{datetime.now(UTC):%Y%m%dT%H%M%SZ}-{uuid4().hex[:8]}"
    )
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", run_id):
        raise ValueError(
            "experiment ID may contain only letters, digits, '.', '_' or '-' (max 80 chars)"
        )
    start_utc = datetime.now(UTC)
    timeout = httpx.Timeout(240.0)
    limits = httpx.Limits(max_connections=max(20, min(args.max_agent_turns, 200)))
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        health = await client.get(f"{base_url}/health")
        health.raise_for_status()
        if health.json().get("inference") != "bedrock":
            raise RuntimeError(
                "Target API health does not report inference=bedrock; refusing to run live AWS workload"
            )
        if health.json().get("model_id") != args.model_id:
            raise RuntimeError(
                "Target API model_id does not match the model used for pricing and CloudWatch collection"
            )
        service_response = await client.get(f"{base_url}/agent/metrics")
        service_response.raise_for_status()
        service_config = service_response.json()
        if service_config.get("offered", 0) or service_config.get("in_flight", 0):
            raise RuntimeError(
                "Target agent process has already handled requests; restart it for a clean experiment and full request budget"
            )
        out = Path(args.output_dir) / run_id
        out.mkdir(parents=True, exist_ok=False)
        case_sessions: dict[tuple[str, float, int, int], str] = {}
        for turn in turns:
            key = (turn.arrival, turn.rate, turn.repetition, turn.index)
            case_sessions[key] = await _create_session(
                client, base_url, CASE_IDS[turn.task_class]
            )
        rows: list[dict[str, Any]] = []
        summaries: list[dict[str, Any]] = []
        # Each repetition starts at a new origin. Its arrivals remain open-loop.
        groups: dict[tuple[str, float, int], list[PlannedTurn]] = {}
        for turn in turns:
            groups.setdefault((turn.arrival, turn.rate, turn.repetition), []).append(
                turn
            )
        ordered_groups = list(groups.items())
        random.Random(args.seed ^ 0x5A17).shuffle(ordered_groups)
        for condition_order, ((arrival, rate, repetition), group) in enumerate(
            ordered_groups, 1
        ):
            origin = time.perf_counter() + 0.25
            tasks = [
                asyncio.create_task(
                    _send_turn(
                        client,
                        base_url,
                        turn,
                        case_sessions[
                            (turn.arrival, turn.rate, turn.repetition, turn.index)
                        ],
                        origin,
                        args.deadline_s,
                        run_id,
                    )
                )
                for turn in group
            ]
            observed = await asyncio.gather(*tasks)
            rows.extend(observed)
            summaries.append(
                {
                    "arrival": arrival,
                    "rate_per_s": rate,
                    "repetition": repetition,
                    "block_id": block_id or repetition,
                    "condition_order": condition_order,
                    **summarize_repetition(observed, deadline_s=args.deadline_s),
                }
            )
    end_utc = datetime.now(UTC)
    cloudwatch = await asyncio.to_thread(
        collect_bedrock_metrics,
        start_utc,
        end_utc,
        args.model_id,
        args.aws_region,
        args.aws_profile,
    )
    manifest = {
        "source": (
            "AWS Bedrock agent via local FastAPI HTTP endpoint"
            if args.hosting == "local"
            else "AWS Bedrock agent via ECS Fargate HTTP endpoint"
        ),
        "hosting": args.hosting,
        "aws_calls": True,
        "bedrock_calls": True,
        "experiment_id": run_id,
        "block_id": block_id,
        "order_in_block": order_in_block,
        "target_host": base_url.split("/")[2],
        "model_id": args.model_id,
        "agent_service_configuration": {
            "concurrency_limit": service_config["concurrency_limit"],
            "queue_capacity": service_config["queue_capacity"],
            "scope": service_config["scope"],
        },
        "fargate_task_shape_assumption": (
            {
                "vcpus": 0.5,
                "memory_gib": 1,
                "desired_tasks": 1,
                "source": "agent-fargate.yaml defaults; verify deployed task before interpreting results",
            }
            if args.hosting == "fargate"
            else None
        ),
        "start_utc": start_utc.isoformat(),
        "end_utc": end_utc.isoformat(),
        "arrival_processes": args.arrivals,
        "rates_per_s": args.rates,
        "duration_s_per_condition": args.duration_s,
        "repetitions": args.repetitions,
        "condition_order_randomized": True,
        "seed": args.seed,
        "planned_turns": len(turns),
        "workload_mix": {"short": 0.4, "ordinary": 0.4, "tool_heavy": 0.2},
        "slo_targets": {
            "deadline_seconds": args.deadline_s,
            "on_time_success_fraction_offered_min": 0.95,
            "rejection_fraction_offered_max": 0.01,
            "technical_failure_fraction_admitted_max": 0.01,
        },
        "cost_scenario": estimate,
        "cost_scenario_not_hard_cap": True,
        "request_text_recorded": False,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
    (out / "study_summary.json").write_text(
        json.dumps(
            aggregate_study(summaries, rows, deadline_s=args.deadline_s), indent=2
        ),
        encoding="utf-8",
    )
    _write_csv(out / "requests.csv", rows)
    (out / "cloudwatch_bedrock_metrics.json").write_text(
        json.dumps(cloudwatch, indent=2), encoding="utf-8"
    )
    print(f"AWS Bedrock workload complete: {out.resolve()}")
    return out


def collect_bedrock_metrics(
    start_utc: datetime,
    end_utc: datetime,
    model_id: str,
    region: str,
    profile: str | None,
) -> dict[str, Any]:
    """Read provider-aggregated Bedrock metrics; unavailable access is recorded."""
    import boto3

    try:
        session = boto3.Session(profile_name=profile, region_name=region)
        client = session.client("cloudwatch")
        specs = {
            "Invocations": "Sum",
            "InvocationLatency": "Average",
            "InvocationClientErrors": "Sum",
            "InvocationServerErrors": "Sum",
            "InvocationThrottles": "Sum",
            "InputTokenCount": "Sum",
            "OutputTokenCount": "Sum",
        }
        response = client.get_metric_data(
            StartTime=start_utc,
            EndTime=end_utc,
            MetricDataQueries=[
                {
                    "Id": f"m{index}",
                    "Label": name,
                    "MetricStat": {
                        "Metric": {
                            "Namespace": "AWS/Bedrock",
                            "MetricName": name,
                            "Dimensions": [{"Name": "ModelId", "Value": model_id}],
                        },
                        "Period": 60,
                        "Stat": stat,
                    },
                    "ReturnData": True,
                }
                for index, (name, stat) in enumerate(specs.items())
            ],
            ScanBy="TimestampAscending",
        )
        return {
            "source": "AWS/Bedrock CloudWatch metric data, model-id dimension",
            "start_utc": start_utc.isoformat(),
            "end_utc": end_utc.isoformat(),
            "period_seconds": 60,
            "series": [
                {
                    "metric": item.get("Label"),
                    "timestamps": [
                        stamp.isoformat() for stamp in item.get("Timestamps", [])
                    ],
                    "values": item.get("Values", []),
                    "status_code": item.get("StatusCode"),
                    "messages": item.get("Messages", []),
                }
                for item in response.get("MetricDataResults", [])
            ],
            "messages": response.get("Messages", []),
            "limitation": "Provider metrics are aggregated by model and time period; they do not identify individual agent requests.",
            "error_metric_note": "ModelId-scoped error series may omit errors before Bedrock identifies a target model. Account-region aggregate series are not attributed to this experiment.",
        }
    except Exception as exc:  # noqa: BLE001 - preserve experiment artifact if read access is absent
        return {
            "source": "AWS/Bedrock CloudWatch metric data",
            "error": type(exc).__name__,
            "message": str(exc)[:400],
            "limitation": "Application request records remain available; native metrics could not be read by the configured AWS profile.",
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Bounded open-loop experiment against a deployed Bedrock agent."
    )
    parser.add_argument(
        "--base-url",
        required=True,
        help="Agent API URL; use loopback (127.0.0.1) with --hosting local",
    )
    parser.add_argument(
        "--hosting",
        choices=("local", "fargate"),
        default="fargate",
        help="Where the API runs; local excludes Fargate and public IPv4 charges",
    )
    parser.add_argument("--experiment-id")
    parser.add_argument(
        "--block-id",
        type=int,
        help="Matched factorial block number; used to combine one-repetition treatment runs",
    )
    parser.add_argument(
        "--order-in-block",
        type=int,
        help="Treatment position from the randomized factorial design file",
    )
    parser.add_argument(
        "--arrivals",
        nargs="+",
        choices=("deterministic", "poisson"),
        default=["deterministic", "poisson"],
    )
    parser.add_argument("--rates", nargs="+", type=float, default=[0.05, 0.1])
    parser.add_argument("--duration-s", type=float, default=60)
    parser.add_argument("--repetitions", type=int, default=10)
    parser.add_argument("--deadline-s", type=float, default=5)
    parser.add_argument("--max-agent-turns", type=int, default=200)
    parser.add_argument("--max-model-calls", type=int, default=3)
    parser.add_argument(
        "--input-tokens-per-call",
        type=int,
        default=10000,
        help="Conservative scenario assumption, not an enforced input-token limit",
    )
    parser.add_argument("--output-tokens-per-call", type=int, default=400)
    parser.add_argument(
        "--input-price-per-million", type=float, default=INPUT_PRICE_PER_MILLION
    )
    parser.add_argument(
        "--output-price-per-million", type=float, default=OUTPUT_PRICE_PER_MILLION
    )
    parser.add_argument("--task-runtime-hours", type=float, default=2.0)
    parser.add_argument("--model-id", default=MODEL_ID)
    parser.add_argument("--aws-region", default="us-east-1")
    parser.add_argument("--aws-profile")
    parser.add_argument("--seed", type=int, default=20261118)
    parser.add_argument("--output-dir", default="results/aws")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Send actual requests to Bedrock through the target agent API",
    )
    parser.add_argument(
        "--approve-cost-usd",
        type=float,
        help="Reviewed scenario ceiling; must cover the estimate and is not an AWS billing cap",
    )
    args = parser.parse_args()
    if (
        args.duration_s <= 0
        or args.repetitions < 1
        or args.max_agent_turns < 1
        or args.deadline_s <= 0
        or (args.hosting == "fargate" and args.task_runtime_hours <= 0)
    ):
        parser.error(
            "duration, repetitions, turn budget, deadline, and task runtime must be positive"
        )
    if any(not math.isfinite(rate) or rate <= 0 for rate in args.rates):
        parser.error("arrival rates must be finite and positive")
    try:
        asyncio.run(run(args))
    except (ValueError, RuntimeError, httpx.HTTPError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
