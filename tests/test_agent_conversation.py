from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from ai_systems_performance_lab.agent.aws_tools import (
    CaseNotFoundError,
    DynamoDBCaseRepository,
    InMemoryCaseRepository,
    S3EvidenceReader,
)
from ai_systems_performance_lab.agent.workload import (
    AgentTurn,
    CreditCase,
    UsageBudgetExceededError,
    load_demo_cases,
)
from ai_systems_performance_lab.api.main import create_app


class FakeBody:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        return self.payload[:size]

    def close(self) -> None:
        self.closed = True


def test_dynamodb_repository_uses_exact_key_and_decodes_attributes() -> None:
    class Client:
        def __init__(self) -> None:
            self.request = None

        def get_item(self, **kwargs):
            self.request = kwargs
            return {
                "Item": {
                    "case_id": {"S": "demo-case-01"},
                    "annual_revenue": {"N": "1000000"},
                    "operating_income": {"N": "200000"},
                    "annual_debt_service": {"N": "100000"},
                    "complexity": {"S": "complex"},
                }
            }

    client = Client()
    case = DynamoDBCaseRepository(
        table_name="synthetic-cases", region_name="us-east-1", client=client
    ).get_case("demo-case-01")

    assert case == CreditCase("demo-case-01", 1_000_000, 200_000, 100_000, "complex")
    assert client.request["Key"] == {"case_id": {"S": "demo-case-01"}}
    assert client.request["ConsistentRead"] is True


def test_dynamodb_repository_reports_missing_case() -> None:
    class Client:
        def get_item(self, **_kwargs):
            return {}

    with pytest.raises(CaseNotFoundError):
        DynamoDBCaseRepository(
            table_name="table", region_name="us-east-1", client=Client()
        ).get_case("missing")


def test_s3_evidence_reader_returns_ranked_citations_and_closes_body() -> None:
    class Client:
        def __init__(self) -> None:
            self.body = FakeBody(
                json.dumps(
                    {
                        "evidence": [
                            {"id": "A", "text": "Revenue is one million."},
                            {
                                "id": "B",
                                "text": "Operating income equals 200 thousand.",
                            },
                        ]
                    }
                ).encode()
            )
            self.request = None

        def get_object(self, **kwargs):
            self.request = kwargs
            return {"Body": self.body}

    client = Client()
    result = S3EvidenceReader(
        bucket="synthetic", region_name="us-east-1", client=client
    ).search(case_id="demo-case-01", query="operating income")

    assert result[0]["id"] == "B"
    assert client.request == {
        "Bucket": "synthetic",
        "Key": "cases/demo-case-01/evidence.json",
    }
    assert client.body.closed


@pytest.mark.asyncio
async def test_fixture_chat_stream_supports_follow_up_turns_and_case_bound_session() -> (
    None
):
    app = create_app(concurrency=1, queue_capacity=1, tool_delay_ms=0)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        session = await client.post("/agent/sessions", json={"case_id": "demo-case-01"})
        conversation_id = session.json()["conversation_id"]
        first = await client.post(
            f"/agent/sessions/{conversation_id}/turns/stream",
            json={"case_id": "demo-case-01", "question": "Calculate the ratio."},
        )
        second = await client.post(
            f"/agent/sessions/{conversation_id}/turns/stream",
            json={"case_id": "demo-case-01", "question": "Now explain the downside."},
        )
        metrics = (await client.get("/agent/metrics")).json()
        wrong_case = await client.post(
            f"/agent/sessions/{conversation_id}/turns/stream",
            json={"case_id": "demo-case-02", "question": "Try to cross cases."},
        )

    assert first.status_code == second.status_code == 200
    assert '"type":"tool_started"' in first.text
    assert '"execution_source":"fixture_no_llm"' in first.text
    assert '"model_calls":0' in first.text
    assert "Calculate the ratio" in first.text
    assert metrics["offered"] == metrics["completed"] == 2
    assert wrong_case.status_code == 400


@pytest.mark.asyncio
async def test_strands_agent_selects_tools_and_reuses_s3_session(monkeypatch) -> None:
    from ai_systems_performance_lab.agent import conversation

    sessions = []
    selected_tools = []
    callbacks = {}

    class FakeSessionManager:
        def __init__(self, **kwargs):
            sessions.append(kwargs)

    class FakeHooks:
        def add_callback(self, event_type, callback):
            callbacks[event_type] = callback

    class FakeResult:
        def __init__(self):
            self.message = {
                "content": [{"text": "The DSCR is 2.0, based on current case values."}]
            }
            self.metrics = type(
                "Metrics",
                (),
                {"accumulated_usage": {"inputTokens": 10, "outputTokens": 8}},
            )()

    class FakeAgent:
        def __init__(self, **kwargs):
            selected_tools.extend(kwargs["tools"])
            self.hooks = FakeHooks()

        async def stream_async(self, _prompt, *, limits):
            assert limits == {"turns": 6, "output_tokens": 900}
            from strands.hooks import AfterModelCallEvent, BeforeModelCallEvent

            callbacks[BeforeModelCallEvent](BeforeModelCallEvent(agent=self))
            # Deliberately call only one tool. The agent is not forced through
            # the full fixture sequence.
            await selected_tools[1]()
            callbacks[AfterModelCallEvent](AfterModelCallEvent(agent=self))
            yield {"result": FakeResult()}

    monkeypatch.setattr(conversation, "Agent", FakeAgent)
    monkeypatch.setattr(conversation, "BedrockModel", lambda **_kwargs: object())
    monkeypatch.setattr(conversation, "S3SessionManager", FakeSessionManager)

    cases = InMemoryCaseRepository(load_demo_cases())

    class Evidence:
        def search(self, **_kwargs):
            return []

    class Policy:
        bucket = "synthetic"
        key = "policy.txt"

        def read_policy(self):
            return "synthetic policy"

    class Memory:
        async def recall(self, **_kwargs):
            return []

        async def remember(self, **_kwargs):
            return None

    agent = conversation.BedrockConversationAgent(
        model_id="example-model",
        region_name="us-east-1",
        cases=cases,
        evidence=Evidence(),
        policy=Policy(),
        session_bucket="synthetic-bucket",
        memory=Memory(),
        max_requests_per_process=1,
    )
    turn = AgentTurn(
        "demo-case-01", "demo-case-01.0123456789abcdef0123456789abcdef", "Compute DSCR"
    )
    progress_events = []
    result = await agent.run(turn, progress=progress_events.append)
    assert result.answer.startswith("The DSCR is 2.0")
    assert result.tool_calls == 1
    assert result.model_calls == 1
    assert [tool.__name__ for tool in selected_tools] == [
        "get_case_facts",
        "calculate_dscr",
        "run_stress_scenario",
        "search_case_evidence",
        "read_reference_policy",
        "evaluate_synthetic_policy",
    ]
    assert sessions[0]["session_id"] == "demo-case-01_0123456789abcdef0123456789abcdef"
    assert any(
        event["type"] == "tool_started" and event["name"] == "calculate_dscr"
        for event in progress_events
    )
    assert any(event["type"] == "model_call_started" for event in progress_events)
    assert any(event["type"] == "token_usage" for event in progress_events)
    with pytest.raises(UsageBudgetExceededError):
        await agent.run(turn)
    assert len(sessions) == 1
