from __future__ import annotations

import math

from ai_systems_performance_lab.experiments.factorial_live import build_study_plan


def test_live_factorial_plan_is_reproducible_and_costs_all_treatments() -> None:
    first = build_study_plan(
        blocks=10,
        seed=31,
        rate=0.1,
        duration_s=50,
        max_turns=250,
        max_model_calls=3,
        input_tokens_per_call=10_000,
        output_tokens_per_call=400,
    )
    second = build_study_plan(
        blocks=10,
        seed=31,
        rate=0.1,
        duration_s=50,
        max_turns=250,
        max_model_calls=3,
        input_tokens_per_call=10_000,
        output_tokens_per_call=400,
    )
    assert first == second
    assert len(first["schedule"]) == 40
    assert first["planned_turns"] == sum(
        row["planned_turns"] for row in first["schedule"]
    )
    assert first["cost_scenario"]["maximum_model_calls"] == first["planned_turns"] * 3
    assert first["cost_scenario"]["scenario_fargate_and_ipv4_usd"] == 0
    assert float(first["cost_scenario"]["scenario_subtotal_usd"]) < 3
    for block in range(1, 11):
        rows = [row for row in first["schedule"] if row["block"] == block]
        assert len({row["seed"] for row in rows}) == 1
        assert len({row["planned_turns"] for row in rows}) == 1


def test_live_factorial_plan_checks_budget_and_positive_inputs() -> None:
    try:
        build_study_plan(
            blocks=10,
            seed=1,
            rate=1,
            duration_s=30,
            max_turns=20,
            max_model_calls=3,
            input_tokens_per_call=10_000,
            output_tokens_per_call=400,
        )
    except ValueError as exc:
        assert "exceed --max-agent-turns" in str(exc)
    else:
        raise AssertionError("workload above cap should be rejected during preflight")

    try:
        build_study_plan(
            blocks=10,
            seed=1,
            rate=math.inf,
            duration_s=30,
            max_turns=200,
            max_model_calls=3,
            input_tokens_per_call=10_000,
            output_tokens_per_call=400,
        )
    except ValueError as exc:
        assert "rate" in str(exc)
    else:
        raise AssertionError("non-finite arrival rate should be rejected")

    try:
        build_study_plan(
            blocks=10,
            seed=20261118,
            rate=0.1,
            duration_s=30,
            max_turns=200,
            max_model_calls=3,
            input_tokens_per_call=10_000,
            output_tokens_per_call=400,
        )
    except ValueError as exc:
        assert "no arrivals" in str(exc)
    else:
        raise AssertionError("a block with no arrivals should fail preflight")
