"""Explicit service-level objectives and bounded sizing-study inputs."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class SLOProfile(BaseModel):
    """Illustrative request-level targets, evaluated over offered turns."""

    name: str = Field(default="Interview demo", min_length=1, max_length=80)
    deadline_ms: float = Field(default=5000, gt=0, allow_inf_nan=False)
    deadline_goodput_target: float = Field(default=0.95, ge=0, le=1)
    max_rejection_fraction: float = Field(default=0.01, ge=0, le=1)
    max_admitted_failure_fraction: float = Field(default=0.01, ge=0, le=1)
    confidence_level: float = Field(default=0.95, gt=0.5, lt=1)


class SizingStudyRequest(BaseModel):
    """One-factor SLO study. The selected focus determines the candidate axis."""

    focus: Literal["queue_capacity", "concurrency", "rate", "arrival"] = "queue_capacity"
    arrival: Literal["poisson", "deterministic"] = "poisson"
    arrival_rate_rps: int = Field(default=12, ge=1, le=20)
    concurrency: int = Field(default=2, ge=1, le=16)
    queue_capacity: int = Field(default=4, ge=0, le=64)
    concurrency_candidates: list[int] = Field(default=[1, 2, 4], min_length=1, max_length=5)
    queue_candidates: list[int] = Field(default=[0, 8], min_length=1, max_length=5)
    arrival_rate_candidates: list[int] = Field(default=[8, 12, 16], min_length=1, max_length=5)
    arrival_candidates: list[Literal["deterministic", "poisson"]] = Field(
        default=["deterministic", "poisson"], min_length=2, max_length=2
    )
    duration_s: int = Field(default=2, ge=1, le=30)
    repetitions: int = Field(default=10, ge=2, le=20)
    seed: int = Field(default=20261118, ge=0, le=2_147_483_647)
    tool_delay_ms: float = Field(default=20, ge=0, le=500)
    model_delay_ms: float = Field(default=30, ge=0, le=500)
    slo: SLOProfile = Field(default_factory=SLOProfile)

    @model_validator(mode="after")
    def validate_study(self) -> SizingStudyRequest:
        axes = {
            "concurrency": self.concurrency_candidates,
            "queue_capacity": self.queue_candidates,
            "rate": self.arrival_rate_candidates,
            "arrival": self.arrival_candidates,
        }
        candidates = axes[self.focus]
        if len(set(candidates)) != len(candidates):
            raise ValueError("candidate values must be unique")
        if any(value < 1 or value > 16 for value in self.concurrency_candidates):
            raise ValueError("concurrency candidates must be between 1 and 16")
        if any(value < 0 or value > 64 for value in self.queue_candidates):
            raise ValueError("queue candidates must be between 0 and 64")
        if any(value < 1 or value > 20 for value in self.arrival_rate_candidates):
            raise ValueError("arrival-rate candidates must be between 1 and 20 requests/s")
        if self.focus == "arrival" and set(self.arrival_candidates) != {"deterministic", "poisson"}:
            raise ValueError("arrival focus must compare deterministic and Poisson arrivals")
        if len(candidates) * self.duration_s * self.repetitions > 600:
            raise ValueError("total offered workload duration must not exceed 600 seconds")
        return self
