"""Pydantic v2 models for the progress scoring system."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field, model_validator


class AlertLevel(str, Enum):
    """Traffic-light alert level derived from dimension scores."""

    RED = "RED"  # below 40
    YELLOW = "YELLOW"  # 40-70
    GREEN = "GREEN"  # above 70


class SubMetric(BaseModel):
    """A single measurable component within a scoring dimension."""

    name: str
    value: float = Field(ge=0.0, le=100.0)
    raw_value: float | None = None  # pre-normalisation value
    description: str = ""


class DimensionScore(BaseModel):
    """Scored assessment of one scoring dimension (e.g. System Health)."""

    name: str  # e.g. "System Health"
    score: float = Field(ge=0.0, le=100.0)
    alert_level: AlertLevel = AlertLevel.RED
    sub_metrics: list[SubMetric] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    collected_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def set_alert_level(self) -> DimensionScore:
        """Compute alert level from score after all fields are set."""
        if self.score >= 70:
            self.alert_level = AlertLevel.GREEN
        elif self.score >= 40:
            self.alert_level = AlertLevel.YELLOW
        else:
            self.alert_level = AlertLevel.RED
        return self


class ScoreTrend(BaseModel):
    """Rolling trend data for a single dimension."""

    current: float
    avg_1h: float | None = None
    avg_6h: float | None = None
    avg_24h: float | None = None
    direction: str = "stable"  # "improving", "stable", "degrading"


class ProgressReport(BaseModel):
    """Complete scoring report across all dimensions."""

    overall_score: float = Field(ge=0.0, le=100.0)
    overall_alert: AlertLevel = AlertLevel.RED
    dimensions: dict[str, DimensionScore] = Field(default_factory=dict)
    trends: dict[str, ScoreTrend] = Field(default_factory=dict)
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def set_overall_alert(self) -> ProgressReport:
        """Compute overall alert level from overall score."""
        if self.overall_score >= 70:
            self.overall_alert = AlertLevel.GREEN
        elif self.overall_score >= 40:
            self.overall_alert = AlertLevel.YELLOW
        else:
            self.overall_alert = AlertLevel.RED
        return self


class ScoringConfig(BaseModel):
    """Tunable configuration for the scoring engine."""

    weights: dict[str, float] = Field(
        default_factory=lambda: {
            "system_health": 0.2,
            "execution_pipeline": 0.25,
            "strategy_validity": 0.25,
            "risk_engine": 0.2,
            "performance_stability": 0.1,
        }
    )
    # Alert thresholds are fixed at 40 (RED) / 70 (GREEN) in model validators
    # on DimensionScore and ProgressReport — not configurable here by design.
    history_retention_days: int = 7
