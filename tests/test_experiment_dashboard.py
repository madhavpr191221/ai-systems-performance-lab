from __future__ import annotations

import asyncio
import json

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from ai_systems_performance_lab.api.main import create_app
from ai_systems_performance_lab.experiments import service as experiment_service
from ai_systems_performance_lab.experiments.interactive import (
    ComparisonRequest,
    _paired_statistics,
    conditions_for,
    run_comparison,
)
from ai_systems_performance_lab.experiments.routes import _aggregate_time_series


def test_comparison_changes_only_selected_factor() -> None:
    request = ComparisonRequest(
        factor="queue_capacity",
        baseline_value=0,
        candidate_value=16,
        arrival="poisson",
        rate_rps=18,
        concurrency=3,
    )
    baseline, candidate = conditions_for(request)
    assert baseline.queue_capacity == 0
    assert candidate.queue_capacity == 16
    assert (baseline.arrival, baseline.rate, baseline.concurrency) == (
        candidate.arrival,
        candidate.rate,
        candidate.concurrency,
    )


@pytest.mark.parametrize(
    "values",
    [
        {"factor": "concurrency", "baseline_value": 1, "candidate_value": 17},
        {"factor": "queue_capacity", "baseline_value": -1, "candidate_value": 2},
        {"factor": "rate", "baseline_value": 1, "candidate_value": 51},
        {"factor": "rate", "baseline_value": 10.5, "candidate_value": 20},
        {"factor": "arrival", "baseline_value": "poisson", "candidate_value": "bursty"},
        {"factor": "queue_capacity", "baseline_value": 2, "candidate_value": 2},
    ],
)
def test_comparison_bounds_and_one_factor_values(values: dict) -> None:
    with pytest.raises(ValidationError):
        ComparisonRequest(**values)


def test_paired_statistics_use_repetitions_and_seeded_bootstrap() -> None:
    summaries = []
    for repetition in range(1, 11):
        for condition, offset in (("baseline", 0), ("candidate", 1)):
            summaries.append(
                {
                    "repetition": repetition,
                    "condition": condition,
                    "successful_goodput_rps": repetition + offset,
                    "rejection_fraction": 0.2 - offset * 0.01,
                    "deadline_compliance_among_offered": 0.7 + offset * 0.01,
                    "successful_end_to_end_ms": {"p95": 100 + offset * 10},
                    "successful_stage_ms": {"queue_wait": {"p95": 20 + offset * 5}},
                }
            )
    first = _paired_statistics(summaries, 52)
    second = _paired_statistics(summaries, 52)
    assert first == second
    assert first["successful_goodput_rps"]["mean_paired_difference"] == 1
    assert first["successful_end_to_end_p95_ms"]["mean_paired_difference"] == 10
    assert first["successful_goodput_rps"]["paired_repetitions"] == 10
    assert first["successful_goodput_rps"]["bootstrap_95_percent_interval"] == [1, 1]


def test_fewer_than_ten_pairs_do_not_get_interval() -> None:
    summaries = []
    for repetition in range(1, 4):
        for condition in ("baseline", "candidate"):
            summaries.append(
                {
                    "repetition": repetition,
                    "condition": condition,
                    "successful_goodput_rps": 1,
                    "rejection_fraction": 0,
                    "deadline_compliance_among_offered": 1,
                    "successful_end_to_end_ms": {"p95": 10},
                    "successful_stage_ms": {"queue_wait": {"p95": 0}},
                }
            )
    result = _paired_statistics(summaries, 3)
    assert result["successful_goodput_rps"]["paired_repetitions"] == 3
    assert result["successful_goodput_rps"]["bootstrap_95_percent_interval"] is None


def test_time_series_averages_within_repetition_before_across_repetitions() -> None:
    rows = [
        {"condition": "baseline", "repetition": "1", "offset_s": "0", "waiting": "0"},
        {"condition": "baseline", "repetition": "1", "offset_s": "0.05", "waiting": "2"},
        {"condition": "baseline", "repetition": "2", "offset_s": "0", "waiting": "4"},
    ]

    result = _aggregate_time_series(rows)

    assert result == [
        {"condition": "baseline", "offset_s": 0.0, "repetitions": 2, "waiting": 2.5}
    ]


def test_interactive_comparison_records_local_artifacts(tmp_path) -> None:
    request = ComparisonRequest(
        factor="queue_capacity",
        baseline_value=0,
        candidate_value=2,
        rate_rps=2,
        duration_s=1,
        repetitions=1,
        tool_delay_ms=0,
        model_delay_ms=0,
    )
    updates = []
    output = asyncio.run(
        run_comparison(request, tmp_path, updates.append, asyncio.Event())
    )
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert manifest["aws_calls"] is False
    assert manifest["bedrock_calls"] is False
    assert manifest["random_seeds"] == [request.seed]
    assert {row["condition"] for row in summary["conditions"]} == {
        "baseline",
        "candidate",
    }
    assert summary["paired_statistics"]["successful_goodput_rps"][
        "bootstrap_95_percent_interval"
    ] is None
    assert updates[-1]["completed_runs"] == 2
    assert (output / "requests.csv").exists()
    assert (output / "queue_occupancy.csv").exists()


