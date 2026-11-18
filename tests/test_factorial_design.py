from __future__ import annotations

from ai_systems_performance_lab.experiments.factorial import (
    analyze_factorial,
    randomized_blocked_design,
)


def test_blocked_design_is_reproducible_and_contains_each_cell_once() -> None:
    first = randomized_blocked_design(blocks=4, seed=19)
    assert first == randomized_blocked_design(blocks=4, seed=19)
    assert len(first["schedule"]) == 16
    for block in range(1, 5):
        cells = [
            (row["concurrency"], row["queue_capacity"])
            for row in first["schedule"]
            if row["block"] == block
        ]
        assert set(cells) == {(1, 0), (1, 8), (4, 0), (4, 8)}


def test_factorial_analysis_uses_complete_blocks_and_recovers_effects() -> None:
    rows = []
    # Each block has a +0.2 concurrency effect, +0.1 queue effect,
    # and +0.05 interaction on the chosen response scale.
    for block, shift in ((1, 0.0), (2, 0.01), (3, -0.01)):
        values = {
            (1, 0): 0.5 + shift,
            (4, 0): 0.7 + shift,
            (1, 8): 0.6 + shift,
            (4, 8): 0.95 + shift,
        }
        rows.extend(
            {
                "block": block,
                "concurrency": concurrency,
                "queue_capacity": queue,
                "on_time_success_fraction_offered": value,
            }
            for (concurrency, queue), value in values.items()
        )
    rows.append(
        {
            "block": 4,
            "concurrency": 1,
            "queue_capacity": 0,
            "on_time_success_fraction_offered": 0.4,
        }
    )
    result = analyze_factorial(rows)
    assert result["complete_blocks"] == 3
    assert result["incomplete_blocks_excluded"] == [4]
    assert abs(result["effects"]["concurrency"]["mean_effect"] - 0.275) < 1e-12
    assert abs(result["effects"]["queue_capacity"]["mean_effect"] - 0.175) < 1e-12


def test_factorial_analysis_rejects_duplicate_cells_in_a_block() -> None:
    rows = [
        {
            "block": 1,
            "concurrency": 1,
            "queue_capacity": 0,
            "on_time_success_fraction_offered": 0.5,
        },
        {
            "block": 1,
            "concurrency": 1,
            "queue_capacity": 0,
            "on_time_success_fraction_offered": 0.6,
        },
    ]
    try:
        analyze_factorial(rows)
    except ValueError as exc:
        assert "Duplicate setting" in str(exc)
    else:
        raise AssertionError("duplicate treatment rows must be rejected")
