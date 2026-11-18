"""HTTP endpoints for saved local experiments and bounded run controls."""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from ai_systems_performance_lab.experiments.interactive import ComparisonRequest
from ai_systems_performance_lab.experiments.service import LocalExperimentManager

router = APIRouter(prefix="/experiments", tags=["experiments"])
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


@router.get("/jobs/current")
async def current_job(request: Request) -> dict[str, Any] | None:
    return _manager(request).snapshot()


@router.post("/jobs", status_code=202)
async def start_job(payload: ComparisonRequest, request: Request) -> dict[str, Any]:
    try:
        return _manager(request).start(payload)
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
    occupancy_limit: int = Query(default=2500, ge=1, le=5000),
    request_limit: int = Query(default=1000, ge=0, le=5000),
) -> dict[str, Any]:
    path = _run_path(_manager(request), run_id)
    manifest = _read_json(path, "manifest.json")
    manifest["display_source"] = _display_source(manifest)
    summary = _read_json(path, "summary.json")
    return {
        "run_id": run_id,
        "manifest": manifest,
        "summary": summary,
        "occupancy": _read_csv(path, "queue_occupancy.csv", occupancy_limit),
        "requests": _read_csv(path, "requests.csv", request_limit),
        "artifact_limits": {"occupancy_rows_returned": occupancy_limit, "request_rows_returned": request_limit},
    }
