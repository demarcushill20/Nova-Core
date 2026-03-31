"""Enhanced signal status endpoint for NovaTrade monitoring.

Provides detailed real-time analysis of signal conditions, helping operators
understand why strategies are or aren't triggering trades. Integrates with
the SignalConditionAnalyzer to provide actionable insights.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from novatrade.monitor.signal_condition_analyzer import SignalConditionAnalyzer

log = logging.getLogger("novatrade.monitor.signal_status")

# Global analyzer instance (would be properly injected in production)
_signal_analyzer: SignalConditionAnalyzer | None = None


def get_signal_analyzer() -> SignalConditionAnalyzer:
    """Get or create global signal analyzer instance."""
    global _signal_analyzer
    if _signal_analyzer is None:
        _signal_analyzer = SignalConditionAnalyzer()
    return _signal_analyzer


def create_signal_status_router() -> APIRouter:
    """Create FastAPI router for signal status endpoints."""
    router = APIRouter()

    @router.get("/signal-conditions")
    async def get_signal_conditions():
        """Get detailed signal condition analysis."""
        try:
            analyzer = get_signal_analyzer()

            # Get current status
            current_status = analyzer.get_current_status_message()

            # Get summary stats
            summary = analyzer.get_summary_stats(lookback_bars=20)

            # Get recent analysis if available
            recent_analysis = None
            if analyzer.history:
                latest = analyzer.history[-1]
                recent_analysis = {
                    "timestamp": latest.timestamp,
                    "bar_index": latest.bar_index,
                    "overall_status": latest.overall_status,
                    "proximity_score": round(latest.proximity_score, 2),
                    "conditions": [
                        {
                            "name": c.name,
                            "status": c.status.value,
                            "actual": round(c.actual_value, 5),
                            "threshold": round(c.threshold_value, 5),
                            "proximity_pct": round(c.proximity_pct, 2),
                            "proximity_level": c.proximity_level.value,
                            "message": c.message,
                        }
                        for c in latest.conditions
                    ],
                    "blocking_conditions": latest.blocking_conditions,
                    "near_conditions": latest.near_conditions,
                }

            return JSONResponse(
                {
                    "status": "ok",
                    "signal_analysis": {
                        "current_status_message": current_status,
                        "summary_stats": summary,
                        "latest_analysis": recent_analysis,
                    },
                }
            )

        except Exception as e:
            log.error("Error getting signal conditions: %s", e)
            return JSONResponse(status_code=500, content={"error": f"Signal condition analysis failed: {e!s}"})

    @router.get("/signal-history")
    async def get_signal_history(bars: int = 10):
        """Get recent signal condition history."""
        try:
            analyzer = get_signal_analyzer()

            if not analyzer.history:
                return JSONResponse({"status": "ok", "message": "No signal history available yet", "history": []})

            # Get recent history
            recent = analyzer.history[-bars:] if bars > 0 else analyzer.history

            history_data = []
            for analysis in recent:
                history_data.append(
                    {
                        "timestamp": analysis.timestamp,
                        "bar_index": analysis.bar_index,
                        "overall_status": analysis.overall_status,
                        "proximity_score": round(analysis.proximity_score, 2),
                        "blocking_conditions": analysis.blocking_conditions,
                        "conditions_count": len(analysis.conditions),
                        "passed_conditions": len([c for c in analysis.conditions if c.status.value == "PASSED"]),
                    }
                )

            return JSONResponse(
                {"status": "ok", "bars_requested": bars, "bars_returned": len(history_data), "history": history_data}
            )

        except Exception as e:
            log.error("Error getting signal history: %s", e)
            return JSONResponse(status_code=500, content={"error": f"Signal history retrieval failed: {e!s}"})

    return router


def format_signal_status_for_console() -> str:
    """Format signal status for console/log output."""
    try:
        analyzer = get_signal_analyzer()

        if not analyzer.history:
            return "📊 Signal Status: No analysis data available yet"

        status_msg = analyzer.get_current_status_message()
        summary = analyzer.get_summary_stats(lookback_bars=20)

        output = [
            "📊 Signal Status Report:",
            f"  Current: {status_msg}",
            f"  Last 20 bars: {summary.get('signals_triggered', 0)} signals,"
            f" {summary.get('near_signals', 0)} near-misses",
            f"  Signal rate: {summary.get('signal_rate_pct', 0):.1f}%"
            f" | Avg proximity: {summary.get('avg_proximity_score', 0):.1f}/100",
        ]

        # Add most common blockers
        blockers = summary.get("most_common_blockers", {})
        if blockers:
            top_blocker = list(blockers.items())[0]
            output.append(f"  Main blocker: {top_blocker[0]} ({top_blocker[1]} times)")

        # Add recent trend
        trend = summary.get("recent_proximity_trend", [])
        if trend:
            output.append(f"  Recent trend: {' → '.join(map(str, trend))}")

        return "\n".join(output)

    except Exception as e:
        return f"📊 Signal Status: Error - {e}"
