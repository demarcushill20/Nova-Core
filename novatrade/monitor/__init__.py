"""NovaTrade monitoring — health snapshots, position reconciliation, and strategy degradation detection.

Key Components:
- health.py: Basic adapter health snapshots
- performance_analyzer.py: Real-time performance metrics
- degradation_detector.py: Hurst exponent, CUSUM, half-life analysis
- strategy_health_dashboard.py: Unified strategy health monitoring
- degradation_integration.py: Integration with existing monitoring systems
"""

# Core monitoring components
# Strategy degradation detection (P1 priority from research findings)
from .degradation_detector import (
    CusumMetrics,
    DegradationDetector,
    DegradationSnapshot,
    HalfLifeMetrics,
    HealthLevel,
    HurstMetrics,
)
from .degradation_integration import DegradationMonitoringIntegration
from .health import take_health_snapshot
from .performance_analyzer import PerformanceAnalyzer, PerformanceMetrics
from .strategy_health_dashboard import (
    HealthAlert,
    HealthScore,
    StrategyHealthDashboard,
    StrategyHealthMonitor,
)

__all__ = [
    "CusumMetrics",
    "DegradationDetector",
    "DegradationMonitoringIntegration",
    "DegradationSnapshot",
    "HalfLifeMetrics",
    "HealthAlert",
    "HealthLevel",
    "HealthScore",
    "HurstMetrics",
    "PerformanceAnalyzer",
    "PerformanceMetrics",
    "StrategyHealthDashboard",
    "StrategyHealthMonitor",
    "take_health_snapshot",
]
