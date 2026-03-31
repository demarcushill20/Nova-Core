"""Risk Engine collector — drawdown enforcement, halt persistence, capital config."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from novatrade.autonomy.collectors.base import BaseCollector
from novatrade.autonomy.schemas import DimensionScore, SubMetric


class RiskCollector(BaseCollector):
    """Measures risk engine integrity: drawdown, halt state, capital, gate pass rate."""

    async def collect(self) -> DimensionScore:
        warnings: list[str] = []
        sub_metrics: list[SubMetric] = []

        # --- drawdown_enforcement ---
        try:
            dd_score, dd_raw = self._check_drawdown_enforcement()
            sub_metrics.append(
                SubMetric(
                    name="drawdown_enforcement",
                    value=self._safe_score(dd_score),
                    raw_value=dd_raw,
                    description="Drawdown limit configured and enforced",
                )
            )
        except Exception as exc:
            self.log.warning("drawdown_enforcement failed: %s", exc)
            warnings.append(f"drawdown_enforcement failed: {exc}")
            sub_metrics.append(SubMetric(name="drawdown_enforcement", value=0.0))

        # --- halt_state_persistence ---
        try:
            halt_score, halt_raw = self._check_halt_state()
            sub_metrics.append(
                SubMetric(
                    name="halt_state_persistence",
                    value=self._safe_score(halt_score),
                    raw_value=halt_raw,
                    description="Halt state file exists and is valid JSON",
                )
            )
        except Exception as exc:
            self.log.warning("halt_state_persistence failed: %s", exc)
            warnings.append(f"halt_state_persistence failed: {exc}")
            sub_metrics.append(SubMetric(name="halt_state_persistence", value=0.0))

        # --- capital_allocation ---
        try:
            cap_score, cap_raw = self._check_capital_allocation()
            sub_metrics.append(
                SubMetric(
                    name="capital_allocation",
                    value=self._safe_score(cap_score),
                    raw_value=cap_raw,
                    description="Position sizing config present",
                )
            )
        except Exception as exc:
            self.log.warning("capital_allocation failed: %s", exc)
            warnings.append(f"capital_allocation failed: {exc}")
            sub_metrics.append(SubMetric(name="capital_allocation", value=0.0))

        # --- risk_gate_pass_rate ---
        try:
            gate_score, gate_raw = self._check_gate_pass_rate()
            sub_metrics.append(
                SubMetric(
                    name="risk_gate_pass_rate",
                    value=self._safe_score(gate_score),
                    raw_value=gate_raw,
                    description="Pre-trade gate pass/fail ratio",
                )
            )
        except Exception as exc:
            self.log.warning("risk_gate_pass_rate failed: %s", exc)
            warnings.append(f"risk_gate_pass_rate failed: {exc}")
            sub_metrics.append(SubMetric(name="risk_gate_pass_rate", value=0.0))

        avg = sum(m.value for m in sub_metrics) / max(len(sub_metrics), 1)

        # Compute confidence based on risk data recency
        confidence = self._compute_confidence(warnings)

        return DimensionScore(
            name="Risk Engine",
            score=round(avg, 1),
            confidence=confidence,
            sub_metrics=sub_metrics,
            warnings=warnings,
        )

    def _compute_confidence(self, warnings: list[str]) -> float:
        """Compute confidence based on risk state file freshness.

        Checks daily_loss_tracker.json and halt_state.json for recency.
        """
        now = time.time()
        state_dir = Path(self.base_path) / "STATE" / "novatrade"

        key_files = [
            state_dir / "daily_loss_tracker.json",
            state_dir / "halt_state.json",
        ]

        available = 0
        fresh = 0  # updated within 30 min
        slightly_stale = 0  # 30min-2h

        for kf in key_files:
            if kf.exists():
                available += 1
                try:
                    age_h = (now - kf.stat().st_mtime) / 3600.0
                    if age_h < 0.5:
                        fresh += 1
                    elif age_h < 2:
                        slightly_stale += 1
                except OSError:
                    pass

        if available == 0:
            warnings.append("no risk state files — confidence very low")
            return 0.1

        if fresh == available:
            return 1.0
        elif fresh + slightly_stale == available:
            return 0.8
        elif available > 0:
            return 0.6

        return 0.3

    # ------------------------------------------------------------------
    # private helpers
    # ------------------------------------------------------------------

    def _check_drawdown_enforcement(self) -> tuple[float, float]:
        """Check if risk engine is actively tracking and enforcing drawdown limits.

        Parses actual drawdown values and verifies the tracker is being updated,
        not just that the file exists.
        """
        state_dir = Path(self.base_path) / "STATE" / "novatrade"

        # Look for daily_loss_tracker.json (our FTMO compliance tracker)
        tracker_path = state_dir / "daily_loss_tracker.json"
        if not tracker_path.exists():
            return 0.0, 0.0

        try:
            data = json.loads(tracker_path.read_text())
            if not isinstance(data, dict):
                return 30.0, 0.0  # file exists but not a dict

            # Check data freshness — was this updated recently?
            age_min = (time.time() - tracker_path.stat().st_mtime) / 60.0

            # Parse actual drawdown values
            daily_loss = data.get("daily_loss_pct", data.get("daily_loss", 0))
            max_daily = data.get("max_daily_loss_pct", data.get("limit", 5.0))  # FTMO default 5%
            try:
                daily_loss = float(daily_loss or 0)
                max_daily = float(max_daily or 5.0)
            except (TypeError, ValueError):
                daily_loss = 0.0
                max_daily = 5.0

            # Score based on drawdown proximity to FTMO limits
            if max_daily > 0:
                usage_ratio = abs(daily_loss) / max_daily
            else:
                usage_ratio = 0.0

            if usage_ratio >= 1.0:
                score = 0.0  # FTMO limit breached
            elif usage_ratio >= 0.8:
                score = 30.0  # dangerously close to limit
            elif usage_ratio >= 0.5:
                score = 70.0  # elevated but manageable
            else:
                score = 100.0  # well within limits

            # Penalize stale tracking data
            if age_min > 30:
                score = min(score, 60.0)  # stale tracker is concerning
            if age_min > 120:
                score = min(score, 40.0)  # very stale

            return score, round(usage_ratio, 3)

        except (json.JSONDecodeError, OSError):
            return 30.0, 0.0

    def _check_halt_state(self) -> tuple[float, float]:
        """Check STATE/novatrade/halt_state.json content for halt status.

        Reads the actual halt state rather than just checking file existence.
        - If halt_state shows active halt → score reflects halt appropriately
        - If no halt (or no halt file) → score high
        - If file is missing → assume not halted but with low confidence
        """
        state_dir = Path(self.base_path) / "STATE" / "novatrade"
        halt_path = state_dir / "halt_state.json"

        if not halt_path.exists():
            # No halt file → assume not halted, but this is uncertain
            # Check if state dir at least exists with some files
            if state_dir.is_dir() and any(state_dir.glob("*.json")):
                return 80.0, 0.0  # state dir active, no halt file = probably OK
            return 50.0, -1.0  # no state dir at all — uncertain

        try:
            data = json.loads(halt_path.read_text())
            if not isinstance(data, dict):
                return 60.0, 0.0  # file exists but not proper format

            halted = data.get("halted", data.get("is_halted", False))

            if halted:
                # Trading is halted — this is a valid risk state
                # Score depends on whether halt is protective (good) vs broken (bad)
                halt_type = data.get("type", data.get("halt_type", ""))
                if halt_type in ("manual", "protective", "scheduled"):
                    # Intentional halt — risk engine is working correctly
                    return 70.0, 1.0
                else:
                    # Unplanned halt — risk engine triggered on anomaly
                    return 40.0, 1.0
            else:
                # Not halted — normal operation
                return 100.0, 0.0

        except (json.JSONDecodeError, OSError):
            return 50.0, 0.0  # can't read halt file → uncertain

    def _check_capital_allocation(self) -> tuple[float, float]:
        """Check if position sizing / lot config exists."""
        state_dir = Path(self.base_path) / "STATE" / "novatrade"
        lot_path = state_dir / "lot_history.json"

        if lot_path.exists():
            try:
                data = json.loads(lot_path.read_text())
                if isinstance(data, (dict, list)):
                    return 100.0, 1.0
            except (json.JSONDecodeError, OSError):
                return 30.0, 0.0

        # Also check for risk policy config
        risk_policy = Path(self.base_path) / "docs" / "demo_test_run" / "risk_policy.yaml"
        if risk_policy.exists():
            return 80.0, 1.0

        return 0.0, 0.0

    _GATE_PASS_RE = re.compile(r"(?:pre.?trade.?gate|PreTradeGate).*(?:ALLOW|PASS(?:ED)?)\b", re.IGNORECASE)
    _GATE_FAIL_RE = re.compile(r"(?:pre.?trade.?gate|PreTradeGate).*(?:REJECT|DENY|FAIL(?:ED)?|HALT)\b", re.IGNORECASE)

    def _check_gate_pass_rate(self) -> tuple[float, float]:
        """Parse recent logs for pre-trade gate pass/fail ratio."""
        logs_dir = Path(self.base_path) / "LOGS"
        if not logs_dir.is_dir():
            return 50.0, -1.0  # no logs — neutral

        passes = 0
        failures = 0
        cutoff = time.time() - 3600  # only files modified in last hour

        for log_file in logs_dir.iterdir():
            if not log_file.is_file():
                continue
            # C2: skip files not modified in the last hour
            try:
                if log_file.stat().st_mtime < cutoff:
                    continue
            except OSError:
                continue
            try:
                text = log_file.read_text(errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                if self._GATE_PASS_RE.search(line):
                    passes += 1
                elif self._GATE_FAIL_RE.search(line):
                    failures += 1

        total = passes + failures
        if total == 0:
            return 50.0, -1.0  # no gate events — neutral

        pass_rate = passes / total
        return pass_rate * 100.0, pass_rate
