"""Reproducible synthetic case and evidence generation for the AWS demo."""

from __future__ import annotations

from typing import Any

import numpy as np

DEFAULT_SYNTHETIC_CASE_COUNT = 1_000
DEFAULT_SYNTHETIC_SEED = 20_261_117
DSCR_STRATA = ((0.65, 0.99), (1.00, 1.24), (1.25, 1.64), (1.65, 2.20))


def generate_synthetic_records(
    count: int = DEFAULT_SYNTHETIC_CASE_COUNT,
    seed: int = DEFAULT_SYNTHETIC_SEED,
) -> list[dict[str, Any]]:
    """Create internally consistent cases across policy-relevant DSCR ranges.

    The stratified DSCR values are a test-design choice, not an estimate of a
    real customer population. Each result contains a DynamoDB-shaped case and
    an evidence document suitable for the existing case-bound S3 reader.
    """
    if count < 0:
        raise ValueError("count must be non-negative")
    rng = np.random.default_rng(seed)
    if count == 0:
        return []

    stratum_order = rng.permutation(count)
    records: list[dict[str, Any]] = []
    for index in range(count):
        revenue = round(float(np.exp(rng.uniform(np.log(250_000), np.log(10_000_000)))))
        margin = float(rng.uniform(0.03, 0.25))
        operating_income = round(revenue * margin)
        complexity = "complex" if rng.random() < 0.30 else "standard"

        # Balanced strata ensure the generated set includes cases near both
        # policy thresholds without pretending the sample is population data.
        stratum = min(3, int(stratum_order[index] * 4 / count))
        low, high = DSCR_STRATA[stratum]
        target_dscr = float(rng.uniform(low, high))
        debt_service = max(1, round(operating_income / target_dscr))

        case_id = f"synthetic-case-{index + 1:06d}"
        margin_pct = 100 * operating_income / revenue
        records.append(
            {
                "case": {
                    "case_id": case_id,
                    "annual_revenue": revenue,
                    "operating_income": operating_income,
                    "annual_debt_service": debt_service,
                    "complexity": complexity,
                },
                "evidence": {
                    "evidence": [
                        {
                            "id": f"SYN-{index + 1:06d}-FIN-01",
                            "text": (
                                f"Synthetic annual revenue is {revenue:,} currency units. "
                                "Values are generated for demonstration only."
                            ),
                        },
                        {
                            "id": f"SYN-{index + 1:06d}-OPS-02",
                            "text": (
                                f"Synthetic operating income is {operating_income:,} "
                                f"currency units, equivalent to a {margin_pct:.2f} percent margin."
                            ),
                        },
                        {
                            "id": f"SYN-{index + 1:06d}-DEBT-03",
                            "text": (
                                f"Synthetic annual debt service is {debt_service:,} "
                                "currency units."
                            ),
                        },
                        {
                            "id": f"SYN-{index + 1:06d}-NOTE-04",
                            "text": (
                                f"The synthetic {complexity} workload profile is illustrative. "
                                "This example is not a real application or credit decision."
                            ),
                        },
                    ]
                },
            }
        )
    return records
