from __future__ import annotations

import argparse
import asyncio
import math

from ai_systems_performance_lab.experiments.aws_live import (
    aggregate_study,
    arrival_offsets,
    estimate_cost,
    plan_turns,
    run,
    summarize_repetition,
    workload_class,
)


def test_deterministic_and_poisson_schedules_are_reproducible() -> None:
    assert arrival_offsets("deterministic", 2, 2, 9) == [0.0, 0.5, 1.0, 1.5]
    assert arrival_offsets("poisson", 2, 10, 9) == arrival_offsets("poisson", 2, 10, 9)
    try:
        arrival_offsets("poisson", 0, 10, 9)
    except ValueError as exc:
        assert "positive" in str(exc)
    else:
        raise AssertionError("zero arrival rate must fail validation")


def test_planned_workload_has_fixed_mix_and_matched_seeded_schedule() -> None:
    turns = plan_turns(
        rates=[1], arrivals=["deterministic"], duration_s=10, repetitions=1, seed=2
    )
    assert len(turns) == 10
    assert [workload_class(i) for i in range(5)] == [
        "short",
        "ordinary",
        "short",
        "ordinary",
        "tool_heavy",
    ]
    assert [turn.offset_s for turn in turns] == [float(i) for i in range(10)]


def test_cost_scenario_includes_conservative_tokens_and_task_runtime() -> None:
    estimate = estimate_cost(
        turn_count=10,
        max_model_calls=3,
        input_tokens_per_call=10_000,
        output_tokens_per_call=400,
        input_price_per_million=0.33,
        output_price_per_million=2.75,
        task_runtime_hours=2,
    )
    assert estimate["scenario_input_tokens"] == 300_000
    assert estimate["scenario_output_tokens"] == 12_000
    assert math.isclose(estimate["scenario_bedrock_usd"], 0.132)
    assert math.isclose(
        float(estimate["scenario_subtotal_usd"]),
        float(estimate["scenario_bedrock_usd"])
        + float(estimate["scenario_fargate_and_ipv4_usd"]),
    )


def test_repetition_summary_uses_offered_denominator_and_admitted_failures() -> None:
    rows = [
        {"outcome": "success", "admitted": True, "end_to_end_ms": 4000},
        {"outcome": "success", "admitted": True, "end_to_end_ms": 6000},
        {"outcome": "rejected", "admitted": False, "end_to_end_ms": 1},
        {"outcome": "failed", "admitted": True, "end_to_end_ms": 3},
    ]
    summary = summarize_repetition(rows, deadline_s=5)
    assert summary["on_time_successes"] == 1
    assert summary["on_time_success_fraction_offered"] == 0.25
    assert summary["rejection_fraction_offered"] == 0.25
    assert summary["admitted"] == 3
    assert math.isclose(summary["technical_failure_fraction_admitted"], 1 / 3)
    assert summary["successful_end_to_end_ms"]["p95"] == 6000


def test_slo_study_requires_ten_repetitions_and_keeps_requests_clustered() -> None:
    runs = [
        {
            "arrival": "deterministic",
            "rate_per_s": 0.1,
            "offered": 10,
            "on_time_successes": 10,
            "rejected": 0,
            "failed": 0,
            "timed_out": 0,
            "on_time_success_fraction_offered": 1.0,
            "rejection_fraction_offered": 0.0,
            "technical_failure_fraction_admitted": 0.0,
        }
        for _ in range(9)
    ]
    requests = [
        {
            "arrival": "deterministic",
            "rate_per_s": 0.1,
            "outcome": "success",
            "end_to_end_ms": 100.0,
        }
        for _ in range(90)
    ]
    result = aggregate_study(runs, requests, deadline_s=5)[0]
    assert result["n_repetitions"] == 9
    assert result["overall_slo_classification"] == "inconclusive"
    assert result["pooled_successful_request_count"] == 90
    runs.append({**runs[0]})
    result = aggregate_study(runs, requests, deadline_s=5)[0]
    assert result["overall_slo_classification"] == "inconclusive_rare_event_evidence"
    assert result["observed_adverse_event_counts"]["rejections"] == 0


def test_slo_interval_requires_ten_valid_repetitions_for_each_metric() -> None:
    runs = [
        {
            "arrival": "deterministic",
            "rate_per_s": 0.1,
            "offered": 1,
            "on_time_successes": 1,
            "rejected": 0,
            "failed": 0,
            "timed_out": 0,
            "on_time_success_fraction_offered": 1.0,
            "rejection_fraction_offered": 0.0,
            "technical_failure_fraction_admitted": 0.0 if index < 9 else None,
        }
        for index in range(10)
    ]
    result = aggregate_study(runs, [], deadline_s=5)[0]
    assert (
        result["slo_metric_classification"]["technical_failure_fraction_admitted"]
        == "inconclusive_insufficient_repetitions"
    )
    assert result["overall_slo_classification"] == "inconclusive_rare_event_evidence"


def test_preflight_prints_cost_scenario_without_network_or_aws_calls(capsys) -> None:
    args = argparse.Namespace(
        base_url="http://127.0.0.1:1",
        hosting="local",
        rates=[0.1],
        arrivals=["deterministic"],
        duration_s=10,
        repetitions=1,
        seed=5,
        max_agent_turns=2,
        max_model_calls=3,
        input_tokens_per_call=10_000,
        output_tokens_per_call=400,
        input_price_per_million=0.33,
        output_price_per_million=2.75,
        task_runtime_hours=2,
        execute=False,
        approve_cost_usd=None,
    )
    result = asyncio.run(run(args))
    assert result is None
    output = capsys.readouterr().out
    assert '"planned_turns": 1' in output
    assert "No requests were sent" in output


def test_turn_budget_is_checked_before_any_live_request(capsys) -> None:
    args = argparse.Namespace(
        base_url="http://127.0.0.1:1",
        hosting="local",
        rates=[1.0],
        arrivals=["deterministic"],
        duration_s=10,
        repetitions=1,
        seed=5,
        max_agent_turns=2,
        max_model_calls=3,
        input_tokens_per_call=10_000,
        output_tokens_per_call=400,
        input_price_per_million=0.33,
        output_price_per_million=2.75,
        task_runtime_hours=2,
        execute=True,
        approve_cost_usd=100,
    )
    try:
        asyncio.run(run(args))
    except ValueError as exc:
        assert "exceeds --max-agent-turns" in str(exc)
    else:
        raise AssertionError("turn budget must be checked before network access")
