"""HTTP APIs for the local conversational demo and deterministic workload."""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from typing import Literal
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ai_systems_performance_lab.admission.conversation_service import (
    BoundedConversationService,
)
from ai_systems_performance_lab.admission.service import (
    BoundedReviewService,
    QueueFullError,
)
from ai_systems_performance_lab.agent.aws_tools import (
    DynamoDBCaseRepository,
    InMemoryCaseRepository,
    S3EvidenceReader,
    S3PolicyReader,
)
from ai_systems_performance_lab.agent.workload import (
    AgentTurn,
    CreditCase,
    SimulatedConversationAgent,
    SyntheticCreditReviewAgent,
    UsageBudgetExceededError,
    load_agent_cases,
)

DEFAULT_CASES = load_agent_cases(
    generated_count=int(os.getenv("SYNTHETIC_CASE_COUNT", "1000")),
    seed=int(os.getenv("SYNTHETIC_SEED", "20261117")),
)


class ReviewRequest(BaseModel):
    case_id: str = Field(min_length=1, max_length=64)
    annual_revenue: float = Field(gt=0)
    operating_income: float = Field(gt=0)
    annual_debt_service: float = Field(gt=0)
    complexity: Literal["standard", "complex"] = "standard"
    question: str = Field(
        default="Summarize the synthetic case findings.",
        min_length=1,
        max_length=1200,
    )


class CreateAgentSessionRequest(BaseModel):
    case_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")


class AgentTurnRequest(BaseModel):
    case_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")
    question: str = Field(min_length=1, max_length=1200)


