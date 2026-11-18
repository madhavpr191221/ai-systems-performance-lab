"""On-demand, read-only LLM review of aggregate SLO sizing evidence."""

from __future__ import annotations

import json
import os
from typing import Any, Literal

from pydantic import Field, ValidationError, create_model


class ReviewOutputError(RuntimeError):
    """Structured review could not be safely tied to the saved study evidence."""


def _review_schema(report: dict[str, Any]) -> type:
    candidate_ids = sorted(item["configuration_id"] for item in report.get("candidate_results", []))
    if not candidate_ids:
        raise ReviewOutputError("This SLO study has no candidate results to review.")
    candidate_literal = Literal[tuple(candidate_ids)]
    metric_literal = Literal[
        "deadline_goodput_fraction",
        "rejection_fraction",
        "admitted_failure_fraction",
        "mean_successful_goodput_rps",
        "mean_of_per_repetition_successful_p95_ms",
    ]
    Evidence = create_model(
        "EvidenceReference",
        candidate_id=(candidate_literal, ...),
        metric=(metric_literal, ...),
    )
    Finding = create_model(
        "ReviewFinding",
        title=(str, Field(min_length=1, max_length=100)),
        interpretation=(str, Field(min_length=1, max_length=700)),
        confidence=(Literal["low", "moderate", "high"], ...),
        evidence=(list[Evidence], Field(min_length=1, max_length=5)),
    )
    NextExperiment = create_model(
        "NextExperiment",
        question=(str, Field(min_length=1, max_length=300)),
        change=(str, Field(min_length=1, max_length=300)),
        hold_constant=(list[str], Field(min_length=1, max_length=8)),
        success_evidence=(str, Field(min_length=1, max_length=400)),
    )
    return create_model(
        "ArchitectureReview",
        decision=(Literal["candidate_for_next_experiment", "no_candidate_meets_targets", "more_evidence_needed"], ...),
        summary=(str, Field(min_length=1, max_length=900)),
        candidate_id=(candidate_literal | None, ...),
        findings=(list[Finding], Field(min_length=1, max_length=6)),
        next_experiment=(NextExperiment, ...),
        limitations=(list[str], Field(min_length=1, max_length=8)),
    )


def _strict_schema(model: type) -> dict[str, Any]:
    """Prepare a Pydantic schema for strict Responses JSON output."""
    schema = model.model_json_schema()

    def visit(node: dict[str, Any]) -> None:
        for key in ("$defs", "definitions"):
            definitions = node.get(key)
            if isinstance(definitions, dict):
                for definition in definitions.values():
                    if isinstance(definition, dict):
                        visit(definition)
        if node.get("type") == "object":
            node["additionalProperties"] = False
            properties = node.get("properties", {})
            node["required"] = list(properties)
            for child in properties.values():
                if isinstance(child, dict):
                    visit(child)
        items = node.get("items")
        if isinstance(items, dict):
            visit(items)
        for union_key in ("anyOf", "oneOf", "allOf"):
            for child in node.get(union_key, []):
                if isinstance(child, dict):
                    visit(child)

    visit(schema)
    return schema


def _final_output_text(response: Any) -> str | None:
    """Read final-answer text while ignoring commentary and tool-call items."""
    chunks = []
    for item in getattr(response, "output", []):
        if getattr(item, "type", None) != "message":
            continue
        phase = getattr(item, "phase", None)
        if phase not in (None, "final_answer"):
            continue
        for content in getattr(item, "content", []):
            if getattr(content, "type", None) == "output_text":
                chunks.append(content.text)
    return "\n".join(chunks) or None


def _tools() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": "read_slo_targets",
            "description": "Read the configured SLO thresholds and study provenance.",
            "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            "strict": True,
        },
        {
            "type": "function",
            "name": "list_candidate_results",
            "description": "Read aggregate candidate outcomes and SLO classifications.",
            "parameters": {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
            "strict": True,
        },
        {
            "type": "function",
            "name": "compare_candidates",
            "description": "Compare two existing candidate IDs using aggregate evidence only.",
            "parameters": {
                "type": "object",
                "properties": {"candidate_a": {"type": "string"}, "candidate_b": {"type": "string"}},
                "required": ["candidate_a", "candidate_b"],
                "additionalProperties": False,
            },
            "strict": True,
        },
    ]


def _dispatch(name: str, arguments: str, report: dict[str, Any]) -> dict[str, Any]:
    candidates = report.get("candidate_results", [])
    if name == "read_slo_targets":
        return {"run_id": report.get("study_id"), "provenance": report.get("provenance"), "scope": report.get("scope"), "slo_profile": report.get("slo_profile"), "statistical_method": report.get("statistical_method"), "focus": report.get("focus")}
    if name == "list_candidate_results":
        return {"candidates": candidates, "recommendation": report.get("recommendation")}
    if name == "compare_candidates":
        args = json.loads(arguments)
        by_id = {item["configuration_id"]: item for item in candidates}
        a, b = args["candidate_a"], args["candidate_b"]
        if a not in by_id or b not in by_id:
            return {"error": "unknown_candidate_id", "allowed_ids": sorted(by_id)}
        return {"candidate_a": by_id[a], "candidate_b": by_id[b]}
    return {"error": "unknown_tool"}


