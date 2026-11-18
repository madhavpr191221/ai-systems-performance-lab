from __future__ import annotations

import asyncio
import time

from ai_systems_performance_lab.experiments.local_http import (
    Condition,
    _summarize,
    arrival_offsets,
    build_conditions,
    child_environment,
    create_open_loop_tasks,
    percentile,
    verify_accounting,
)


def test_deterministic_arrivals_have_exact_spacing() -> None:
    assert arrival_offsets("deterministic", 4, 1, 1) == [0, 0.25, 0.5, 0.75]


def test_poisson_schedule_is_seeded_and_has_exponential_variability() -> None:
    first = arrival_offsets("poisson", 10, 100, 24)
    assert first == arrival_offsets("poisson", 10, 100, 24)
    gaps = [b - a for a, b in zip([0, *first[:-1]], first)]
    assert 900 < len(first) < 1100
    assert max(gaps) > 0.2


def test_open_loop_dispatch_is_not_delayed_by_prior_completion() -> None:
    async def exercise() -> list[float]:
        dispatches: list[float] = []

        async def slow_send(index: int, target: float, dispatched: float) -> int:
            dispatches.append(dispatched)
            await asyncio.sleep(0.12)
            return index

        tasks = create_open_loop_tasks([0, 0.02, 0.04], time.perf_counter(), slow_send)
        await asyncio.gather(*tasks)
        return dispatches

    dispatches = asyncio.run(exercise())
    assert len(dispatches) == 3
    assert dispatches[2] - dispatches[0] < 0.1


def test_condition_matrix_has_eight_distinct_cells() -> None:
    conditions = build_conditions(10)
    assert len(conditions) == 8
    assert sum("arrivals" in item.groups for item in conditions) == 4
    assert {item.queue_capacity for item in conditions if "queue" in item.groups} == {
        0,
        16,
    }
    assert {
        item.concurrency for item in conditions if "concurrency" in item.groups
    } == {1, 4}


def test_percentiles_use_nearest_rank_and_preserve_empty() -> None:
    assert percentile([], 95) is None
    assert percentile([4, 1, 3, 2], 50) == 2
    assert percentile([4, 1, 3, 2], 95) == 4


def test_summary_counts_rejections_and_unfinished_as_offered() -> None:
    condition = Condition("cell", ("queue",), "poisson", 2, 1, 0)
    rows = [
        {
            "outcome": "success",
            "end_to_end_ms": 20,
            "completed_offset_s": 1,
            "within_deadline": True,
            "queue_wait_ms": 2,
            "processing_ms": 18,
            "dispatch_offset_s": 0.01,
        },
        {
            "outcome": "rejected",
            "end_to_end_ms": 30,
            "completed_offset_s": 1.2,
            "within_deadline": False,
            "queue_wait_ms": None,
            "processing_ms": None,
            "dispatch_offset_s": 0.02,
        },
        {
            "outcome": "failed",
            "end_to_end_ms": 10,
            "completed_offset_s": 1.1,
            "within_deadline": False,
            "queue_wait_ms": None,
            "processing_ms": None,
            "dispatch_offset_s": 0.03,
        },
        {
            "outcome": "unfinished",
            "end_to_end_ms": None,
            "completed_offset_s": None,
            "within_deadline": False,
            "queue_wait_ms": None,
            "processing_ms": None,
            "dispatch_offset_s": None,
        },
    ]
    result = _summarize(rows, {"offered": 2, "rejected": 1}, condition, 1)
    assert result["offered"] == 4
    assert (
        result["success"]
        == result["rejected"]
        == result["failed"]
        == result["unfinished"]
        == 1
    )
    assert result["rejection_fraction"] == 1 / 4
    assert result["deadline_compliance_among_offered"] == 1 / 4
    assert result["api_not_received"] == 1


def test_sample_path_service_accounting_includes_undispatched_schedule() -> None:
    rows = [
        {"outcome": "success", "dispatch_offset_s": 0.0},
        {"outcome": "rejected", "dispatch_offset_s": 0.1},
        {"outcome": "unfinished", "dispatch_offset_s": None},
    ]
    verify_accounting(
        rows,
        {
            "offered": 2,
            "admitted": 1,
            "rejected": 1,
            "completed": 1,
            "in_flight": 0,
        },
    )


def test_sample_path_accounting_rejects_conservation_error() -> None:
    try:
        verify_accounting(
            [{"outcome": "success", "dispatch_offset_s": 0.0}],
            {
                "offered": 1,
                "admitted": 1,
                "rejected": 0,
                "completed": 0,
                "in_flight": 0,
            },
        )
    except AssertionError as exc:
        assert "completed plus in-flight" in str(exc)
    else:
        raise AssertionError("unbalanced service accounting should fail")


def test_child_environment_forces_simulated_mode_and_removes_aws_configuration(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AGENT_BACKEND", "bedrock")
    monkeypatch.setenv("AWS_PROFILE", "personal-profile")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "never-copy")
    monkeypatch.setenv("BEDROCK_MODEL_ID", "model")
    env = child_environment(2, 4, 20, 30)
    assert env["AGENT_BACKEND"] == "simulated"
    assert env["LAB_CONCURRENCY"] == "2"
    assert not any(key.upper().startswith("AWS_") for key in env)
    assert "BEDROCK_MODEL_ID" not in env