def test_saved_run_api_lists_and_opens_legacy_artifact(tmp_path) -> None:
    run = tmp_path / "local" / "legacy-run-01"
    run.mkdir(parents=True)
    (run / "manifest.json").write_text(
        json.dumps({"source": "local fixture HTTP measurements", "aws_calls": False}),
        encoding="utf-8",
    )
    (run / "summary.json").write_text("[]", encoding="utf-8")
    (run / "queue_occupancy.csv").write_text(
        "offset_s,waiting\n0.0,0\n", encoding="utf-8"
    )
    (run / "requests.csv").write_text("outcome\nsuccess\n", encoding="utf-8")
    app = create_app(experiment_root=tmp_path)

    async def request_routes():
        async with app.router.lifespan_context(app), AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            listing = await client.get("/experiments/runs")
            detail = await client.get("/experiments/runs/legacy-run-01")
            state = await client.get("/agent/state")
            missing = await client.get("/experiments/runs/../README")
            return listing, detail, state, missing

    listing, detail, state, missing = asyncio.run(request_routes())
    assert listing.status_code == 200
    assert listing.json()[0]["run_id"] == "legacy-run-01"
    assert "simulated-agent" in listing.json()[0]["source"]
    assert listing.json()[0]["recorded_source"] == "local fixture HTTP measurements"
    assert detail.status_code == 200
    assert "simulated-agent" in detail.json()["manifest"]["display_source"]
    assert detail.json()["occupancy"] == [{"offset_s": "0.0", "waiting": "0"}]
    assert detail.json()["time_series"] == []
    assert state.json()["completed"] == 0
    assert missing.status_code in {404, 405}


def test_job_api_allows_one_active_job(monkeypatch, tmp_path) -> None:
    async def fake_comparison(request, root, progress, cancelled):
        await asyncio.sleep(0.05)
        return root / "local" / "fake-run"

    monkeypatch.setattr(experiment_service, "run_comparison", fake_comparison)
    app = create_app(experiment_root=tmp_path)

    async def request_routes():
        async with app.router.lifespan_context(app), AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            payload = {
                "factor": "queue_capacity",
                "baseline_value": 0,
                "candidate_value": 2,
                "duration_s": 1,
                "repetitions": 1,
            }
            started = await client.post("/experiments/jobs", json=payload)
            duplicate = await client.post("/experiments/jobs", json=payload)
            await asyncio.sleep(0.08)
            current = await client.get("/experiments/jobs/current")
            return started, duplicate, current

    started, duplicate, current = asyncio.run(request_routes())
    assert started.status_code == 202
    assert duplicate.status_code == 409
    assert current.json()["status"] == "completed"
    assert current.json()["run_id"] == "fake-run"


def test_slo_sizing_job_endpoint_is_bounded_and_tracks_run(monkeypatch, tmp_path) -> None:
    async def fake_study(request, root, progress, cancelled):
        progress({"completed_runs": 1, "total_runs": 1, "current": "c1-k0"})
        await asyncio.sleep(0.01)
        return root / "local" / "sizing-fake"

    monkeypatch.setattr(experiment_service, "run_sizing_study", fake_study)
    app = create_app(experiment_root=tmp_path)

    async def request_routes():
        async with app.router.lifespan_context(app), AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            payload = {
                "arrival_rate_rps": 2,
                "concurrency_candidates": [1],
                "queue_candidates": [0],
                "duration_s": 1,
                "repetitions": 2,
            }
            started = await client.post("/experiments/sizing-studies", json=payload)
            await asyncio.sleep(0.03)
            current = await client.get("/experiments/jobs/current")
            oversized = await client.post(
                "/experiments/sizing-studies",
                json={**payload, "duration_s": 30, "repetitions": 21},
            )
            return started, current, oversized

    started, current, oversized = asyncio.run(request_routes())
    assert started.status_code == 202
    assert started.json()["kind"] == "slo_sizing"
    assert current.json()["status"] == "completed"
    assert current.json()["run_id"] == "sizing-fake"
    assert oversized.status_code == 422


def test_architecture_review_is_explicit_and_persisted(monkeypatch, tmp_path) -> None:
    run = tmp_path / "local" / "sizing-review-test"
    run.mkdir(parents=True)
    (run / "manifest.json").write_text(
        json.dumps({"method": "matched seeded SLO sizing sweep"}), encoding="utf-8"
    )
    report = {"study_id": "sizing-review-test", "candidate_results": []}
    (run / "summary.json").write_text(json.dumps({"sizing_study": report}), encoding="utf-8")

    async def fake_review(input_report):
        assert input_report == report
        return {"review": {"recommendation": "inspect more runs"}, "tool_calls": 2}

    from ai_systems_performance_lab.experiments import reviewer

    monkeypatch.setattr(reviewer, "review_study", fake_review)
    app = create_app(experiment_root=tmp_path)

    async def request_route():
        async with app.router.lifespan_context(app), AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post(
                "/experiments/runs/sizing-review-test/architecture-review"
            )
            detail = await client.get("/experiments/runs/sizing-review-test")
            return response, detail

    response, detail = asyncio.run(request_route())
    assert response.status_code == 200
    assert response.json()["tool_calls"] == 2
    assert detail.json()["architecture_review"]["review"]["recommendation"] == "inspect more runs"