async def review_study(report: dict[str, Any]) -> dict[str, Any]:
    """Call OpenAI only when explicitly requested by the API caller."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("The AI reviewer is not configured. Set OPENAI_API_KEY in the project .env file.")
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, timeout=45, max_retries=0)
    try:
        return await _review_with_client(client, report)
    finally:
        await client.close()


async def _review_with_client(client: Any, report: dict[str, Any]) -> dict[str, Any]:
    model = os.getenv("OPENAI_REVIEW_MODEL", "gpt-4.1-mini")
    output_schema = _review_schema(report)
    response_format = {
        "format": {
            "type": "json_schema",
            "name": "architecture_review",
            "strict": True,
            "schema": _strict_schema(output_schema),
        }
    }
    model_calls = 0
    usage_totals = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    def record_usage(result: Any) -> None:
        usage = getattr(result, "usage", None)
        if usage is not None:
            for field in usage_totals:
                usage_totals[field] += int(getattr(usage, field, 0) or 0)

    system = (
        "You are an architecture reviewer outside the measured inference path. Use the read-only tools. "
        "Choose only candidate IDs and metric names allowed by the structured output schema. The UI obtains "
        "all numerical values from the saved report, so never put invented numeric measurements in text. "
        "Distinguish local measurements from uncertainty and inference. Do not infer AWS capacity, production "
        "readiness, or cost feasibility. Select candidate_for_next_experiment only for a candidate not classified "
        "infeasible; use more_evidence_needed for inconclusive or under-sampled outcomes. Never propose deployment."
    )
    # Use the raw Responses method here. Its Pydantic `parse` helper can try to
    # parse an intermediate assistant text message emitted alongside a required
    # tool call as if it were the final review. We only validate final-answer
    # text after the tool loop is complete.
    response = await client.responses.create(
        model=model,
        input=[{"role": "system", "content": system}, {"role": "user", "content": "Review this saved SLO sizing study. Select evidence references, explain what the data supports, and propose one controlled next experiment."}],
        tools=_tools(),
        tool_choice="required",
        text=response_format,
    )
    model_calls += 1
    record_usage(response)
    calls_used = 0
    while True:
        calls = [item for item in response.output if getattr(item, "type", None) == "function_call"]
        if not calls:
            break
        outputs = []
        for call in calls:
            calls_used += 1
            if calls_used > 4:
                raise ReviewOutputError("The AI reviewer reached its four-tool-call limit. Please retry.")
            result = _dispatch(call.name, call.arguments, report)
            outputs.append({"type": "function_call_output", "call_id": call.call_id, "output": json.dumps(result, separators=(",", ":"))})
        response = await client.responses.create(
            model=model,
            previous_response_id=response.id,
            input=outputs,
            tools=_tools(),
            tool_choice="auto",
            text=response_format,
        )
        model_calls += 1
        record_usage(response)
    output_text = _final_output_text(response)
    if output_text is None:
        raise ReviewOutputError("The AI reviewer did not return the required structured result. Please retry.")
    try:
        parsed = output_schema.model_validate_json(output_text).model_dump()
    except ValidationError as exc:
        fields = [".".join(str(part) for part in error["loc"]) for error in exc.errors(include_input=False)]
        field_list = ", ".join(dict.fromkeys(fields)) or "response"
        raise ReviewOutputError(
            f"The AI reviewer returned an incomplete structured result ({field_list}). Please retry."
        ) from exc
    candidates = {item["configuration_id"]: item for item in report.get("candidate_results", [])}
    try:
        for finding in parsed["findings"]:
            for reference in finding["evidence"]:
                candidate = candidates.get(reference["candidate_id"])
                if candidate is None:
                    raise ValueError("unknown candidate")
                key = reference["metric"]
                if key in candidate.get("slo_metrics", {}):
                    value = candidate["slo_metrics"][key].get("mean")
                else:
                    value = candidate.get("diagnostics", {}).get(key)
                if value is None:
                    raise ValueError("metric has no measured value")
        selected = parsed["candidate_id"]
        if selected and candidates[selected]["classification"] == "infeasible":
            raise ValueError("infeasible candidate selected")
        decision = parsed["decision"]
        if decision == "candidate_for_next_experiment" and selected is None:
            raise ValueError("candidate decision requires a candidate ID")
        if decision == "no_candidate_meets_targets" and any(
            item["classification"] != "infeasible" for item in candidates.values()
        ):
            raise ValueError("study has inconclusive candidates")
    except (KeyError, ValueError, TypeError) as exc:
        raise ReviewOutputError("The AI reviewer returned evidence that does not match this study. Please retry.") from exc
    return {
        "review": parsed,
        "review_model": model,
        "tool_calls": calls_used,
        "model_calls": model_calls,
        "usage": usage_totals,
        "review_path": "separate on-demand OpenAI architecture review; excluded from measured agent workload",
    }
