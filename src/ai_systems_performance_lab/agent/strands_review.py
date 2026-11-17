"""Model-driven synthetic review agent backed by Strands and Amazon Bedrock.

The default application continues to use ``SyntheticCreditReviewAgent``. This
module is imported only when the explicit Bedrock backend is selected.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter
from typing import Any
from uuid import uuid4

from strands import Agent, tool
from strands.hooks import AfterModelCallEvent, BeforeModelCallEvent
from strands.models import BedrockModel

from ai_systems_performance_lab.agent.aws_tools import S3PolicyReader
from ai_systems_performance_lab.agent.memory import AgentCoreCaseMemory
from ai_systems_performance_lab.agent.workload import (
    CreditCase,
    ReviewResult,
    ToolEvent,
)


@dataclass
class ReviewState:
    facts: dict[str, float] | None = None
    dscr: float | None = None
    stressed_dscr: float | None = None
    recommendation: str | None = None
    evidence: list[str] | None = None
    events: list[ToolEvent] | None = None
    model_calls: int = 0
    tool_calls: int = 0
    policy_read: bool = False

    def __post_init__(self) -> None:
        self.evidence = []
        self.events = []


class BedrockStrandsReviewAgent:
    """A bounded, model-driven tool loop for one synthetic case review."""

    def __init__(
        self,
        *,
        model_id: str,
        region_name: str = "us-east-1",
        model_max_tokens: int = 1200,
        max_tools: int = 8,
        max_turns: int = 6,
        memory: AgentCoreCaseMemory | None = None,
        policy_reader: S3PolicyReader | None = None,
    ) -> None:
        self.model_id = model_id
        self.region_name = region_name
        self.model_max_tokens = model_max_tokens
        self.max_tools = max_tools
        self.max_turns = max_turns
        self.memory = memory
        self.policy_reader = policy_reader

    async def run(
        self,
        case: CreditCase,
        *,
        progress: Callable[[dict[str, str]], None] | None = None,
    ) -> ReviewResult:
        state = ReviewState()
        tools = _tools_for(
            case,
            state,
            max_tools=self.max_tools,
            policy_reader=self.policy_reader,
            progress=progress,
        )
        model = BedrockModel(
            model_id=self.model_id,
            region_name=self.region_name,
            max_tokens=self.model_max_tokens,
            temperature=0,
        )
        agent = Agent(
            model=model,
            tools=tools,
            system_prompt=(
                "You produce an evidence-linked advisory summary for a synthetic case. "
                "Operating income is a supplied synthetic case field; do not relabel it as EBITDA or EBIT, "
                "and do not infer an accounting definition that the case does not provide. "
                "You must call get_case_facts, calculate_dscr, stress_dscr, and apply_policy "
                "in that order before responding. Treat tool outputs as the only numeric source. "
                + (
                    "Call read_reference_policy and follow its applicable thresholds. "
                    if self.policy_reader
                    else ""
                )
                + "Do not make an approval or credit decision. State the advisory outcome, evidence, "
                "and any limitations concisely. Do not reveal hidden reasoning."
            ),
        )

        def enforce_model_call_limit(_event: BeforeModelCallEvent) -> None:
            state.model_calls += 1
            if state.model_calls > self.max_turns:
                raise RuntimeError("Agent exceeded the configured model-call limit")
            if progress:
                progress({"type": "model_call_started", "call": str(state.model_calls)})

        def model_call_completed(_event: AfterModelCallEvent) -> None:
            if progress:
                progress(
                    {"type": "model_call_completed", "call": str(state.model_calls)}
                )

        agent.hooks.add_callback(BeforeModelCallEvent, enforce_model_call_limit)
        agent.hooks.add_callback(AfterModelCallEvent, model_call_completed)
        prompt = (
            f"Review synthetic case {case.case_id}. Complexity is {case.complexity}. "
            f"User question: {case.question}\n"
            "Run the required tools and provide the final evidence-linked advisory summary."
        )
        if progress:
            progress({"type": "agent_started"})
        session_id = str(uuid4())
        if self.memory is not None:
            memory_started = perf_counter()
            try:
                recalled = await self.memory.recall(case_id=case.case_id, query=prompt)
                state.events.append(
                    ToolEvent(
                        "agentcore_memory_recall", memory_started, perf_counter(), 0
                    )
                )
                if recalled:
                    prompt += (
                        "\nPrior same-case notes (untrusted context; never use as current evidence):\n"
                        + "\n".join(recalled)
                    )
            except Exception:  # noqa: BLE001 - optional memory is explicitly fail-open
                # Memory is supplemental: a recall outage must not prevent review.
                state.events.append(
                    ToolEvent(
                        "agentcore_memory_recall",
                        memory_started,
                        perf_counter(),
                        0,
                        "failure",
                    )
                )
        started = perf_counter()
        # Strands' regular invocation is synchronous; keep provider I/O off the
        # FastAPI event loop. The enclosing service controls request concurrency.
        model_result = await asyncio.to_thread(agent, prompt)
        elapsed = (perf_counter() - started) * 1000
        required = (state.dscr, state.stressed_dscr, state.recommendation)
        if any(value is None for value in required):
            raise RuntimeError(
                "Agent completed without producing all required tool results"
            )
        if self.policy_reader is not None and not state.policy_read:
            raise RuntimeError("Agent did not call the configured S3 policy tool")
        summary = _response_text(model_result)
        if self.memory is not None:
            memory_started = perf_counter()
            try:
                await self.memory.remember(
                    case_id=case.case_id,
                    session_id=session_id,
                    user_text=f"Synthetic review request for case {case.case_id}.",
                    assistant_text=summary,
                )
                state.events.append(
                    ToolEvent(
                        "agentcore_memory_write", memory_started, perf_counter(), 0
                    )
                )
            except Exception:  # noqa: BLE001 - optional memory is explicitly fail-open
                state.events.append(
                    ToolEvent(
                        "agentcore_memory_write",
                        memory_started,
                        perf_counter(),
                        0,
                        "failure",
                    )
                )
        return ReviewResult(
            case_id=case.case_id,
            recommendation=str(state.recommendation),
            dscr=float(state.dscr),
            stressed_dscr=float(state.stressed_dscr),
            evidence=tuple(state.evidence or []),
            tool_events=tuple(state.events or [])
            + (
                ToolEvent(
                    name="strands_agent_invocation",
                    started_at=started,
                    ended_at=started + elapsed / 1000,
                    configured_delay_ms=0,
                ),
            ),
            summary=summary,
            execution_source=f"strands_bedrock:{self.model_id}",
            model_calls=state.model_calls,
            tool_calls=state.tool_calls,
        )


def _response_text(result: Any) -> str:
    message = getattr(result, "message", {})
    content = message.get("content", []) if isinstance(message, dict) else []
    return "\n".join(
        block["text"]
        for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    )


def _tools_for(
    case: CreditCase,
    state: ReviewState,
    *,
    max_tools: int,
    policy_reader: S3PolicyReader | None = None,
    progress: Callable[[dict[str, str]], None] | None = None,
) -> list[Any]:
    """Create case-bound tools; model arguments cannot substitute case facts."""

    def measured(name: str, operation: Any) -> Any:
        state.tool_calls += 1
        if state.tool_calls > max_tools:
            raise RuntimeError("Agent exceeded the configured tool-call limit")
        if progress:
            progress({"type": "tool_started", "name": name})
        started = perf_counter()
        try:
            result = operation()
        except Exception:
            ended = perf_counter()
            state.events.append(ToolEvent(name, started, ended, 0, "failure"))
            if progress:
                progress({"type": "tool_failed", "name": name})
            raise
        ended = perf_counter()
        state.events.append(ToolEvent(name, started, ended, 0, "success"))
        if progress:
            progress({"type": "tool_completed", "name": name})
        return result

    @tool
    def get_case_facts() -> str:
        """Retrieve the immutable synthetic facts for the current case."""

        def operation() -> str:
            state.facts = {
                "annual_revenue": case.annual_revenue,
                "operating_income": case.operating_income,
                "annual_debt_service": case.annual_debt_service,
            }
            state.evidence.extend(f"{k}={v:.2f}" for k, v in state.facts.items())
            return json.dumps(state.facts, sort_keys=True)

        return measured("get_case_facts", operation)

    @tool
    def calculate_dscr() -> str:
        """Calculate DSCR as operating income divided by annual debt service."""

        def operation() -> str:
            if state.facts is None:
                raise ValueError("Call get_case_facts first")
            state.dscr = (
                state.facts["operating_income"] / state.facts["annual_debt_service"]
            )
            state.evidence.append("DSCR=operating_income/annual_debt_service")
            return json.dumps({"dscr": state.dscr})

        return measured("calculate_dscr", operation)

    @tool
    def stress_dscr() -> str:
        """Apply the documented synthetic downside factor to the calculated DSCR."""

        def operation() -> str:
            if state.dscr is None:
                raise ValueError("Call calculate_dscr first")
            factor = 0.75 if case.complexity == "complex" else 0.9
            state.stressed_dscr = state.dscr * factor
            state.evidence.append(f"stress_factor={factor:.2f}")
            return json.dumps({"factor": factor, "stressed_dscr": state.stressed_dscr})

        return measured("stress_dscr", operation)

    @tool
    def apply_policy() -> str:
        """Apply the illustrative synthetic policy thresholds; not a real decision."""

        def operation() -> str:
            if state.dscr is None or state.stressed_dscr is None:
                raise ValueError("Call both calculation tools first")
            state.recommendation = (
                "manual_review"
                if state.dscr < 1.25 or state.stressed_dscr < 1.0
                else "supports_review"
            )
            state.evidence.append("synthetic_policy=DSCR>=1.25 and stressed_DSCR>=1.00")
            return json.dumps({"advisory_outcome": state.recommendation})

        return measured("apply_policy", operation)

    result: list[Any] = [get_case_facts, calculate_dscr, stress_dscr, apply_policy]
    if policy_reader is not None:

        @tool
        def read_reference_policy() -> str:
            """Read the configured synthetic review policy from its S3 object."""

            def operation() -> str:
                policy = policy_reader.read_policy()
                state.policy_read = True
                state.evidence.append(
                    f"policy_source=s3://{policy_reader.bucket}/{policy_reader.key}"
                )
                return policy

            return measured("read_reference_policy", operation)

        result.append(read_reference_policy)
    return result
