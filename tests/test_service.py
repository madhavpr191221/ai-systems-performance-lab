import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from ai_systems_performance_lab.admission.service import (
    BoundedReviewService,
    QueueFullError,
)
from ai_systems_performance_lab.agent.workload import (
    CreditCase,
    ReviewResult,
    SyntheticCreditReviewAgent,
)
from ai_systems_performance_lab.api.main import create_app


def case(case_id: str = "case-1") -> CreditCase:
    return CreditCase(
        case_id=case_id,
        annual_revenue=1_000_000,
        operating_income=200_000,
        annual_debt_service=100_000,
        complexity="standard",
    )


class GatedAgent:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run(self, request: CreditCase) -> ReviewResult:
        self.started.set()
        await self.release.wait()
        return ReviewResult(
            case_id=request.case_id,
            recommendation="supports_review",
            dscr=1.5,
            stressed_dscr=1.2,
            evidence=(),
            tool_events=(),
        )


@pytest.mark.asyncio
async def test_finite_queue_rejects_overflow_and_conserves_requests() -> None:
    agent = GatedAgent()
    service = BoundedReviewService(agent, concurrency=1, queue_capacity=1)
    await service.start()

    first = asyncio.create_task(service.submit(case("active")))
    await agent.started.wait()
    second = asyncio.create_task(service.submit(case("waiting")))
    await asyncio.sleep(0)

    with pytest.raises(QueueFullError):
        await service.submit(case("rejected"))

    snapshot = service.snapshot()
    assert snapshot["active"] == 1
    assert snapshot["waiting"] == 1
    assert snapshot["in_flight"] == 2
    assert snapshot["offered"] == 3
    assert snapshot["admitted"] == 2
    assert snapshot["rejected"] == 1

    agent.release.set()
    await asyncio.gather(first, second)
    snapshot = service.snapshot()
    assert snapshot["completed"] == 2
    assert snapshot["in_flight"] == 0
    assert snapshot["admitted"] == snapshot["completed"] + snapshot["in_flight"]
    await service.stop()


@pytest.mark.asyncio
async def test_zero_waiting_capacity_still_allows_processing_slots() -> None:
    agent = GatedAgent()
    service = BoundedReviewService(agent, concurrency=1, queue_capacity=0)
    await service.start()

    first = asyncio.create_task(service.submit(case("active")))
    await agent.started.wait()
    with pytest.raises(QueueFullError):
        await service.submit(case("rejected"))

    agent.release.set()
    await first
    await service.stop()
    assert service.snapshot()["rejected"] == 1


@pytest.mark.asyncio
async def test_api_returns_multi_tool_result_and_local_metric_scope() -> None:
    app = create_app(concurrency=1, queue_capacity=1, tool_delay_ms=0)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post(
            "/review",
            json={
                "case_id": "demo-1",
                "annual_revenue": 1_000_000,
                "operating_income": 200_000,
                "annual_debt_service": 100_000,
            },
        )
        metrics = (await client.get("/metrics")).json()

    assert response.status_code == 200
    body = response.json()
    assert body["case_id"] == "demo-1"
    assert body["recommendation"] == "supports_review"
    assert len(body["tool_events"]) == 5
    assert body["model_calls"] == 0
    assert body["tool_calls"] == 5
    assert body["execution_source"] == "synthetic_fixture"
    assert body["timing_ms"]["end_to_end"] >= body["timing_ms"]["processing"]
    assert metrics["scope"] == "process_local"
    assert metrics["admitted"] == metrics["completed"] == 1


@pytest.mark.asyncio
async def test_review_stream_emits_live_tool_activity_and_result() -> None:
    app = create_app(concurrency=1, queue_capacity=1, tool_delay_ms=0)
    async with (
        app.router.lifespan_context(app),
        AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client,
    ):
        response = await client.post(
            "/review/stream",
            json={
                "case_id": "stream-demo",
                "annual_revenue": 1_000_000,
                "operating_income": 200_000,
                "annual_debt_service": 100_000,
                "question": "What does the synthetic analysis find?",
            },
        )

    assert response.status_code == 200
    assert (
        "event: " not in response.text
    )  # SSE uses data frames, not named event fields.
    assert '"type":"tool_started"' in response.text
    assert '"name":"calculate_dscr"' in response.text
    assert '"type":"result"' in response.text
    assert "deterministic fixture response" in response.text


@pytest.mark.asyncio
async def test_complex_path_has_more_configured_stress_work() -> None:
    agent = SyntheticCreditReviewAgent(tool_delay_ms=0.1, model_delay_ms=0.1)
    standard = await agent.run(case())
    complex_result = await agent.run(
        CreditCase(
            case_id="complex-1",
            annual_revenue=1_000_000,
            operating_income=200_000,
            annual_debt_service=100_000,
            complexity="complex",
        )
    )

    standard_stress = next(
        event for event in standard.tool_events if event.name == "stress_scenario"
    )
    complex_stress = next(
        event for event in complex_result.tool_events if event.name == "stress_scenario"
    )
    assert complex_stress.configured_delay_ms == 2 * standard_stress.configured_delay_ms
