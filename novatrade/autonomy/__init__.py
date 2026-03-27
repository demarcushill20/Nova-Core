"""NovaCore Autonomy Expansion — progress scoring, decision engine, and goal decomposition.

This package provides a multi-dimensional scoring system that continuously
evaluates system health, execution pipeline status, strategy validity,
risk engine integrity, and performance stability. It also includes an
adaptive decision engine and goal decomposition framework.
"""

from novatrade.autonomy.action_executor import ActionResult, DirectActionExecutor
from novatrade.autonomy.decision_context import (
    ContextAssembler,
    DecisionContext,
    GoalSummary,
    TaskSummary,
)
from novatrade.autonomy.decision_engine import (
    ActionMode,
    Decision,
    DecisionConfig,
    DecisionEngine,
)
from novatrade.autonomy.decomposition import (
    GoalDecomposer,
    GoalDecomposition,
    SubGoal,
    SubGoalStatus,
    build_novatrade_tree,
)
from novatrade.autonomy.investigation_executor import (
    InvestigationExecutor,
    InvestigationReport,
    InvestigationStep,
)
from novatrade.autonomy.outcome_analyzer import (
    DecisionSnapshot,
    EffectivenessReport,
    OutcomeAnalyzer,
)
from novatrade.autonomy.progress_scorer import ProgressScorer
from novatrade.autonomy.reasoning_engine import (
    ReasoningConfig,
    ReasoningEngine,
    ReasoningResult,
)
from novatrade.autonomy.schemas import (
    AlertLevel,
    DimensionScore,
    ProgressReport,
    ScoreTrend,
    ScoringConfig,
    SubMetric,
)
from novatrade.autonomy.task_generator import TaskSpec, TaskSpecGenerator

__all__ = [
    "ActionMode",
    "ActionResult",
    "AlertLevel",
    "ContextAssembler",
    "Decision",
    "DecisionConfig",
    "DecisionContext",
    "DecisionEngine",
    "DecisionSnapshot",
    "DimensionScore",
    "DirectActionExecutor",
    "EffectivenessReport",
    "GoalDecomposer",
    "GoalDecomposition",
    "GoalSummary",
    "InvestigationExecutor",
    "InvestigationReport",
    "InvestigationStep",
    "OutcomeAnalyzer",
    "ProgressReport",
    "ProgressScorer",
    "ReasoningConfig",
    "ReasoningEngine",
    "ReasoningResult",
    "ScoreTrend",
    "ScoringConfig",
    "SubGoal",
    "SubGoalStatus",
    "SubMetric",
    "TaskSpec",
    "TaskSpecGenerator",
    "TaskSummary",
    "build_novatrade_tree",
]
