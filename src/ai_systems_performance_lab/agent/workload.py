"""A deterministic, multi-tool synthetic workload used to exercise the service."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from time import perf_counter
from typing import Literal


@dataclass(frozen=True)
class CreditCase:
    case_id: str
    annual_revenue: float
    operating_income: float
    annual_debt_service: float
    complexity: Literal["standard", "complex"] = "standard"
    question: str = "Summarize the synthetic case findings."


@dataclass(frozen=True)
class AgentTurn:
    """One user turn in a case-bound conversation."""

    case_id: str
    conversation_id: str
    question: str


class UsageBudgetExceededError(RuntimeError):
    """A local Bedrock request-count guard stopped another paid interaction."""


@dataclass(frozen=True)
class AgentAnswer:
    """User-facing answer from a model-driven agent turn."""

    case_id: str
    conversation_id: str
    answer: str
    evidence: tuple[str, ...]
    tool_events: tuple[ToolEvent, ...]
    execution_source: str
    model_calls: int
    tool_calls: int


@dataclass(frozen=True)
class ToolEvent:
    name: str
    started_at: float
    ended_at: float
    configured_delay_ms: float
    outcome: Literal["success", "failure"] = "success"

    @property
    def duration_ms(self) -> float:
        return (self.ended_at - self.started_at) * 1000


@dataclass(frozen=True)
class ReviewResult:
    case_id: str
    recommendation: str
    dscr: float
    stressed_dscr: float
    evidence: tuple[str, ...]
    tool_events: tuple[ToolEvent, ...]
    summary: str | None = None
    execution_source: str = "synthetic_fixture"
    model_calls: int = 0
    tool_calls: int = 0


def load_demo_cases() -> dict[str, CreditCase]:
    """Load the same synthetic case fixtures used by the AWS seed utility."""
    fixture_path = Path(__file__).resolve().parents[3] / "fixtures" / "cases.json"
    records = json.loads(fixture_path.read_text(encoding="utf-8"))
    return {record["case_id"]: CreditCase(**record) for record in records}


def load_agent_cases(
    *, generated_count: int = 1_000, seed: int = 20_261_117
) -> dict[str, CreditCase]:
    """Load golden cases plus the default reproducible synthetic selection set."""
    from ai_systems_performance_lab.agent.synthetic_data import (
        generate_synthetic_records,
    )

    cases = load_demo_cases()
    for record in generate_synthetic_records(generated_count, seed):
        case = CreditCase(**record["case"])
        cases[case.case_id] = case
    return cases


class SyntheticCreditReviewAgent:
    """Scripted agent workload: retrieve, calculate, stress, and apply policy.

    Delays are explicit fixture parameters. No model is called and no real credit
    decision is produced; the workflow exists to generate correlated, inspectable
    tool work for systems experiments.
    """

    def __init__(
        self, tool_delay_ms: float = 20.0, model_delay_ms: float = 30.0
    ) -> None:
        self.tool_delay_s = tool_delay_ms / 1000
        self.model_delay_s = model_delay_ms / 1000

    async def run(
        self,
        case: CreditCase,
        *,
        progress: Callable[[dict[str, str]], None] | None = None,
    ) -> ReviewResult:
        events: list[ToolEvent] = []

        evidence = await self._call(
            "retrieve_evidence",
            self.tool_delay_s,
            events,
            lambda: (
                case.annual_revenue,
                case.operating_income,
                case.annual_debt_service,
            ),
            progress=progress,
        )
        dscr = await self._call(
            "calculate_dscr",
            self.tool_delay_s,
            events,
            lambda: evidence[1] / evidence[2],
            progress=progress,
        )
        stress_factor = 0.75 if case.complexity == "complex" else 0.9
        stressed_dscr = await self._call(
            "stress_scenario",
            self.tool_delay_s * (2 if case.complexity == "complex" else 1),
            events,
            lambda: dscr * stress_factor,
            progress=progress,
        )
        recommendation = await self._call(
            "apply_policy",
            self.tool_delay_s,
            events,
            lambda: (
                "manual_review"
                if dscr < 1.25 or stressed_dscr < 1.0
                else "supports_review"
            ),
            progress=progress,
        )
        # The mock model stage makes the workload shape explicit without claiming
        # that fixture output came from an LLM.
        await self._call(
            "mock_model_summary",
            self.model_delay_s,
            events,
            lambda: f"Fixture summary for {case.case_id}: {recommendation}",
            progress=progress,
        )
        return ReviewResult(
            case_id=case.case_id,
            recommendation=str(recommendation),
            dscr=float(dscr),
            stressed_dscr=float(stressed_dscr),
            evidence=(
                f"annual_revenue={evidence[0]:.2f}",
                f"operating_income={evidence[1]:.2f}",
                f"annual_debt_service={evidence[2]:.2f}",
                "synthetic_policy=DSCR>=1.25 and stressed_DSCR>=1.00",
            ),
            tool_events=tuple(events),
            summary=(
                f"{case.question} Findings: DSCR {dscr:.2f}, stressed DSCR {stressed_dscr:.2f}; "
                f"synthetic outcome: {recommendation}. This is a deterministic fixture response, "
                "not a language-model answer or credit decision."
            ),
            execution_source="synthetic_fixture",
            model_calls=0,
            tool_calls=len(events),
        )

    async def _call(
        self,
        name: str,
        delay_s: float,
        events: list[ToolEvent],
        operation: Callable[[], object],
        *,
        progress: Callable[[dict[str, str]], None] | None = None,
    ) -> object:
        started = perf_counter()
        if progress:
            progress({"type": "tool_started", "name": name})
        await asyncio.sleep(delay_s)
        result = operation()
        ended = perf_counter()
        events.append(
            ToolEvent(
                name=name,
                started_at=started,
                ended_at=ended,
                configured_delay_ms=delay_s * 1000,
            )
        )
        if progress:
            progress({"type": "tool_completed", "name": name})
        return result


class FixtureConversationAgent:
    """No-LLM conversational interface over deterministic synthetic fixtures."""

    def __init__(
        self, cases: dict[str, CreditCase], *, tool_delay_ms: float = 5.0
    ) -> None:
        self.cases = cases
        self.review_agent = SyntheticCreditReviewAgent(tool_delay_ms=tool_delay_ms)

    async def run(
        self,
        turn: AgentTurn,
        *,
        progress: Callable[[dict[str, str]], None] | None = None,
    ) -> AgentAnswer:
        try:
            case = self.cases[turn.case_id]
        except KeyError as exc:
            raise ValueError(f"Unknown synthetic case: {turn.case_id}") from exc
        case = replace(case, question=turn.question)
        review = await self.review_agent.run(case, progress=progress)
        return AgentAnswer(
            case_id=turn.case_id,
            conversation_id=turn.conversation_id,
            answer=(
                f"Fixture response to: {turn.question}\n\n{review.summary}\n\n"
                "This deterministic demonstration did not call a language model."
            ),
            evidence=review.evidence,
            tool_events=review.tool_events,
            execution_source="fixture_no_llm",
            model_calls=0,
            tool_calls=review.tool_calls,
        )
