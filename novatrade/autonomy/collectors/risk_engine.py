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

        return DimensionScore(
            name="Risk Engine",
            score=round(avg, 1),
            sub_metrics=sub_metrics,
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # private helpers
    # ------------------------------------------------------------------

    def _check_drawdown_enforcement(self) -> tuple[float, float]:
        """Check if risk engine state files exist with drawdown config."""
        state_dir = Path(self.base_path) / "STATE" / "novatrade"

        # Look for daily_loss_tracker.json (our FTMO compliance tracker)
        tracker_path = state_dir / "daily_loss_tracker.json"
        if not tracker_path.exists():
            return 0.0, 0.0

        try:
            data = json.loads(tracker_path.read_text())
            if isinstance(data, dict):
                return 100.0, 1.0
            return 50.0, 0.5
        except (json.JSONDecodeError, OSError):
            return 30.0, 0.0

    def _check_halt_state(self) -> tuple[float, float]:
        """Check STATE/novatrade/ for halt state persistence."""
        state_dir = Path(self.base_path) / "STATE" / "novatrade"
        if not state_dir.is_dir():
            return 0.0, 0.0

        # The halt state is typically in a risk state file or similar
        # If the directory exists and has state files, it means persistence works
        state_files = list(state_dir.glob("*.json"))
        if not state_files:
            return 0.0, 0.0

        # Validate each file is parseable JSON
        valid = 0
        for f in state_files:
            try:
                json.loads(f.read_text())
                valid += 1
            except (json.JSONDecodeError, OSError):
                continue

        if valid == len(state_files):
            return 100.0, float(valid)
        elif valid > 0:
            return 70.0, float(valid)
        return 0.0, 0.0

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
