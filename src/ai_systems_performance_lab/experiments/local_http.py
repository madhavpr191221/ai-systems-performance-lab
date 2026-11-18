"""Reproducible open-loop HTTP experiments against the simulated agent."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import random
import socket
import statistics
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx


@dataclass(frozen=True)
class Condition:
    name: str
    groups: tuple[str, ...]
    arrival: str
    rate: float
    concurrency: int
    queue_capacity: int


def arrival_offsets(
    kind: str, rate: float, duration_s: float, seed: int
) -> list[float]:
    """Return scheduled arrival offsets; Poisson times are exponential iid gaps."""
    if rate <= 0 or duration_s <= 0:
        raise ValueError("rate and duration_s must be positive")
    if kind == "deterministic":
        count = int(duration_s * rate)
        return [i / rate for i in range(count)]
    if kind != "poisson":
        raise ValueError("kind must be deterministic or poisson")
    rng = random.Random(seed)
    offsets: list[float] = []
    current = 0.0
    while True:
        current += rng.expovariate(rate)
        if current >= duration_s:
            break
        offsets.append(current)
    return offsets


def build_conditions(baseline_capacity: float) -> list[Condition]:
    cells: dict[str, Condition] = {}

    def add(name: str, group: str, arrival: str, rate: float, c: int, k: int) -> None:
        key = f"{name}|{arrival}|{rate:.8f}|{c}|{k}"
        previous = cells.get(key)
        groups = (group,) if previous is None else (*previous.groups, group)
        cells[key] = Condition(name, groups, arrival, rate, c, k)

    for rho in (0.7, 1.2):
        for kind in ("deterministic", "poisson"):
            add(f"rho_{rho}_{kind}", "arrivals", kind, rho * baseline_capacity, 2, 4)
    for k in (0, 16):
        add(f"queue_{k}", "queue", "poisson", 1.2 * baseline_capacity, 2, k)
    for c in (1, 4):
        add(f"concurrency_{c}", "concurrency", "poisson", 1.2 * baseline_capacity, c, 4)
    return list(cells.values())


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[
        max(0, min(len(ordered) - 1, int((p / 100) * len(ordered) + 0.999999) - 1))
    ]


def verify_accounting(rows: list[dict[str, Any]], server: dict[str, Any]) -> None:
    """Validate scheduled-request outcomes and process-level conservation."""
    outcome_names = {"success", "rejected", "failed", "timed_out", "unfinished"}
    if any(row["outcome"] not in outcome_names for row in rows):
        raise AssertionError("every scheduled turn must have one recognized outcome")
    dispatched = sum(row["dispatch_offset_s"] is not None for row in rows)
    if server["offered"] > dispatched:
        raise AssertionError(
            "API cannot receive more turns than the load generator dispatched"
        )
    if server["offered"] != server["admitted"] + server["rejected"]:
        raise AssertionError("API offered must equal admitted plus rejected")
    if server["admitted"] != server["completed"] + server["in_flight"]:
        raise AssertionError("API admitted must equal completed plus in-flight")


def child_environment(
    concurrency: int, queue_capacity: int, tool_ms: float, model_ms: float
) -> dict[str, str]:
    env = os.environ.copy()
    for key in list(env):
        if key.upper().startswith("AWS_") or key in {
            "BEDROCK_MODEL_ID",
            "DYNAMODB_CASES_TABLE",
            "AWS_DATA_BUCKET",
            "AGENT_SESSION_S3_BUCKET",
            "AGENTCORE_MEMORY_ID",
            "AWS_PROFILE",
        }:
            env.pop(key, None)
    env.update(
        {
            "AGENT_BACKEND": "simulated",
            "LAB_CONCURRENCY": str(concurrency),
            "LAB_QUEUE_CAPACITY": str(queue_capacity),
            "LAB_TOOL_DELAY_MS": str(tool_ms),
            "LAB_MODEL_DELAY_MS": str(model_ms),
            "SYNTHETIC_CASE_COUNT": "100",
        }
    )
    return env


def create_open_loop_tasks(
    offsets: list[float], origin: float, send: Any
) -> list[asyncio.Task[Any]]:
    """Create request tasks on scheduled arrival times, independent of prior responses."""

    async def schedule(index: int, offset: float) -> Any:
        target = origin + offset
        await asyncio.sleep(max(0.0, target - time.perf_counter()))
        return await send(index, target, time.perf_counter())

    return [
        asyncio.create_task(schedule(i, offset), name=f"arrival-{i}")
        for i, offset in enumerate(offsets)
    ]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


async def _wait_ready(
    client: httpx.AsyncClient, process: subprocess.Popen[Any]
) -> None:
    for _ in range(100):
        if process.poll() is not None:
            raise RuntimeError("simulated-agent API exited before becoming ready")
        try:
            response = await client.get("/health")
            if (
                response.status_code == 200
                and response.json().get("inference") == "simulated"
            ):
                return
        except httpx.HTTPError:
            pass
        await asyncio.sleep(0.1)
    raise TimeoutError("simulated-agent API did not become ready within 10 seconds")


class SimulatedAgentServer:
    def __init__(self, c: int, k: int, tool_ms: float, model_ms: float) -> None:
        self.port = _free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "ai_systems_performance_lab.api.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            cwd=Path(__file__).resolve().parents[3],
            env=child_environment(c, k, tool_ms, model_ms),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    async def close(self) -> None:
        if self.process.poll() is None:
            self.process.terminate()
            try:
                await asyncio.to_thread(self.process.wait, 5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                await asyncio.to_thread(self.process.wait)


async def _turn(
    client: httpx.AsyncClient,
    base: str,
    conversation_id: str,
    case_id: str,
    scheduled: float,
    dispatch: float,
    deadline_ms: float,
    origin: float,
) -> dict[str, Any]:
    outcome, body, service_ms, queue_wait_ms, model_calls, tool_calls = (
        "failed",
        "",
        None,
        None,
        0,
        0,
    )
    admitted = False
    try:
        async with client.stream(
            "POST",
            f"{base}/agent/sessions/{conversation_id}/turns/stream",
            json={
                "case_id": case_id,
                "question": "Summarize the synthetic case evidence.",
            },
            timeout=httpx.Timeout(15),
        ) as response:
            if response.status_code != 200:
                body = f"http_{response.status_code}"
            else:
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        event = json.loads(line[6:])
                        if event.get("type") == "admitted":
                            admitted = True
                        elif event.get("type") == "result":
                            outcome = "success"
                            service_ms = (
                                event["data"].get("timing_ms", {}).get("processing")
                            )
                            queue_wait_ms = (
                                event["data"].get("timing_ms", {}).get("queue_wait")
                            )
                            model_calls = event["data"].get("model_calls", 0)
                            tool_calls = event["data"].get("tool_calls", 0)
                        elif event.get("type") == "error":
                            reason = event.get("data", {}).get(
                                "reason", "agent_failure"
                            )
                            outcome = "rejected" if reason == "queue_full" else "failed"
                            body = reason
    except httpx.TimeoutException:
        outcome, body = "timed_out", "client_timeout"
    except httpx.HTTPError as exc:
        outcome, body = "failed", type(exc).__name__
    completed = time.perf_counter()
    end_to_end_ms = (completed - scheduled) * 1000
    return {
        "case_id": case_id,
        "scheduled_offset_s": scheduled - origin,
        "dispatch_offset_s": dispatch - origin,
        "scheduler_lag_ms": max(0.0, (dispatch - scheduled) * 1000),
        "outcome": outcome,
        "admitted": admitted or outcome == "success",
        "reason": body,
        "completed_offset_s": completed - origin,
        "end_to_end_ms": end_to_end_ms,
        "queue_wait_ms": queue_wait_ms,
        "processing_ms": service_ms,
        "model_calls": model_calls,
        "tool_calls": tool_calls,
        "within_deadline": outcome == "success" and end_to_end_ms <= deadline_ms,
    }


async def _run_once(
    condition: Condition,
    duration: float,
    seed: int,
    deadline_ms: float,
    tool_ms: float,
    model_ms: float,
    baseline_request_count: int = 0,
    capacity_hint: float | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    server = SimulatedAgentServer(
        condition.concurrency, condition.queue_capacity, tool_ms, model_ms
    )
    client = httpx.AsyncClient(
        base_url=server.base_url,
        limits=httpx.Limits(max_connections=256, max_keepalive_connections=128),
    )
    try:
        await _wait_ready(client, server.process)
        case_resp = await client.get("/agent/cases")
        case_ids = [record["case_id"] for record in case_resp.json()]
        # The baseline run estimates capacity; each experiment process gets two
        # unmeasured sequential warmups unless it is itself the baseline run.
        baseline: list[dict[str, Any]] = []
        request_count = baseline_request_count or 2
        for i in range(request_count):
            case_id = case_ids[(seed + i) % len(case_ids)]
            session = (
                await client.post("/agent/sessions", json={"case_id": case_id})
            ).json()
            now = time.perf_counter()
            row = await _turn(
                client,
                server.base_url,
                session["conversation_id"],
                case_id,
                now,
                now,
                deadline_ms,
                now,
            )
            baseline.append(row)
        processing = [
            float(row["processing_ms"])
            for row in baseline
            if row["processing_ms"] is not None
        ]
        estimated_capacity = capacity_hint or condition.concurrency / (
            statistics.mean(processing) / 1000
        )
        offsets = arrival_offsets(condition.arrival, condition.rate, duration, seed)
        sessions = []
        for i in range(len(offsets)):
            case_id = case_ids[(seed + i) % len(case_ids)]
            session = (
                await client.post("/agent/sessions", json={"case_id": case_id})
            ).json()
            sessions.append((case_id, session["conversation_id"]))
        metrics_before = (await client.get("/agent/metrics")).json()
        state_before = (await client.get("/agent/state")).json()
        rows: list[dict[str, Any]] = []
        dispatches: dict[int, float] = {}
        occupancy: list[dict[str, Any]] = []
        t0 = time.perf_counter()
        stop = asyncio.Event()

        async def poll_state() -> None:
            while not stop.is_set():
                try:
                    state = (await client.get("/agent/state", timeout=2)).json()
                    for key in ("offered", "admitted", "rejected", "completed"):
                        state[key] -= state_before[key]
                    occupancy.append({"offset_s": time.perf_counter() - t0, **state})
                except httpx.HTTPError:
                    pass
                try:
                    await asyncio.wait_for(stop.wait(), 0.25)
                except TimeoutError:
                    continue

        poller = asyncio.create_task(poll_state())

        async def send(i: int, target: float, dispatched: float) -> None:
            dispatches[i] = dispatched
            case_id, conv = sessions[i]
            rows.append(
                await _turn(
                    client,
                    server.base_url,
                    conv,
                    case_id,
                    target,
                    dispatched,
                    deadline_ms,
                    t0,
                )
            )

        tasks = create_open_loop_tasks(offsets, t0, send)
        if tasks:
            done, pending = await asyncio.wait(tasks, timeout=duration + 10)
            if pending:
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
                done_names = {task.get_name() for task in done}
                for i, offset in enumerate(offsets):
                    if f"arrival-{i}" not in done_names:
                        case_id, _ = sessions[i]
                        dispatched = dispatches.get(i)
                        rows.append(
                            {
                                "case_id": case_id,
                                "scheduled_offset_s": offset,
                                "dispatch_offset_s": dispatched - t0
                                if dispatched is not None
                                else None,
                                "scheduler_lag_ms": max(
                                    0, (dispatched - (t0 + offset)) * 1000
                                )
                                if dispatched is not None
                                else None,
                                "outcome": "unfinished",
                                "admitted": False,
                                "reason": "drain_limit",
                                "completed_offset_s": None,
                                "end_to_end_ms": (time.perf_counter() - (t0 + offset))
                                * 1000,
                                "queue_wait_ms": None,
                                "processing_ms": None,
                                "model_calls": 0,
                                "tool_calls": 0,
                                "within_deadline": False,
                            }
                        )
        stop.set()
        await poller
        final_state = (await client.get("/agent/state")).json()
        for key in ("offered", "admitted", "rejected", "completed"):
            final_state[key] -= state_before[key]
        occupancy.append({"offset_s": time.perf_counter() - t0, **final_state})
        metrics = (await client.get("/agent/metrics")).json()
        for key in (
            "offered",
            "admitted",
            "rejected",
            "completed",
            "succeeded",
            "failed",
        ):
            metrics[key] -= metrics_before[key]
        # Service quantiles include setup warmups; request-level samples below
        # are the experiment population used for latency summaries.
        metrics.pop("latency_ms", None)
        metrics["estimated_capacity_rps"] = estimated_capacity
        metrics["observed_mean_processing_ms"] = statistics.mean(processing)
        verify_accounting(rows, metrics)
        return (
            rows,
            occupancy,
            {"server": metrics, "baseline": baseline, "scheduled_count": len(offsets)},
        )
    finally:
        await client.aclose()
        await server.close()


def _summarize(
    rows: list[dict[str, Any]], server: dict[str, Any], condition: Condition, rep: int
) -> dict[str, Any]:
    counts = {
        key: sum(row["outcome"] == key for row in rows)
        for key in ("success", "rejected", "failed", "timed_out", "unfinished")
    }
    latencies = [row["end_to_end_ms"] for row in rows if row["outcome"] == "success"]
    queue_wait = [
        row["queue_wait_ms"]
        for row in rows
        if row["outcome"] == "success" and row["queue_wait_ms"] is not None
    ]
    processing = [
        row["processing_ms"]
        for row in rows
        if row["outcome"] == "success" and row["processing_ms"] is not None
    ]
    completed_times = [
        row["completed_offset_s"]
        for row in rows
        if row["completed_offset_s"] is not None
    ]
    elapsed = max(completed_times, default=0)
    return {
        "condition": condition.name,
        "groups": list(condition.groups),
        "repetition": rep,
        "arrival": condition.arrival,
        "offered_rate_rps": condition.rate,
        "concurrency": condition.concurrency,
        "queue_capacity": condition.queue_capacity,
        "offered": len(rows),
        "admitted": int(server.get("admitted", sum(bool(row.get("admitted")) for row in rows))),
        "dispatched": sum(row["dispatch_offset_s"] is not None for row in rows),
        "not_dispatched": sum(row["dispatch_offset_s"] is None for row in rows),
        **counts,
        "rejection_fraction": counts["rejected"] / len(rows) if rows else 0,
        "api_rejection_fraction": server["rejected"] / server["offered"]
        if server["offered"]
        else 0,
        "api_not_received": max(
            0,
            sum(row["dispatch_offset_s"] is not None for row in rows)
            - server["offered"],
        ),
        "successful_goodput_rps": counts["success"] / elapsed if elapsed else 0,
        "deadline_goodput_rps": sum(row["within_deadline"] for row in rows) / elapsed
        if elapsed
        else 0,
        "deadline_compliance_among_offered": sum(row["within_deadline"] for row in rows)
        / len(rows)
        if rows
        else 0,
        "success_within_deadline": sum(bool(row["within_deadline"]) for row in rows),
        "admitted_failure_fraction": sum(
            bool(row.get("admitted")) and row["outcome"] in {"failed", "timed_out"}
            for row in rows
        ) / max(1, int(server.get("admitted", sum(bool(row.get("admitted")) for row in rows)))),
        "successful_end_to_end_ms": {
            f"p{p}": percentile(latencies, p) for p in (50, 95, 99)
        },
        "successful_latency_sample_count": len(latencies),
        "successful_stage_ms": {
            stage: {f"p{p}": percentile(values, p) for p in (50, 95, 99)}
            for stage, values in (
                ("queue_wait", queue_wait),
                ("processing", processing),
            )
        },
        "server": server,
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _plot(path: Path, summaries: list[dict[str, Any]]) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    names = list(dict.fromkeys(item["condition"] for item in summaries))
    means = [
        statistics.mean(
            item["successful_end_to_end_ms"]["p95"] or 0
            for item in summaries
            if item["condition"] == name
        )
        for name in names
    ]
    rejects = [
        statistics.mean(
            item["rejection_fraction"]
            for item in summaries
            if item["condition"] == name
        )
        for name in names
    ]
    fig, axes = plt.subplots(
        2, 1, figsize=(max(9, len(names) * 1.1), 7), constrained_layout=True
    )
    axes[0].bar(names, means, color="#227c83")
    axes[0].set_ylabel("Successful end-to-end P95 (ms)")
    axes[1].bar(names, rejects, color="#c05a61")
    axes[1].set_ylabel("Rejected / offered")
    axes[1].set_ylim(0, 1)
    for ax in axes:
        ax.tick_params(axis="x", labelrotation=30)
    fig.savefig(path, dpi=140)
    plt.close(fig)


async def run(args: argparse.Namespace) -> Path:
    # One baseline run at c=2 establishes the normalization rate before the matrix.
    baseline_condition = Condition("baseline", (), "deterministic", 1, 2, 4)
    _, _, base_info = await _run_once(
        baseline_condition,
        0.01,
        args.seed,
        args.deadline_ms,
        args.tool_delay_ms,
        args.model_delay_ms,
        baseline_request_count=10,
    )
    baseline_capacity = base_info["server"]["estimated_capacity_rps"]
    conditions = build_conditions(baseline_capacity)
    if args.scenario != "all":
        conditions = [item for item in conditions if args.scenario in item.groups]
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + f"-{os.getpid()}"
    out = Path(args.output_dir) / run_id
    out.mkdir(parents=True, exist_ok=False)
    summaries: list[dict[str, Any]] = []
    request_rows: list[dict[str, Any]] = []
    queue_rows: list[dict[str, Any]] = []
    for condition in conditions:
        for rep in range(args.repetitions):
            rows, occupancy, info = await _run_once(
                condition,
                args.duration_s,
                args.seed + rep,
                args.deadline_ms,
                args.tool_delay_ms,
                args.model_delay_ms,
                capacity_hint=baseline_capacity,
            )
            summaries.append(_summarize(rows, info["server"], condition, rep + 1))
            request_rows.extend(
                {"condition": condition.name, "repetition": rep + 1, **row}
                for row in rows
            )
            queue_rows.extend(
                {"condition": condition.name, "repetition": rep + 1, **row}
                for row in occupancy
            )
    manifest = {
        "source": "local simulated-agent HTTP measurements",
        "aws_calls": False,
        "bedrock_calls": False,
        "arrival_deadline_ms": args.deadline_ms,
        "deadline_is_diagnostic": True,
        "duration_s_per_cell": args.duration_s,
        "repetitions": args.repetitions,
        "seed": args.seed,
        "baseline_capacity_rps": baseline_capacity,
        "baseline_processing_ms": base_info["server"]["observed_mean_processing_ms"],
        "baseline_sample_size": len(base_info["baseline"]),
        "warmup_turns_per_experiment_process": 2,
        "drain_limit_s": 10,
        "max_http_connections": 256,
        "tool_delay_ms": args.tool_delay_ms,
        "model_delay_ms": args.model_delay_ms,
        "conditions": [asdict(item) for item in conditions],
        "created_utc": run_id,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summaries, indent=2), encoding="utf-8")
    _write_csv(out / "requests.csv", request_rows)
    _write_csv(out / "queue_occupancy.csv", queue_rows)
    _write_csv(out / "baseline_requests.csv", base_info["baseline"])
    _plot(out / "comparison.png", summaries)
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run local open-loop experiments against the simulated agent."
    )
    parser.add_argument(
        "--scenario", choices=("all", "arrivals", "queue", "concurrency"), default="all"
    )
    parser.add_argument("--duration-s", type=float, default=30)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument(
        "--deadline-ms",
        type=float,
        default=500,
        help="Exploratory diagnostic deadline, not a production SLO",
    )
    parser.add_argument("--tool-delay-ms", type=float, default=20)
    parser.add_argument("--model-delay-ms", type=float, default=30)
    parser.add_argument("--seed", type=int, default=20261117)
    parser.add_argument("--output-dir", default="results/local")
    args = parser.parse_args()
    if (
        args.duration_s <= 0
        or args.repetitions < 1
        or args.deadline_ms <= 0
        or args.tool_delay_ms < 0
        or args.model_delay_ms < 0
    ):
        parser.error(
            "duration/deadline must be positive, repetitions >= 1, and delays nonnegative"
        )
    output = asyncio.run(run(args))
    print(f"Simulated-agent experiment complete: {output.resolve()}")


if __name__ == "__main__":
    main()
