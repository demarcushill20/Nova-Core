"""Unified pipeline orchestrator for NovaTrade strategy evaluation.

Re-exports the public API so callers can write::

    from novatrade.pipeline import run_pipeline, PipelineConfig, PipelineResult
"""

from novatrade.pipeline.checkpoint import (
    CampaignCheckpoint,
    delete_checkpoint,
    load_checkpoint,
    save_checkpoint,
    should_skip_experiment,
)
from novatrade.pipeline.orchestrator import (
    PipelineConfig,
    PipelineMode,
    PipelineResult,
    PipelineStage,
    run_pipeline,
)
from novatrade.pipeline.progress import (
    LoggingProgressCallback,
    ProgressCallback,
    TelegramProgressCallback,
)
from novatrade.pipeline.reporter import (
    campaign_report,
    format_pipeline_json,
    format_pipeline_report,
    report_to_file,
)

__all__ = [
    "CampaignCheckpoint",
    "LoggingProgressCallback",
    "PipelineConfig",
    "PipelineMode",
    "PipelineResult",
    "PipelineStage",
    "ProgressCallback",
    "TelegramProgressCallback",
    "campaign_report",
    "delete_checkpoint",
    "format_pipeline_json",
    "format_pipeline_report",
    "load_checkpoint",
    "report_to_file",
    "run_pipeline",
    "save_checkpoint",
    "should_skip_experiment",
]
