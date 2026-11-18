from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from ai_systems_performance_lab.experiments import sizing as sizing_module
from ai_systems_performance_lab.experiments.reviewer import (
    _dispatch,
    _review_schema,
    _review_with_client,
)
from ai_systems_performance_lab.experiments.sizing import _candidate_report
from ai_systems_performance_lab.experiments.slo import SizingStudyRequest


def _rep(repetition: int, *, goodput: float, rejected: float, failed: float = 0.0) -> dict:
    return {
        "condition": "c2-k4",
        "repetition": repetition,
        "concurrency": 2,
        "queue_capacity": 4,
        "deadline_compliance_among_offered": goodput,
        "rejection_fraction": rejected,
        "admitted_failure_fraction": failed,
        "successful_goodput_rps": 1.0,
        "successful_end_to_end_ms": {"p95": 250.0},
        "timed_out": 0,
        "admitted": 1000,
        "offered": 1000,
        "success_within_deadline": round(goodput * 1000),
        "rejected": round(rejected * 1000),
        "failed": round(failed * 1000),
    }


def test_sizing_request_rejects_experiment_over_budget() -> None:
    with pytest.raises(ValidationError, match="600 seconds"):
        SizingStudyRequest(
            focus="concurrency",
            concurrency_candidates=[1, 2, 3, 4, 5],
            queue_candidates=[0, 1, 2, 3, 4],
            duration_s=30,
            repetitions=20,
        )


@pytest.mark.parametrize(
    ("focus", "overrides", "expected"),
    [
        ("concurrency", {"concurrency_candidates": [1, 3], "queue_capacity": 6}, [(1, 6, "poisson", 12), (3, 6, "poisson", 12)]),
        ("queue_capacity", {"queue_candidates": [0, 9], "concurrency": 3}, [(3, 0, "poisson", 12), (3, 9, "poisson", 12)]),
        ("rate", {"arrival_rate_candidates": [4, 7], "arrival": "deterministic"}, [(2, 4, "deterministic", 4), (2, 4, "deterministic", 7)]),
        ("arrival", {"arrival_candidates": ["deterministic", "poisson"], "arrival_rate_rps": 6}, [(2, 4, "deterministic", 6), (2, 4, "poisson", 6)]),
    ],
)
def test_selected_bottleneck_controls_only_slo_candidate_axis(monkeypatch, tmp_path, focus, overrides, expected) -> None:
    async def fake_run_once(condition, duration, seed, deadline, tool_ms, model_ms):
        row = {
            "outcome": "success", "admitted": True, "within_deadline": True,
            "dispatch_offset_s": 0.0, "completed_offset_s": 0.1,
            "end_to_end_ms": 100.0, "queue_wait_ms": 0.0, "processing_ms": 100.0,
            "model_calls": 1, "tool_calls": 1, "reason": "", "case_id": "synthetic",
        }
        server = {"offered": 1, "admitted": 1, "rejected": 0, "completed": 1, "failed": 0}
        return [row], [], {"server": server}

    monkeypatch.setattr(sizing_module, "_run_once", fake_run_once)
    request = SizingStudyRequest(focus=focus, duration_s=1, repetitions=2, **overrides)
    output = asyncio.run(sizing_module.run_sizing_study(request, tmp_path, lambda _: None, asyncio.Event()))
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    actual = [
        (condition["concurrency"], condition["queue_capacity"], condition["arrival"], condition["rate"])
        for condition in manifest["conditions"]
    ]
    assert actual == expected


def test_candidate_feasibility_uses_repetition_level_intervals() -> None:
    request = SizingStudyRequest(
        concurrency_candidates=[2], queue_candidates=[4], duration_s=5, repetitions=10
    )
    rows = [
        _rep(i, goodput=0.98 + (i % 3) * 0.003, rejected=0.001, failed=0.001)
        for i in range(10)
    ]
    result = _candidate_report(rows, "c2-k4", request)
    assert result["classification"] == "feasible"
    assert result["slo_metrics"]["deadline_goodput_fraction"]["repetition_count"] == 10
    assert result["slo_metrics"]["deadline_goodput_fraction"]["uncertainty_unit"] == "whole repetition"


def test_candidate_with_no_adverse_events_is_not_claimed_feasible() -> None:
    request = SizingStudyRequest(
        concurrency_candidates=[2], queue_candidates=[4], duration_s=5, repetitions=2
    )
    result = _candidate_report([_rep(i, goodput=1.0, rejected=0.0) for i in range(2)], "c2-k4", request)
    assert result["classification"] == "insufficient_repetitions"
    assert result["feasible"] is None


def test_zero_adverse_events_are_not_misreported_as_zero_uncertainty() -> None:
    request = SizingStudyRequest(
        concurrency_candidates=[2], queue_candidates=[4], duration_s=5, repetitions=10
    )
    result = _candidate_report(
        [_rep(i, goodput=1.0, rejected=0.0) for i in range(10)], "c2-k4", request
    )
    assert result["classification"] == "inconclusive_rare_event_evidence"
    assert result["feasible"] is None
    assert "no_rejections_observed" in result["rare_event_warning"]


def test_reviewer_tool_only_exposes_aggregate_saved_evidence() -> None:
    report = {
        "study_id": "sizing-abc",
        "provenance": "local simulated-agent HTTP measurements",
        "scope": "one process",
        "slo_profile": {"deadline_ms": 5000},
        "statistical_method": "whole repetition bootstrap",
        "candidate_results": [{"configuration_id": "c2-k4", "feasible": True}],
        "recommendation": {"configuration_id": "c2-k4"},
        "requests": [{"case_id": "must-not-leak", "prompt": "must-not-leak"}],
    }
    result = _dispatch("list_candidate_results", "{}", report)
    assert "must-not-leak" not in str(result)
    compared = _dispatch("compare_candidates", '{"candidate_a":"c2-k4","candidate_b":"missing"}', report)
    assert compared["error"] == "unknown_candidate_id"


