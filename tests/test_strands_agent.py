from __future__ import annotations

import pytest

from ai_systems_performance_lab.agent.strands_review import (
    BedrockStrandsReviewAgent,
    ReviewState,
    _tools_for,
)
from ai_systems_performance_lab.agent.workload import CreditCase


def test_case_bound_tools_compute_reproducible_results() -> None:
    state = ReviewState()
    tools = _tools_for(
        CreditCase("synthetic-1", 1_000_000, 200_000, 100_000, "complex"),
        state,
        max_tools=4,
    )

    facts = tools[0]()
    dscr = tools[1]()
    stress = tools[2]()
    policy = tools[3]()

    assert '"operating_income": 200000' in facts
    assert '"dscr": 2.0' in dscr
    assert '"stressed_dscr": 1.5' in stress
    assert '"advisory_outcome": "supports_review"' in policy
    assert [event.name for event in state.events] == [
        "get_case_facts",
        "calculate_dscr",
        "stress_dscr",
        "apply_policy",
    ]


def test_case_bound_tools_enforce_call_limit() -> None:
    tools = _tools_for(CreditCase("synthetic-1", 10, 2, 1), ReviewState(), max_tools=1)
    tools[0]()
    with pytest.raises(RuntimeError, match="tool-call limit"):
        tools[0]()


def test_optional_s3_policy_tool_is_bound_and_measured() -> None:
    from ai_systems_performance_lab.agent.aws_tools import S3PolicyReader

    class FakePolicyReader(S3PolicyReader):
        def __init__(self):
            self.bucket = "synthetic-policy-bucket"
            self.key = "demo/policy.txt"

        def read_policy(self):
            return "Synthetic thresholds"

    state = ReviewState()
    tools = _tools_for(
        CreditCase("synthetic-1", 10, 2, 1),
        state,
        max_tools=1,
        policy_reader=FakePolicyReader(),
    )
    assert tools[-1]() == "Synthetic thresholds"
    assert state.policy_read is True
    assert state.evidence == [
        "policy_source=s3://synthetic-policy-bucket/demo/policy.txt"
    ]
    assert state.events[0].name == "read_reference_policy"


def test_bedrock_backend_requires_model_id_before_startup() -> None:
    from ai_systems_performance_lab.api.main import create_app

    app = create_app(agent_backend="bedrock")
    with pytest.raises(RuntimeError, match="BEDROCK_MODEL_ID"):

        async def start():
            async with app.router.lifespan_context(app):
                pass

        import asyncio

        asyncio.run(start())


@pytest.mark.asyncio
async def test_strands_adapter_executes_bounded_tools_and_returns_model_summary(
    monkeypatch,
) -> None:
    from ai_systems_performance_lab.agent import strands_review

    class FakeHooks:
        def add_callback(self, _event_type, callback):
            self.callback = callback

    class FakeAgent:
        def __init__(self, **kwargs):
            self.tools = kwargs["tools"]
            self.hooks = FakeHooks()

        def __call__(self, _prompt):
            from strands.hooks import BeforeModelCallEvent

            self.hooks.callback(BeforeModelCallEvent(agent=self))
            for function in self.tools:
                function()
            return type(
                "Result",
                (),
                {"message": {"content": [{"text": "Evidence-backed summary."}]}},
            )()

    monkeypatch.setattr(strands_review, "Agent", FakeAgent)
    monkeypatch.setattr(strands_review, "BedrockModel", lambda **_kwargs: object())

    result = await BedrockStrandsReviewAgent(
        model_id="test-model", max_tools=4, max_turns=1
    ).run(CreditCase("synthetic-2", 1_000_000, 200_000, 100_000))

    assert result.execution_source == "strands_bedrock:test-model"
    assert result.summary == "Evidence-backed summary."
    assert result.dscr == 2.0
    assert [event.name for event in result.tool_events[:4]] == [
        "get_case_facts",
        "calculate_dscr",
        "stress_dscr",
        "apply_policy",
    ]
