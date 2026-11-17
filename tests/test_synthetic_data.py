from __future__ import annotations

import json
import sys

import pytest
from botocore.exceptions import ClientError
from httpx import ASGITransport, AsyncClient

from ai_systems_performance_lab.agent.synthetic_data import (
    DEFAULT_SYNTHETIC_SEED,
    generate_synthetic_records,
)
from ai_systems_performance_lab.api.main import classify_provider_error, create_app
from scripts import seed_aws_demo


def test_synthetic_records_are_reproducible_and_valid() -> None:
    first = generate_synthetic_records(1000, DEFAULT_SYNTHETIC_SEED)
    second = generate_synthetic_records(1000, DEFAULT_SYNTHETIC_SEED)
    assert first == second
    assert first != generate_synthetic_records(1000, DEFAULT_SYNTHETIC_SEED + 1)
    assert len({row["case"]["case_id"] for row in first}) == 1000

    dscr_values = []
    for row in first:
        case = row["case"]
        assert case["annual_revenue"] > case["operating_income"] > 0
        assert case["annual_debt_service"] > 0
        assert case["complexity"] in {"standard", "complex"}
        dscr_values.append(case["operating_income"] / case["annual_debt_service"])
        evidence = row["evidence"]["evidence"]
        assert len(evidence) == 4
        assert len({item["id"] for item in evidence}) == 4
        assert all("synthetic" in item["text"].casefold() for item in evidence)

    assert sum(value < 1.0 for value in dscr_values) >= 245
    assert sum(1.0 <= value < 1.25 for value in dscr_values) >= 245
    assert sum(1.25 <= value < 1.65 for value in dscr_values) >= 245
    assert sum(value >= 1.65 for value in dscr_values) >= 245


def test_dataset_keeps_golden_cases_and_has_expected_s3_keys() -> None:
    cases, objects = seed_aws_demo.load_dataset(generated_count=3, seed=7)
    assert len(cases) == 5
    assert [case["case_id"] for case in cases[:2]] == [
        "demo-case-01",
        "demo-case-02",
    ]
    assert len(objects) == 6
    assert "cases/synthetic-case-000001/evidence.json" in objects
    assert seed_aws_demo.POLICY_KEY in objects
    generated_evidence = json.loads(
        objects["cases/synthetic-case-000001/evidence.json"]
    )
    assert len(generated_evidence["evidence"]) == 4


def test_dry_run_does_not_construct_aws_session(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "seed_aws_demo.py",
            "--table",
            "cases",
            "--bucket",
            "private-demo-bucket",
            "--generated-count",
            "0",
        ],
    )

    def forbidden_session(*_args, **_kwargs):
        pytest.fail("dry-run must not initialize an AWS session")

    monkeypatch.setattr(seed_aws_demo.boto3, "Session", forbidden_session)
    assert seed_aws_demo.main() == 0
    output = capsys.readouterr().out
    assert "2 DynamoDB records and 3 S3 objects" in output
    assert "no AWS API writes were made" in output


@pytest.mark.parametrize(
    ("code", "message", "expected"),
    [
        (
            "AccessDeniedException",
            "Your account is currently being verified.",
            "provider_account_verification",
        ),
        ("AccessDeniedException", "Not authorized", "provider_access_denied"),
        ("ThrottlingException", "Slow down", "provider_throttled"),
        ("ServiceUnavailableException", "Try later", "provider_unavailable"),
    ],
)
def test_provider_errors_are_classified_without_exposing_raw_messages(
    code: str, message: str, expected: str
) -> None:
    error = ClientError(
        {"Error": {"Code": code, "Message": message}}, "ConverseStream"
    )
    assert classify_provider_error(error) == expected


@pytest.mark.asyncio
async def test_local_case_selector_exposes_reproducible_generated_ids() -> None:
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.get("/agent/cases")

    cases = response.json()
    assert len(cases) == 1002
    assert cases[0]["case_id"] == "demo-case-01"
    assert cases[2]["case_id"] == "synthetic-case-000001"