def test_review_schema_limits_candidate_and_metric_references_to_report() -> None:
    schema = _review_schema({"candidate_results": [{"configuration_id": "k8"}]})
    valid = {
        "decision": "more_evidence_needed",
        "summary": "The sample is inconclusive.",
        "candidate_id": "k8",
        "findings": [{
            "title": "Rare events",
            "interpretation": "No rejection was observed in the available repetitions.",
            "confidence": "moderate",
            "evidence": [{"candidate_id": "k8", "metric": "rejection_fraction"}],
        }],
        "next_experiment": {
            "question": "Does the pattern persist with more repetitions?",
            "change": "Increase repetition count.",
            "hold_constant": ["arrival rate", "concurrency", "queue capacity"],
            "success_evidence": "Interval estimates can resolve the rare event target.",
        },
        "limitations": ["Local single-process measurements only."],
    }
    assert schema.model_validate(valid).candidate_id == "k8"
    valid["findings"][0]["evidence"][0]["candidate_id"] = "invented"
    with pytest.raises(ValidationError):
        schema.model_validate(valid)


def test_reviewer_skips_intermediate_commentary_and_parses_final_structured_answer() -> None:
    report = {
        "study_id": "sizing-test", "focus": "queue_capacity", "provenance": "local simulation",
        "scope": "one process", "slo_profile": {}, "statistical_method": "whole repetition bootstrap",
        "candidate_results": [{
            "configuration_id": "k8", "classification": "feasible",
            "slo_metrics": {
                "deadline_goodput_fraction": {"mean": 0.99},
                "rejection_fraction": {"mean": 0.0},
                "admitted_failure_fraction": {"mean": 0.0},
            },
            "diagnostics": {"mean_successful_goodput_rps": 4.0, "mean_of_per_repetition_successful_p95_ms": 200.0},
        }],
    }
    final_json = json.dumps({
        "decision": "candidate_for_next_experiment",
        "summary": "The selected local candidate met the configured targets in this sample.",
        "candidate_id": "k8",
        "findings": [{
            "title": "Deadline performance", "interpretation": "The measured interval clears the target.",
            "confidence": "moderate",
            "evidence": [{"candidate_id": "k8", "metric": "deadline_goodput_fraction"}],
        }],
        "next_experiment": {
            "question": "Does this hold at a higher offered rate?", "change": "Increase offered rate.",
            "hold_constant": ["arrival pattern", "service delays"], "success_evidence": "All target intervals still pass.",
        },
        "limitations": ["Local simulated-agent result only."],
    })
    function_call = SimpleNamespace(type="function_call", name="read_slo_targets", arguments="{}", call_id="call_1")
    tool_response = SimpleNamespace(id="resp_tool", usage=None, output=[function_call])
    commentary = SimpleNamespace(type="output_text", text="I will inspect the study first.")
    final_text = SimpleNamespace(type="output_text", text=final_json)
    intermediate_message = SimpleNamespace(type="message", phase="commentary", content=[commentary])
    final_message = SimpleNamespace(type="message", phase="final_answer", content=[final_text])
    final_response = SimpleNamespace(id="resp_final", usage=None, output=[intermediate_message, final_message])

    class FakeResponses:
        def __init__(self):
            self.calls = []

        async def create(self, **kwargs):
            self.calls.append(kwargs)
            return tool_response if len(self.calls) == 1 else final_response

    class FakeClient:
        def __init__(self):
            self.responses = FakeResponses()

    client = FakeClient()
    result = asyncio.run(_review_with_client(client, report))
    assert result["review"]["candidate_id"] == "k8"
    assert result["tool_calls"] == 1
    assert len(client.responses.calls) == 2
    assert "text" in client.responses.calls[0]


def test_sizing_study_persists_provenance_and_slo_report(monkeypatch, tmp_path) -> None:
    async def fake_run_once(condition, duration, seed, deadline, tool_ms, model_ms):
        row = {
            "outcome": "success", "admitted": True, "within_deadline": True,
            "dispatch_offset_s": 0.0, "completed_offset_s": 0.1,
            "end_to_end_ms": 100.0, "queue_wait_ms": 0.0, "processing_ms": 100.0,
            "model_calls": 1, "tool_calls": 1, "reason": "", "case_id": "synthetic",
        }
        server = {"offered": 1, "admitted": 1, "rejected": 0, "completed": 1, "failed": 0}
        return [row], [{"offset_s": 0.0, "offered": 1, "admitted": 1, "rejected": 0, "completed": 1, "active": 1, "waiting": 0}], {"server": server}

    monkeypatch.setattr(sizing_module, "_run_once", fake_run_once)
    request = SizingStudyRequest(
        concurrency_candidates=[1], queue_candidates=[0], duration_s=1, repetitions=2
    )
    output = asyncio.run(sizing_module.run_sizing_study(request, tmp_path, lambda _: None, asyncio.Event()))
    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    summary = json.loads((output / "summary.json").read_text(encoding="utf-8"))
    assert manifest["aws_calls"] is False
    assert manifest["bedrock_calls"] is False
    assert summary["sizing_study"]["candidate_results"][0]["classification"] == "insufficient_repetitions"
    assert (output / "requests.csv").exists()
    assert (output / "queue_occupancy.csv").exists()
