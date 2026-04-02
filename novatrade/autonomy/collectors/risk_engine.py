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

        Prioritises the authoritative novatrade_risk_state.json (written by
        RiskEngine every cycle) over fallback files.  If the primary source is
        fresh the confidence is high regardless of stale fallbacks.
        """
        now = time.time()

        # Primary source: novatrade_risk_state.json (authoritative, updated every cycle)
        primary = Path(self.base_path) / "STATE" / "novatrade_risk_state.json"
        if primary.exists():
            try:
                age_h = (now - primary.stat().st_mtime) / 3600.0
                if age_h < 0.5:
                    return 1.0  # primary source fresh — high confidence
                elif age_h < 2:
                    return 0.8
            except OSError:
                pass

        # Fallback: check secondary files when primary is missing or stale
        state_dir = Path(self.base_path) / "STATE" / "novatrade"
        key_files = [
            state_dir / "daily_loss_tracker.json",
            state_dir / "halt_state.json",
            state_dir / "live_metrics.json",
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
        elif fresh > 0:
            # At least one source is fresh — good enough for decent confidence
            return 0.8
        elif slightly_stale > 0:
            return 0.7
        elif available > 0:
            return 0.5

        return 0.3

    # ------------------------------------------------------------------
    # private helpers
    # ------------------------------------------------------------------

    def _check_drawdown_enforcement(self) -> tuple[float, float]:
        """Check if risk engine is actively tracking and enforcing drawdown limits.

        Reads daily_loss_tracker.json and/or novatrade_risk_state.json to
        determine actual drawdown usage.  The tracker file stores *config*
        fields (daily_loss_pct = the 5 % limit, NOT the actual loss).  Actual
        loss must be derived from ``day_reference - current_equity`` or from
        the risk-state snapshot written by RiskEngine.write_risk_state().
        """
        state_dir = Path(self.base_path) / "STATE" / "novatrade"

        # --- Primary source: novatrade_risk_state.json (written by RiskEngine) ---
        risk_state_path = Path(self.base_path) / "STATE" / "novatrade_risk_state.json"
        usage_ratio, score = self._try_risk_state(risk_state_path)
        if score is not None:
            return score, round(usage_ratio, 3)

        # --- Fallback: daily_loss_tracker.json ---
        tracker_path = state_dir / "daily_loss_tracker.json"
        if not tracker_path.exists():
            return 0.0, 0.0

        try:
            data = json.loads(tracker_path.read_text())
            if not isinstance(data, dict):
                return 30.0, 0.0  # file exists but not a dict

            age_min = (time.time() - tracker_path.stat().st_mtime) / 60.0

            # Compute actual loss from tracker state fields.
            # daily_loss_pct is the *limit config* (e.g. 5.0), NOT actual loss.
            day_ref = float(data.get("day_reference", 0) or 0)
            peak_eq = float(data.get("peak_equity_today", 0) or 0)
            initial = float(data.get("initial_account_size", 0) or 0)
            limit_pct = float(data.get("daily_loss_pct", 5.0) or 5.0)

            # Detect uninitialized tracker: never fed live data (primary source handles it)
            tracker_uninit = initial == 0 and day_ref == 0

            if initial > 0 and day_ref > 0:
                # Actual daily loss = day_reference - current (peak serves as proxy)
                actual_loss = max(day_ref - peak_eq, 0.0)
                daily_limit = initial * (limit_pct / 100.0)
                usage_ratio = actual_loss / daily_limit if daily_limit > 0 else 0.0
            else:
                # Tracker initialized but no live data, or partial state.
                # This is NOT a breach — the engine simply hasn't seen equity.
                usage_ratio = 0.0

            score = self._score_from_usage(usage_ratio)

            if tracker_uninit:
                # Uninitialized tracker — age is meaningless because the primary
                # source (novatrade_risk_state.json) is the real authority.
                # Give a healthy-idle score: system has risk tracking configured
                # but this specific fallback file was never activated.
                score = max(score, 80.0)
            else:
                # Context-aware freshness penalty (same logic as _try_risk_state)
                idle = usage_ratio == 0.0
                if idle:
                    if age_min > 240:
                        score = min(score, 80.0)
                    if age_min > 1440:
                        score = min(score, 60.0)
                else:
                    if age_min > 30:
                        score = min(score, 60.0)
                    if age_min > 120:
                        score = min(score, 40.0)

            return score, round(usage_ratio, 3)

        except (json.JSONDecodeError, OSError):
            return 30.0, 0.0

    def _try_risk_state(self, path: Path) -> tuple[float, float | None]:
        """Try to derive usage_ratio from RiskEngine's persisted state file.

        Freshness penalties are context-aware:
        - Idle state (0% drawdown, no breach): relaxed thresholds (4h/24h)
          because no money is at risk and the file legitimately doesn't update.
        - Active drawdown (>0%): tight thresholds (30min/2h) because we need
          fresh data when positions are open.
        - Breached: always 0 regardless of freshness.
        """
        if not path.exists():
            return 0.0, None
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                return 0.0, None

            age_min = (time.time() - path.stat().st_mtime) / 60.0

            # Check for explicit breach flag
            if data.get("breached"):
                score = 0.0
                usage_ratio = 1.0
            else:
                # Derive from drawdown fields
                dd_daily = float(data.get("daily_drawdown_pct", 0) or 0)
                dd_limit = float(data.get("max_daily_drawdown_pct", 5.0) or 5.0)
                usage_ratio = abs(dd_daily) / dd_limit if dd_limit > 0 else 0.0
                score = self._score_from_usage(usage_ratio)

            # Context-aware freshness penalty
            idle = usage_ratio == 0.0 and not data.get("breached") and not data.get("halted")
            if idle:
                # Idle state: relaxed thresholds — file doesn't update without activity
                if age_min > 240:  # 4 hours
                    score = min(score, 80.0)
                if age_min > 1440:  # 24 hours
                    score = min(score, 60.0)
            else:
                # Active drawdown: tight thresholds — need fresh data
                if age_min > 30:
                    score = min(score, 60.0)
                if age_min > 120:
                    score = min(score, 40.0)

            return usage_ratio, score
        except (json.JSONDecodeError, OSError):
            return 0.0, None

    @staticmethod
    def _score_from_usage(usage_ratio: float) -> float:
        """Convert drawdown usage ratio to a 0-100 score."""
        if usage_ratio >= 1.0:
            return 0.0  # FTMO limit breached
        if usage_ratio >= 0.8:
            return 30.0  # dangerously close
        if usage_ratio >= 0.5:
            return 70.0  # elevated but manageable
        return 100.0  # well within limits

    def _check_halt_state(self) -> tuple[float, float]:
        """Check halt status from halt_state.json or novatrade_risk_state.json.

        Reads the actual halt state rather than just checking file existence.
        - If halt_state shows active halt → score reflects halt appropriately
        - If no halt (or no halt file) → score high
        - If file is missing → check risk_state.json as fallback
        """
        state_dir = Path(self.base_path) / "STATE" / "novatrade"
        halt_path = state_dir / "halt_state.json"

        if not halt_path.exists():
            # No halt_state.json — check novatrade_risk_state.json as fallback
            risk_state = Path(self.base_path) / "STATE" / "novatrade_risk_state.json"
            if risk_state.exists():
                try:
                    data = json.loads(risk_state.read_text())
                    if isinstance(data, dict):
                        if data.get("breached") or data.get("halted"):
                            return 40.0, 1.0  # halted per risk state
                        return 100.0, 0.0  # not halted, authoritative source
                except (json.JSONDecodeError, OSError):
                    pass

            # No halt file, no risk state — check if NovaTrade is running
            if state_dir.is_dir() and any(state_dir.glob("*.json")):
                # Active state dir with other files.  Absence of halt_state.json
                # is the normal case when the risk engine has never halted.
                # Score 90 (not 100 because we'd prefer an explicit file).
                return 90.0, 0.0
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
        """Parse recent logs for pre-trade gate pass/fail ratio.

        When no gate events exist (idle market, no signals), check whether the
        risk gate *infrastructure* is present (config + code).  An idle but
        functional gate scores 75 (healthy-idle), not 50 (unknown).
        """
        logs_dir = Path(self.base_path) / "LOGS"
        if not logs_dir.is_dir():
            return 50.0, -1.0  # no logs — neutral

        passes = 0
        failures = 0
        cutoff = time.time() - 3600  # only files modified in last hour

        for log_file in logs_dir.iterdir():
            if not log_file.is_file():
                continue
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
            # No gate events in logs — check if the gate infrastructure exists.
            # If pre_trade_gate.py and risk_policy.yaml exist, the gate is
            # functional but idle (no signals to evaluate).
            gate_module = Path(self.base_path) / "novatrade" / "risk" / "pre_trade_gate.py"
            risk_policy = Path(self.base_path) / "docs" / "demo_test_run" / "risk_policy.yaml"
            if gate_module.exists() and risk_policy.exists():
                return 75.0, -1.0  # infrastructure present, no events (healthy idle)
            if gate_module.exists():
                return 65.0, -1.0  # gate code exists but no policy doc
            return 50.0, -1.0  # truly unknown

        pass_rate = passes / total
        return pass_rate * 100.0, pass_rate
