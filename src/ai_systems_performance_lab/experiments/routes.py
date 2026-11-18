"""HTTP endpoints for saved local experiments and bounded run controls."""

from __future__ import annotations

import csv
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from ai_systems_performance_lab.experiments.interactive import ComparisonRequest
from ai_systems_performance_lab.experiments.service import LocalExperimentManager
from ai_systems_performance_lab.experiments.slo import SizingStudyRequest

router = APIRouter(prefix="/experiments", tags=["experiments"])
_logger = logging.getLogger(__name__)
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,100}$")


def _manager(request: Request) -> LocalExperimentManager:
    return request.app.state.experiment_manager


def _run_path(manager: LocalExperimentManager, run_id: str) -> Path:
    if not _RUN_ID.fullmatch(run_id):
        raise HTTPException(status_code=404, detail="run_not_found")
    path = (manager.output_root / "local" / run_id).resolve()
    if path.parent != manager.output_root / "local" or not path.is_dir():
        raise HTTPException(status_code=404, detail="run_not_found")
    return path


def _read_json(path: Path, name: str) -> Any:
    try:
        return json.loads((path / name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"invalid_{name}") from exc


def _display_source(manifest: dict[str, Any]) -> str:
    source = str(manifest.get("source", "unknown source"))
    if source == "local fixture HTTP measurements":
        return "Local simulated-agent HTTP measurements (legacy source label)"
    return source


def _read_csv(path: Path, name: str, limit: int) -> list[dict[str, str]]:
    try:
        with (path / name).open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))[:limit]
    except OSError:
        return []


def _aggregate_time_series(rows: list[dict[str, str]], *, max_points: int = 240) -> list[dict[str, Any]]:
    """Return bounded per-condition means in fixed 250 ms elapsed-time bins."""
    fields = ("offered", "admitted", "rejected", "completed", "active", "waiting")
    repetition_bins: dict[tuple[str, str, int], dict[str, list[float]]] = {}
    for row in rows:
        try:
            condition = row["condition"]
            repetition = row["repetition"]
            offset = float(row["offset_s"])
        except (KeyError, TypeError, ValueError):
            continue
        point = min(max_points - 1, max(0, round(offset * 4)))
        values = repetition_bins.setdefault((condition, repetition, point), {})
        for field in fields:
            try:
                value = float(row[field])
            except (KeyError, TypeError, ValueError):
                continue
            values.setdefault(field, []).append(value)
    result: list[dict[str, Any]] = []
    condition_bins: dict[tuple[str, int], dict[str, list[float]]] = {}
    for (condition, _repetition, point), values in repetition_bins.items():
        target = condition_bins.setdefault((condition, point), {})
        for field, samples in values.items():
            target.setdefault(field, []).append(sum(samples) / len(samples))
    for (condition, point), values in sorted(condition_bins.items()):
        result.append({
            "condition": condition,
            "offset_s": point / 4,
            "repetitions": max((len(samples) for samples in values.values()), default=0),
            **{field: sum(samples) / len(samples) for field, samples in values.items() if samples},
        })
    return result


@router.get("/jobs/current")
async def current_job(request: Request) -> dict[str, Any] | None:
    return _manager(request).snapshot()


