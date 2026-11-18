"""Run a blocked capacity experiment against a local API using real Bedrock.

The API is restarted for every randomized treatment row so each run gets the
requested concurrency/queue settings and starts with empty service counters.
The wrapper previews the total study cost and requires an explicit ceiling.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from ai_systems_performance_lab.experiments.aws_live import (
    INPUT_PRICE_PER_MILLION,
    MODEL_ID,
    OUTPUT_PRICE_PER_MILLION,
    arrival_offsets,
    estimate_cost,
)
from ai_systems_performance_lab.experiments.aws_live import (
    run as run_workload,
)
from ai_systems_performance_lab.experiments.factorial import randomized_blocked_design


def _assert_port_available(host: str, port: int) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError as exc:
            raise RuntimeError(
                f"API port {port} on {host} is already in use; choose another port. "
                "The launcher will not stop an unrelated process."
            ) from exc


async def _wait_for_api(process: subprocess.Popen[Any], base_url: str) -> None:
    deadline = time.monotonic() + 45
    async with httpx.AsyncClient(timeout=1.0) as client:
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(
                    f"API exited during startup (code {process.returncode})"
                )
            try:
                response = await client.get(f"{base_url}/health")
                if response.status_code == 200:
                    body = response.json()
                    if body.get("inference") != "bedrock":
                        raise RuntimeError(
                            "API health does not report inference=bedrock"
                        )
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.25)
    raise RuntimeError("API did not become healthy within 45 seconds")


def _stop_api(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
            process.wait(timeout=15)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def _start_api(
    args: argparse.Namespace, env: dict[str, str], log_stream: Any
) -> subprocess.Popen[Any]:
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "ai_systems_performance_lab.api.main:app",
            "--host",
            args.api_host,
            "--port",
            str(args.api_port),
        ],
        cwd=args.project_root,
        env=env,
        stdout=log_stream,
        stderr=subprocess.STDOUT,
        creationflags=flags,
    )


def build_study_plan(
    *,
    blocks: int,
    seed: int,
    rate: float,
    duration_s: float,
    max_turns: int,
    max_model_calls: int,
    input_tokens_per_call: int,
    output_tokens_per_call: int,
    input_price_per_million: float = INPUT_PRICE_PER_MILLION,
    output_price_per_million: float = OUTPUT_PRICE_PER_MILLION,
) -> dict[str, Any]:
    design = randomized_blocked_design(blocks=blocks, seed=seed)
    for value, label in ((rate, "rate"), (duration_s, "duration")):
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{label} must be finite and positive")
    for row in design["schedule"]:
        row["planned_turns"] = len(
            arrival_offsets("poisson", rate, duration_s, int(row["seed"]))
        )
        row["workload_seed"] = row["seed"]
    empty_blocks = sorted(
        row["block"] for row in design["schedule"] if row["planned_turns"] == 0
    )
    if empty_blocks:
        raise ValueError(
            f"The planned Poisson workload has no arrivals in block(s) {empty_blocks}; "
            "increase --duration-s or --rate before running so every block can be compared."
        )
    planned = sum(row["planned_turns"] for row in design["schedule"])
    if planned > max_turns:
        raise ValueError(
            f"Planned {planned} turns exceed --max-agent-turns {max_turns}; "
            "reduce blocks/rate/duration or review a higher limit and cost scenario."
        )
    estimate = estimate_cost(
        turn_count=planned,
        max_model_calls=max_model_calls,
        input_tokens_per_call=input_tokens_per_call,
        output_tokens_per_call=output_tokens_per_call,
        input_price_per_million=input_price_per_million,
        output_price_per_million=output_price_per_million,
        task_runtime_hours=0,
        hosting="local",
    )
    return {
        **design,
        "workload": {
            "arrival": "poisson",
            "rate_per_s": rate,
            "duration_s": duration_s,
            "fixed_task_mix": {"short": 0.4, "ordinary": 0.4, "tool_heavy": 0.2},
        },
        "planned_turns": planned,
        "agent_limits": {
            "maximum_model_calls_per_request": max_model_calls,
            "maximum_output_tokens_per_call": output_tokens_per_call,
            "input_tokens_per_call_for_cost_estimate_only": input_tokens_per_call,
        },
        "cost_scenario": estimate,
        "cost_scenario_not_hard_cap": True,
    }


async def execute_study(args: argparse.Namespace) -> Path | None:
    plan = build_study_plan(
        blocks=args.blocks,
        seed=args.seed,
        rate=args.rate,
        duration_s=args.duration_s,
        max_turns=args.max_agent_turns,
        max_model_calls=args.max_model_calls,
        input_tokens_per_call=args.input_tokens_per_call,
        output_tokens_per_call=args.output_tokens_per_call,
        input_price_per_million=args.input_price_per_million,
        output_price_per_million=args.output_price_per_million,
    )
    estimate = plan["cost_scenario"]
    print(
        json.dumps(
            {
                "design": plan["design"],
                "blocks": plan["blocks"],
                "factors": plan["factors"],
                "workload": plan["workload"],
                "planned_turns": plan["planned_turns"],
                "agent_limits": plan["agent_limits"],
                "cost_scenario": estimate,
                "warning": "Scenario estimate, not an AWS bill cap.",
            },
            indent=2,
        )
    )
    if not args.execute:
        print(
            "Preflight only. No API process was started and no AWS requests were sent."
        )
        return None
    if (
        args.approve_cost_usd is None
        or not math.isfinite(args.approve_cost_usd)
        or args.approve_cost_usd < float(estimate["scenario_subtotal_usd"])
    ):
        raise ValueError(
            "--execute requires --approve-cost-usd at least as large as the total study scenario"
        )
    if os.getenv("AGENT_BACKEND", "").strip().casefold() != "bedrock":
        raise RuntimeError(
            "Set AGENT_BACKEND=bedrock in the environment before execution"
        )
    _assert_port_available(args.api_host, args.api_port)

    run_root = Path(args.output_dir)
    run_root.mkdir(parents=True, exist_ok=True)
    study_id = (
        args.study_id or f"capacity-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
    )
    study_dir = run_root / study_id
    study_dir.mkdir(parents=True, exist_ok=False)
    (study_dir / "design.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    ledger_path = study_dir / "execution.json"
    ledger: dict[str, Any] = {
        "study_id": study_id,
        "status": "running",
        "trials": [{**row, "status": "pending"} for row in plan["schedule"]],
    }

    def save_ledger() -> None:
        ledger_path.write_text(json.dumps(ledger, indent=2), encoding="utf-8")

    save_ledger()
    base_url = f"http://{args.api_host}:{args.api_port}"
    profile = args.aws_profile or os.getenv("AWS_PROFILE")
    region = args.aws_region or os.getenv("AWS_REGION", "us-east-1")
    completed: list[str] = []

    try:
        for row_index, row in enumerate(plan["schedule"]):
            trial = ledger["trials"][row_index]
            row_id = (
                f"b{row['block']:02d}-o{row['order_in_block']}-"
                f"c{row['concurrency']}-k{row['queue_capacity']}"
            )
            trial["run_id"] = f"{study_id}-{row_id}"
            trial["started_utc"] = datetime.now(UTC).isoformat()
            trial["status"] = "running"
            save_ledger()
            env = os.environ.copy()
            env.update(
                {
                    "AGENT_BACKEND": "bedrock",
                    "LAB_CONCURRENCY": str(row["concurrency"]),
                    "LAB_QUEUE_CAPACITY": str(row["queue_capacity"]),
                    "AGENT_MAX_REQUESTS_PER_PROCESS": str(row["planned_turns"]),
                    "AGENT_MAX_MODEL_TURNS": str(args.max_model_calls),
                    "AGENT_MAX_OUTPUT_TOKENS": str(args.output_tokens_per_call),
                }
            )
            log_path = study_dir / f"{row_id}-api.log"
            with log_path.open("w", encoding="utf-8") as log_stream:
                process = _start_api(args, env, log_stream)
                try:
                    await _wait_for_api(process, base_url)
                    per_run = estimate_cost(
                        turn_count=row["planned_turns"],
                        max_model_calls=args.max_model_calls,
                        input_tokens_per_call=args.input_tokens_per_call,
                        output_tokens_per_call=args.output_tokens_per_call,
                        input_price_per_million=args.input_price_per_million,
                        output_price_per_million=args.output_price_per_million,
                        task_runtime_hours=0,
                        hosting="local",
                    )
                    workload_args = argparse.Namespace(
                        base_url=base_url,
                        hosting="local",
                        experiment_id=f"{study_id}-{row_id}",
                        block_id=row["block"],
                        order_in_block=row["order_in_block"],
                        arrivals=["poisson"],
                        rates=[args.rate],
                        duration_s=args.duration_s,
                        repetitions=1,
                        deadline_s=args.deadline_s,
                        max_agent_turns=max(args.max_agent_turns, row["planned_turns"]),
                        max_model_calls=args.max_model_calls,
                        input_tokens_per_call=args.input_tokens_per_call,
                        output_tokens_per_call=args.output_tokens_per_call,
                        input_price_per_million=args.input_price_per_million,
                        output_price_per_million=args.output_price_per_million,
                        task_runtime_hours=0,
                        model_id=args.model_id,
                        aws_region=region,
                        aws_profile=profile,
                        seed=row["workload_seed"],
                        output_dir=str(study_dir / "runs"),
                        execute=True,
                        approve_cost_usd=float(per_run["scenario_subtotal_usd"]),
                    )
                    result_dir = await run_workload(workload_args)
                    completed.append(str(result_dir))
                    trial["status"] = "completed"
                    trial["run_dir"] = str(result_dir)
                    trial["completed_utc"] = datetime.now(UTC).isoformat()
                    save_ledger()
                    print(f"Completed {row_id}: {result_dir}", flush=True)
                except BaseException as exc:
                    trial["status"] = (
                        "interrupted"
                        if isinstance(exc, KeyboardInterrupt)
                        else "failed"
                    )
                    trial["error_type"] = type(exc).__name__
                    trial["completed_utc"] = datetime.now(UTC).isoformat()
                    save_ledger()
                    raise
                finally:
                    _stop_api(process)
    finally:
        ledger["status"] = (
            "complete"
            if all(trial["status"] == "completed" for trial in ledger["trials"])
            else "incomplete"
        )
        ledger["completed_runs"] = completed
        ledger["updated_utc"] = datetime.now(UTC).isoformat()
        save_ledger()
    analysis_rows = []
    for run_dir in completed:
        run_path = Path(run_dir)
        manifest = json.loads((run_path / "manifest.json").read_text(encoding="utf-8"))
        for summary in json.loads(
            (run_path / "summary.json").read_text(encoding="utf-8")
        ):
            summary["block_id"] = manifest["block_id"]
            summary["concurrency"] = manifest["agent_service_configuration"][
                "concurrency_limit"
            ]
            summary["queue_capacity"] = manifest["agent_service_configuration"][
                "queue_capacity"
            ]
            analysis_rows.append(summary)
    from ai_systems_performance_lab.experiments.factorial import analyze_factorial

    analysis = analyze_factorial(analysis_rows)
    (study_dir / "analysis.json").write_text(
        json.dumps(analysis, indent=2), encoding="utf-8"
    )
    print(f"Study complete: {study_dir.resolve()}")
    return study_dir


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run a randomized 2x2 capacity study against the real local Bedrock agent."
    )
    parser.add_argument("--blocks", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20261118)
    parser.add_argument("--rate", type=float, default=0.1)
    parser.add_argument("--duration-s", type=float, default=50)
    parser.add_argument("--deadline-s", type=float, default=5)
    parser.add_argument("--max-agent-turns", type=int, default=250)
    parser.add_argument("--max-model-calls", type=int, default=3)
    parser.add_argument("--input-tokens-per-call", type=int, default=10000)
    parser.add_argument("--output-tokens-per-call", type=int, default=400)
    parser.add_argument(
        "--input-price-per-million", type=float, default=INPUT_PRICE_PER_MILLION
    )
    parser.add_argument(
        "--output-price-per-million", type=float, default=OUTPUT_PRICE_PER_MILLION
    )
    parser.add_argument("--model-id", default=MODEL_ID)
    parser.add_argument("--aws-region")
    parser.add_argument("--aws-profile")
    parser.add_argument("--api-host", default="127.0.0.1")
    parser.add_argument("--api-port", type=int, default=8011)
    parser.add_argument("--study-id")
    parser.add_argument("--output-dir", default="results/aws/factorial")
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path(__file__).resolve().parents[3],
        help="Directory containing pyproject.toml and the project's .venv",
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--approve-cost-usd", type=float)
    args = parser.parse_args()
    if not 1 <= args.api_port <= 65535 or not args.api_host:
        parser.error("API host and port are invalid")
    try:
        asyncio.run(execute_study(args))
    except KeyboardInterrupt:
        print(
            "Study interrupted. See execution.json for completed and in-progress trials."
        )
    except (ValueError, RuntimeError, httpx.HTTPError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