def create_app(
    *,
    concurrency: int = 2,
    queue_capacity: int = 4,
    tool_delay_ms: float = 20.0,
    model_delay_ms: float = 30.0,
    agent_backend: Literal["simulated", "fixture", "bedrock"] = "simulated",
    bedrock_model_id: str | None = None,
    aws_region: str = "us-east-1",
    case_table: str | None = None,
    data_bucket: str | None = None,
    session_bucket: str | None = None,
    memory_id: str | None = None,
) -> FastAPI:
    if agent_backend not in {"simulated", "fixture", "bedrock"}:
        raise ValueError(
            "agent_backend must be 'simulated' (legacy 'fixture') or 'bedrock'"
        )
    simulated_mode = agent_backend in {"simulated", "fixture"}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # The deterministic review workload is used only by
        # the separate workload/queue API; it never calls Bedrock.
        review_service = BoundedReviewService(
            SyntheticCreditReviewAgent(
                tool_delay_ms=tool_delay_ms, model_delay_ms=model_delay_ms
            ),
            concurrency=concurrency,
            queue_capacity=queue_capacity,
        )
        cases = InMemoryCaseRepository(DEFAULT_CASES)
        if simulated_mode:
            conversation_agent = SimulatedConversationAgent(
                DEFAULT_CASES,
                tool_delay_ms=tool_delay_ms,
                model_delay_ms=model_delay_ms,
            )
        else:
            if not bedrock_model_id:
                raise RuntimeError(
                    "BEDROCK_MODEL_ID is required when AGENT_BACKEND=bedrock"
                )
            if not case_table or not data_bucket:
                raise RuntimeError(
                    "DYNAMODB_CASES_TABLE and AWS_DATA_BUCKET are required for the Bedrock agent"
                )
            cases = DynamoDBCaseRepository(
                table_name=case_table, region_name=aws_region
            )
            evidence_reader = S3EvidenceReader(
                bucket=data_bucket, region_name=aws_region
            )
            policy_reader = S3PolicyReader(
                bucket=data_bucket,
                key=os.getenv("POLICY_S3_KEY", "policies/synthetic-review-v1.txt"),
                region_name=aws_region,
            )
            selected_session_bucket = session_bucket or data_bucket
            memory = None
            if memory_id:
                from ai_systems_performance_lab.agent.memory import AgentCoreCaseMemory

                memory = AgentCoreCaseMemory(
                    memory_id=memory_id,
                    region_name=aws_region,
                    namespace_template=os.getenv(
                        "AGENTCORE_MEMORY_NAMESPACE", "case/{actorId}/reviews"
                    ),
                )
            from ai_systems_performance_lab.agent.conversation import (
                BedrockConversationAgent,
            )

            conversation_agent = BedrockConversationAgent(
                model_id=bedrock_model_id,
                region_name=aws_region,
                cases=cases,
                evidence=evidence_reader,
                policy=policy_reader,
                session_bucket=selected_session_bucket,
                session_prefix=os.getenv("AGENT_SESSION_PREFIX", "agent-sessions"),
                memory=memory,
                max_tools=int(os.getenv("AGENT_MAX_TOOL_CALLS", "8")),
                max_turns=int(os.getenv("AGENT_MAX_MODEL_TURNS", "6")),
                max_output_tokens=int(os.getenv("AGENT_MAX_OUTPUT_TOKENS", "900")),
                model_max_tokens=int(os.getenv("BEDROCK_MAX_TOKENS", "1200")),
                max_requests_per_process=int(
                    os.getenv("AGENT_MAX_REQUESTS_PER_PROCESS", "5")
                ),
                model_read_timeout_seconds=int(
                    os.getenv("BEDROCK_READ_TIMEOUT_SECONDS", "60")
                ),
                request_timeout_seconds=int(
                    os.getenv("AGENT_REQUEST_TIMEOUT_SECONDS", "180")
                ),
            )
        conversation_service = BoundedConversationService(
            conversation_agent,
            concurrency=concurrency,
            queue_capacity=queue_capacity,
        )
        await review_service.start()
        await conversation_service.start()
        app.state.review_service = review_service
        app.state.conversation_service = conversation_service
        app.state.agent_backend = "simulated" if simulated_mode else "bedrock"
        app.state.cases = cases
        app.state.case_options = DEFAULT_CASES
        try:
            yield
        finally:
            await conversation_service.stop()
            await review_service.stop()

    app = FastAPI(title="AI Systems Performance Lab", lifespan=lifespan)

    @app.get("/health")
    async def health(request: Request) -> dict[str, str]:
        return {"status": "ok", "inference": request.app.state.agent_backend}

    @app.get("/metrics")
    async def metrics(request: Request) -> dict:
        service: BoundedReviewService = request.app.state.review_service
        return service.snapshot()

    @app.get("/agent/metrics")
    async def agent_metrics(request: Request) -> dict:
        service: BoundedConversationService = request.app.state.conversation_service
        return service.snapshot()

    @app.get("/agent/state")
    async def agent_state(request: Request) -> dict:
        service: BoundedConversationService = request.app.state.conversation_service
        return service.state()

    @app.get("/agent/cases")
    async def demo_cases(request: Request) -> list[dict[str, str]]:
        options: dict[str, CreditCase] = request.app.state.case_options
        result = []
        for case in options.values():
            if case.case_id.startswith("demo-case-"):
                label = f"{case.case_id} · {case.complexity} · golden example"
            else:
                index = case.case_id.removeprefix("synthetic-case-")
                dscr = case.operating_income / case.annual_debt_service
                label = f"Synthetic {index} · {case.complexity} · DSCR {dscr:.2f}"
            result.append({"case_id": case.case_id, "label": label})
        return result

    @app.post("/agent/sessions")
    async def create_agent_session(
        payload: CreateAgentSessionRequest,
    ) -> dict[str, str]:
        # Session state is stored by Strands' S3 session manager in Bedrock mode.
        # A UUID is generated server-side and reused by the UI for each turn.
        return {
            "conversation_id": f"{payload.case_id}.{uuid4().hex}",
            "case_id": payload.case_id,
        }

    @app.post("/agent/sessions/{conversation_id}/turns/stream")
    async def agent_turn_stream(
        conversation_id: str,
        payload: AgentTurnRequest,
        request: Request,
    ) -> StreamingResponse:
        session_case_id, separator, nonce = conversation_id.rpartition(".")
        if (
            not separator
            or session_case_id != payload.case_id
            or not nonce.isalnum()
            or len(nonce) != 32
        ):
            raise HTTPException(
                status_code=400, detail={"reason": "invalid_session_id"}
            )
        service: BoundedConversationService = request.app.state.conversation_service
        turn = AgentTurn(
            case_id=payload.case_id,
            conversation_id=conversation_id,
            question=payload.question,
        )
        events: asyncio.Queue[dict | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def publish(event: dict[str, str]) -> None:
            if not loop.is_closed():
                loop.call_soon_threadsafe(events.put_nowait, event)

        async def execute() -> None:
            try:
                response = await service.submit(turn, progress=publish)
                await events.put({"type": "result", "data": response})
            except QueueFullError:
                await events.put({"type": "error", "data": {"reason": "queue_full"}})
            except UsageBudgetExceededError:
                await events.put(
                    {"type": "error", "data": {"reason": "usage_budget_exhausted"}}
                )
            except TimeoutError:
                await events.put({"type": "error", "data": {"reason": "agent_timeout"}})
            except ClientError as exc:
                reason = classify_provider_error(exc)
                await events.put({"type": "error", "data": {"reason": reason}})
            except BotoCoreError:
                await events.put(
                    {"type": "error", "data": {"reason": "provider_unavailable"}}
                )
            except ValueError as exc:
                await events.put(
                    {
                        "type": "error",
                        "data": {"reason": "invalid_request", "message": str(exc)},
                    }
                )
            except Exception:  # noqa: BLE001 - provider details stay out of user-facing stream
                await events.put({"type": "error", "data": {"reason": "agent_failure"}})
            finally:
                await events.put(None)

        async def stream():
            producer = asyncio.create_task(execute())
            try:
                while (event := await events.get()) is not None:
                    yield f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
            finally:
                if not producer.done():
                    producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/review")
    async def review(payload: ReviewRequest, request: Request) -> dict:
        service: BoundedReviewService = request.app.state.review_service
        case = CreditCase(**payload.model_dump())
        try:
            return await service.submit(case)
        except QueueFullError:
            raise HTTPException(
                status_code=429,
                detail={
                    "reason": "queue_full",
                    "message": "No waiting positions are available.",
                },
                headers={"Retry-After": "1"},
            ) from None
        except Exception as exc:
            raise HTTPException(
                status_code=502, detail={"reason": "agent_failure"}
            ) from exc

    @app.post("/review/stream")
    async def review_stream(
        payload: ReviewRequest, request: Request
    ) -> StreamingResponse:
        service: BoundedReviewService = request.app.state.review_service
        case = CreditCase(**payload.model_dump())
        events: asyncio.Queue[dict | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def publish(event: dict[str, str]) -> None:
            if not loop.is_closed():
                loop.call_soon_threadsafe(events.put_nowait, event)

        async def execute() -> None:
            try:
                result = await service.submit(case, progress=publish)
                await events.put({"type": "result", "data": result})
            except QueueFullError:
                await events.put({"type": "error", "data": {"reason": "queue_full"}})
            except Exception:  # noqa: BLE001 - keep the workload endpoint shape stable
                await events.put({"type": "error", "data": {"reason": "agent_failure"}})
            finally:
                await events.put(None)

        async def stream():
            producer = asyncio.create_task(execute())
            try:
                while (event := await events.get()) is not None:
                    yield f"data: {json.dumps(event, separators=(',', ':'))}\n\n"
            finally:
                if not producer.done():
                    producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


app = create_app(
    concurrency=int(os.getenv("LAB_CONCURRENCY", "2")),
    queue_capacity=int(os.getenv("LAB_QUEUE_CAPACITY", "4")),
    tool_delay_ms=float(os.getenv("LAB_TOOL_DELAY_MS", "20")),
    model_delay_ms=float(os.getenv("LAB_MODEL_DELAY_MS", "30")),
    agent_backend=os.getenv("AGENT_BACKEND", "simulated"),  # type: ignore[arg-type]
    bedrock_model_id=os.getenv("BEDROCK_MODEL_ID"),
    aws_region=os.getenv("AWS_REGION", "us-east-1"),
    case_table=os.getenv("DYNAMODB_CASES_TABLE"),
    data_bucket=os.getenv("AWS_DATA_BUCKET"),
    session_bucket=os.getenv("AGENT_SESSION_S3_BUCKET"),
    memory_id=os.getenv("AGENTCORE_MEMORY_ID"),
)


def classify_provider_error(exc: ClientError) -> str:
    """Map provider failures to safe, useful UI categories without raw details."""
    error = exc.response.get("Error", {})
    code = str(error.get("Code", "")).casefold()
    message = str(error.get("Message", "")).casefold()
    if "throttl" in code or "toomanyrequests" in code:
        return "provider_throttled"
    if code in {"accessdeniedexception", "accessdenied"}:
        if "account is currently being verified" in message:
            return "provider_account_verification"
        return "provider_access_denied"
    return "provider_unavailable"
