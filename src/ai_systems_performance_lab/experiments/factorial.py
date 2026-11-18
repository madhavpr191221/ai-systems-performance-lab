"""Small blocked 2x2 factorial designs for agent capacity studies.

This module plans and summarizes experiments; it never contacts AWS or an agent.
The block (usually a matched random seed) is the unit used for uncertainty.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

CONCURRENCY_LEVELS = (1, 4)
QUEUE_LEVELS = (0, 8)
CELLS = tuple((c, q) for c in CONCURRENCY_LEVELS for q in QUEUE_LEVELS)


def randomized_blocked_design(
    *, blocks: int = 10, seed: int = 20261118
) -> dict[str, Any]:
    """Return a reproducible complete-block 2x2 design in randomized order."""
    if blocks < 2:
        raise ValueError("Use at least two complete blocks to estimate run variation")
    rng = random.Random(seed)
    schedule: list[dict[str, int]] = []
    for block in range(1, blocks + 1):
        order = list(CELLS)
        rng.shuffle(order)
        schedule.extend(
            {
                "block": block,
                "order_in_block": order_index,
                "concurrency": concurrency,
                "queue_capacity": queue_capacity,
                "seed": seed + block,
            }
            for order_index, (concurrency, queue_capacity) in enumerate(order, 1)
        )
    return {
        "schema_version": 1,
        "design": "complete_block_2x2_factorial",
        "factors": {
            "concurrency": list(CONCURRENCY_LEVELS),
            "queue_capacity": list(QUEUE_LEVELS),
        },
        "blocks": blocks,
        "seed": seed,
        "randomized_within_each_block": True,
        "experimental_unit": "one independently restarted API process and one workload repetition",
        "blocking_variable": "matched workload seed; all four settings in a block use the same seed",
        "schedule": schedule,
    }


def _bootstrap_mean_interval(
    values: list[float], *, seed: int, resamples: int = 10_000
) -> dict[str, float | None]:
    if len(values) < 2:
        return {"lower": None, "upper": None}
    rng = random.Random(seed)
    means = sorted(
        statistics.mean(rng.choices(values, k=len(values))) for _ in range(resamples)
    )
    return {
        "lower": means[int(0.025 * (resamples - 1))],
        "upper": means[int(0.975 * (resamples - 1))],
    }


def analyze_factorial(
    rows: list[dict[str, Any]],
    *,
    response: str = "on_time_success_fraction_offered",
    bootstrap_seed: int = 20261118,
) -> dict[str, Any]:
    """Estimate paired 2x2 main effects and interaction from block-level outcomes.

    Each row requires ``block``, ``concurrency``, ``queue_capacity`` and the
    selected numeric response. The response should be a run-level summary (for
    example, on-time successes divided by offered requests), not a request row.
    """
    by_block: dict[int, dict[tuple[int, int], float]] = defaultdict(dict)
    for row in rows:
        block = int(row["block"])
        cell = (int(row["concurrency"]), int(row["queue_capacity"]))
        if cell not in CELLS:
            raise ValueError(f"Unexpected factor setting in block {block}: {cell}")
        value = row.get(response)
        if value is None:
            continue
        value = float(value)
        if not math.isfinite(value):
            raise ValueError(f"{response} must be finite")
        if not 0 <= value <= 1 and "fraction_" in response:
            raise ValueError(f"{response} must be between 0 and 1")
        if cell in by_block[block]:
            raise ValueError(f"Duplicate setting {cell} in block {block}")
        by_block[block][cell] = value

    complete: list[tuple[int, dict[tuple[int, int], float]]] = [
        (block, cells)
        for block, cells in sorted(by_block.items())
        if set(cells) == set(CELLS)
    ]
    effect_values: dict[str, list[float]] = {
        "concurrency": [],
        "queue_capacity": [],
        "concurrency_x_queue_capacity": [],
    }
    for _, y in complete:
        y11 = y[(4, 8)]
        y10 = y[(4, 0)]
        y01 = y[(1, 8)]
        y00 = y[(1, 0)]
        effect_values["concurrency"].append(((y10 + y11) - (y00 + y01)) / 2)
        effect_values["queue_capacity"].append(((y01 + y11) - (y00 + y10)) / 2)
        effect_values["concurrency_x_queue_capacity"].append(
            ((y11 + y00) - (y10 + y01)) / 2
        )

    effects: dict[str, Any] = {}
    for index, (name, values) in enumerate(effect_values.items()):
        effects[name] = {
            "mean_effect": statistics.mean(values) if values else None,
            "block_effects": values,
            "descriptive_95_percent_bootstrap_interval": _bootstrap_mean_interval(
                values, seed=bootstrap_seed + index
            ),
        }
    return {
        "schema_version": 1,
        "design": "complete_block_2x2_factorial",
        "response": response,
        "blocks_supplied": len(by_block),
        "complete_blocks": len(complete),
        "incomplete_blocks_excluded": sorted(set(by_block) - {b for b, _ in complete}),
        "effects": effects,
        "interpretation": (
            "Effects are differences in the selected run-level response between factor settings. "
            "Intervals resample complete blocks; they are descriptive and do not establish an SLO guarantee."
        ),
    }


def load_run_directories(paths: list[Path]) -> list[dict[str, Any]]:
    """Join each saved run's treatment configuration to its run summaries."""
    rows: list[dict[str, Any]] = []
    for path in paths:
        manifest = json.loads((path / "manifest.json").read_text(encoding="utf-8"))
        config = manifest["agent_service_configuration"]
        for summary in json.loads((path / "summary.json").read_text(encoding="utf-8")):
            rows.append(
                {
                    "block": int(summary.get("block_id", summary["repetition"])),
                    "concurrency": int(config["concurrency_limit"]),
                    "queue_capacity": int(config["queue_capacity"]),
                    "on_time_success_fraction_offered": summary.get(
                        "on_time_success_fraction_offered"
                    ),
                    "rejection_fraction_offered": summary.get(
                        "rejection_fraction_offered"
                    ),
                    "technical_failure_fraction_admitted": summary.get(
                        "technical_failure_fraction_admitted"
                    ),
                }
            )
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Plan or analyze a blocked 2x2 agent capacity experiment."
    )
    parser.add_argument("--blocks", type=int, default=10)
    parser.add_argument("--seed", type=int, default=20261118)
    parser.add_argument("--output", type=Path, default=Path("results/aws/design.json"))
    parser.add_argument(
        "--analyze", nargs="*", type=Path, help="Saved AWS run directories"
    )
    parser.add_argument(
        "--response",
        default="on_time_success_fraction_offered",
        choices=(
            "on_time_success_fraction_offered",
            "rejection_fraction_offered",
            "technical_failure_fraction_admitted",
        ),
    )
    args = parser.parse_args()
    if args.analyze is not None:
        result = analyze_factorial(
            load_run_directories(args.analyze), response=args.response
        )
    else:
        result = randomized_blocked_design(blocks=args.blocks, seed=args.seed)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"Wrote {args.output.resolve()}")


if __name__ == "__main__":
    main()
