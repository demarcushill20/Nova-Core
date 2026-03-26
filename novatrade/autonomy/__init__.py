"""NovaCore Autonomy Expansion — progress scoring, decision engine, and goal decomposition.

This package provides a multi-dimensional scoring system that continuously
evaluates system health, execution pipeline status, strategy validity,
risk engine integrity, and performance stability. It also includes an
adaptive decision engine and goal decomposition framework.
"""

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
from novatrade.autonomy.progress_scorer import ProgressScorer
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
    "AlertLevel",
    "ContextAssembler",
    "Decision",
    "DecisionConfig",
    "DecisionContext",
    "DecisionEngine",
    "DimensionScore",
    "GoalDecomposer",
    "GoalDecomposition",
    "GoalSummary",
    "ProgressReport",
    "ProgressScorer",
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
