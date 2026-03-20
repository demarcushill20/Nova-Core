"""Unified pipeline orchestrator for NovaTrade strategy evaluation.

Re-exports the public API so callers can write::

    from novatrade.pipeline import run_pipeline, PipelineConfig, PipelineResult
"""

from novatrade.pipeline.orchestrator import (
    PipelineConfig,
    PipelineMode,
    PipelineResult,
    PipelineStage,
    run_pipeline,
)
from novatrade.pipeline.reporter import format_pipeline_json, format_pipeline_report

__all__ = [
    "PipelineConfig",
    "PipelineMode",
    "PipelineResult",
    "PipelineStage",
    "format_pipeline_json",
    "format_pipeline_report",
    "run_pipeline",
]