@router.post("/jobs", status_code=202)
async def start_job(payload: ComparisonRequest, request: Request) -> dict[str, Any]:
    try:
        return _manager(request).start(payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/sizing-studies", status_code=202)
async def start_sizing_study(payload: SizingStudyRequest, request: Request) -> dict[str, Any]:
    try:
        return _manager(request).start_sizing(payload)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.delete("/jobs/current")
async def cancel_job(request: Request) -> dict[str, Any] | None:
    return await _manager(request).cancel()


@router.get("/runs")
async def list_runs(request: Request, limit: int = Query(default=50, ge=1, le=100)) -> list[dict[str, Any]]:
    manager = _manager(request)
    root = manager.output_root / "local"
    if not root.exists():
        return []
    results = []
    for path in sorted((item for item in root.iterdir() if item.is_dir()), key=lambda p: p.name, reverse=True):
        try:
            manifest = _read_json(path, "manifest.json")
            summary = _read_json(path, "summary.json")
        except HTTPException:
            continue
        if isinstance(summary, list):  # Older experiment matrix format.
            conditions = summary
            paired = None
        else:
            conditions = summary.get("conditions", [])
            paired = summary.get("paired_statistics")
        results.append({
            "run_id": path.name,
            "source": _display_source(manifest),
            "recorded_source": manifest.get("source"),
            "aws_calls": manifest.get("aws_calls"),
            "bedrock_calls": manifest.get("bedrock_calls"),
            "created_utc": manifest.get("created_utc"),
            "method": manifest.get("method", "recorded experiment"),
            "request": manifest.get("request"),
            "conditions": conditions,
            "paired_statistics": paired,
        })
        if len(results) >= limit:
            break
    return results


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    request: Request,
    occupancy_limit: int = Query(default=5000, ge=1, le=5000),
    request_limit: int = Query(default=1000, ge=0, le=5000),
) -> dict[str, Any]:
    path = _run_path(_manager(request), run_id)
    manifest = _read_json(path, "manifest.json")
    manifest["display_source"] = _display_source(manifest)
    summary = _read_json(path, "summary.json")
    occupancy = _read_csv(path, "queue_occupancy.csv", occupancy_limit)
    review_path = path / "architecture_review.json"
    return {
        "run_id": run_id,
        "manifest": manifest,
        "summary": summary,
        "architecture_review": json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else None,
        "occupancy": occupancy,
        "time_series": _aggregate_time_series(occupancy),
        "requests": _read_csv(path, "requests.csv", request_limit),
        "artifact_limits": {"occupancy_rows_returned": occupancy_limit, "request_rows_returned": request_limit},
    }


@router.post("/runs/{run_id}/architecture-review")
async def architecture_review(run_id: str, request: Request) -> dict[str, Any]:
    """Explicitly invoke a separate LLM reviewer on saved aggregate study data."""
    path = _run_path(_manager(request), run_id)
    manifest = _read_json(path, "manifest.json")
    summary = _read_json(path, "summary.json")
    report = summary.get("sizing_study") if isinstance(summary, dict) else None
    if "SLO sizing" not in str(manifest.get("method", "")) or not isinstance(report, dict):
        raise HTTPException(status_code=409, detail="architecture_review_requires_slo_sizing_study")
    try:
        from ai_systems_performance_lab.experiments.reviewer import (
            ReviewOutputError,
            review_study,
        )

        result = await review_study(report)
    except ReviewOutputError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        status_code = getattr(exc, "status_code", None)
        request_id = getattr(exc, "request_id", None)
        body = getattr(exc, "body", None)
        error_body = body.get("error", body) if isinstance(body, dict) else {}
        provider_code = error_body.get("code") if isinstance(error_body, dict) else None
        if not isinstance(provider_code, str):
            provider_code = None
        _logger.warning(
            "Architecture review provider request failed: exception=%s status=%s code=%s request_id=%s",
            type(exc).__name__,
            status_code,
            provider_code,
            request_id,
        )
        if status_code == 401:
            detail = "OpenAI rejected the API key (HTTP 401). Check that the key in the project .env is active."
        elif status_code == 403:
            detail = "OpenAI denied this request (HTTP 403). Check project access and model permissions."
        elif status_code == 404:
            detail = "The configured OpenAI model or endpoint was not found (HTTP 404). Check OPENAI_REVIEW_MODEL."
        elif status_code == 429:
            detail = "OpenAI returned HTTP 429. Check project quota, billing, or rate limits."
        elif status_code is not None:
            detail = f"OpenAI returned HTTP {status_code} during architecture review."
        else:
            detail = f"Could not complete the OpenAI architecture review ({type(exc).__name__}). Check the API terminal for diagnostics."
        if provider_code:
            detail += f" Provider code: {provider_code}."
        if isinstance(request_id, str) and request_id:
            detail += f" Request ID: {request_id}."
        raise HTTPException(status_code=502, detail=detail) from exc
    result["created_utc"] = datetime.now(UTC).isoformat()
    (path / "architecture_review.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result
