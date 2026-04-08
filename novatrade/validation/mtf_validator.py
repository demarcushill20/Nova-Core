"""Multi-timeframe signal validation for NovaTrade.

Validates signal coherence between M5 and H1 timeframes to ensure:
- Signal direction consistency across timeframes
- IRB bar detection alignment
- EMA slope agreement between M5 and H1
- ADX threshold validation on both timeframes
- Proper signal timing and sequence validation

This addresses Sprint Item S6: Multi-timeframe Validation requirements.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

log = logging.getLogger("novatrade.validation.mtf_validator")


class ValidationResult(Enum):
    """MTF validation results."""

    PASS = "PASS"  # noqa: S105
    FAIL = "FAIL"
    WARNING = "WARNING"
    NO_DATA = "NO_DATA"


@dataclass
class MTFSignal:
    """Multi-timeframe signal data."""

    timestamp: float
    symbol: str
    timeframe: str
    direction: str  # "BUY" or "SELL"
    ema_slope: float
    adx: float
    atr_ratio: float
    confidence: float
    irb_detected: bool = False
    stop_level: float = 0.0
    entry_level: float = 0.0


@dataclass
class MTFValidationReport:
    """Multi-timeframe validation report."""

    timestamp: float
    symbol: str
    m5_signal: MTFSignal | None = None
    h1_signal: MTFSignal | None = None

    # Validation results
    direction_coherence: ValidationResult = ValidationResult.NO_DATA
    ema_alignment: ValidationResult = ValidationResult.NO_DATA
    adx_confirmation: ValidationResult = ValidationResult.NO_DATA
    timing_sequence: ValidationResult = ValidationResult.NO_DATA
    irb_alignment: ValidationResult = ValidationResult.NO_DATA

    # Detailed metrics
    direction_agreement: bool = False
    ema_slope_diff: float = 0.0
    adx_ratio: float = 0.0
    timing_offset_seconds: float = 0.0
    coherence_score: float = 0.0

    # Issues and warnings
    issues: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class MTFSignalValidator:
    """Multi-timeframe signal validator.

    Validates signal coherence between M5 and H1 timeframes according to
    IRB strategy requirements and FTMO compliance rules.
    """

    def __init__(
        self,
        m5_signals_path: Path | None = None,
        h1_signals_path: Path | None = None,
        output_path: Path | None = None,
    ) -> None:
        self.m5_signals_path = m5_signals_path or Path("OUTPUT/novatrade/m5_signals.jsonl")
        self.h1_signals_path = h1_signals_path or Path("OUTPUT/novatrade/h1_signals.jsonl")
        self.output_path = output_path or Path("OUTPUT/novatrade/mtf_validation.jsonl")

        # Validation thresholds
        self.min_adx_threshold = 20.0
        self.max_timing_offset_seconds = 900.0  # 15 minutes
        self.min_ema_slope_agreement = 0.8  # 80% agreement in slope direction
        self.min_coherence_score = 0.7  # 70% overall coherence

    def load_signals(self, timeframe: str) -> list[MTFSignal]:
        """Load signals from JSONL file for given timeframe."""
        if timeframe == "M5":
            signals_path = self.m5_signals_path
        elif timeframe == "H1":
            signals_path = self.h1_signals_path
        else:
            raise ValueError(f"Unsupported timeframe: {timeframe}")

        if not signals_path.exists():
            log.warning(f"Signals file not found: {signals_path}")
            return []

        signals = []
        try:
            with open(signals_path) as f:
                for line in f:
                    if line.strip():
                        data = json.loads(line)
                        signal = MTFSignal(
                            timestamp=data["timestamp"],
                            symbol=data["symbol"],
                            timeframe=timeframe,
                            direction=data["direction"],
                            ema_slope=data["ema_slope"],
                            adx=data["adx"],
                            atr_ratio=data["atr_ratio"],
                            confidence=data["confidence"],
                            irb_detected=data.get("irb_detected", False),
                            stop_level=data.get("stop_level", 0.0),
                            entry_level=data.get("entry_level", 0.0),
                        )
                        signals.append(signal)
        except Exception as e:
            log.error(f"Error loading signals from {signals_path}: {e}")

        return signals

    def find_matching_signals(
        self,
        m5_signals: list[MTFSignal],
        h1_signals: list[MTFSignal],
        symbol: str,
        time_window: float = 1800.0,  # 30 minutes
    ) -> list[tuple[MTFSignal | None, MTFSignal | None]]:
        """Find M5/H1 signal pairs within time window."""
        matches: list[tuple[MTFSignal | None, MTFSignal | None]] = []

        for m5_signal in m5_signals:
            if m5_signal.symbol != symbol:
                continue

            # Find closest H1 signal within time window
            best_h1 = None
            best_time_diff = float("inf")

            for h1_signal in h1_signals:
                if h1_signal.symbol != symbol:
                    continue

                time_diff = abs(m5_signal.timestamp - h1_signal.timestamp)
                if time_diff <= time_window and time_diff < best_time_diff:
                    best_h1 = h1_signal
                    best_time_diff = time_diff

            matches.append((m5_signal, best_h1))

        # Add unmatched H1 signals
        matched_h1_timestamps = {m[1].timestamp for m in matches if m[1] is not None}
        for h1_signal in h1_signals:
            if h1_signal.symbol == symbol and h1_signal.timestamp not in matched_h1_timestamps:
                matches.append((None, h1_signal))

        return matches

    def validate_direction_coherence(
        self,
        m5_signal: MTFSignal | None,
        h1_signal: MTFSignal | None,
    ) -> tuple[ValidationResult, bool, list[str]]:
        """Validate signal direction coherence between timeframes."""
        issues: list[str] = []

        if m5_signal is None or h1_signal is None:
            return ValidationResult.NO_DATA, False, issues

        direction_match = m5_signal.direction == h1_signal.direction

        if not direction_match:
            issues.append(f"Direction mismatch: M5={m5_signal.direction}, H1={h1_signal.direction}")
            return ValidationResult.FAIL, False, issues

        return ValidationResult.PASS, True, issues

    def validate_ema_alignment(
        self,
        m5_signal: MTFSignal | None,
        h1_signal: MTFSignal | None,
    ) -> tuple[ValidationResult, float, list[str]]:
        """Validate EMA slope alignment between timeframes."""
        issues: list[str] = []

        if m5_signal is None or h1_signal is None:
            return ValidationResult.NO_DATA, 0.0, issues

        slope_diff = abs(m5_signal.ema_slope - h1_signal.ema_slope)

        # Check if slopes have same sign (direction)
        same_direction = (m5_signal.ema_slope > 0 and h1_signal.ema_slope > 0) or (
            m5_signal.ema_slope < 0 and h1_signal.ema_slope < 0
        )

        if not same_direction:
            issues.append(f"EMA slope direction mismatch: M5={m5_signal.ema_slope:.6f}, H1={h1_signal.ema_slope:.6f}")
            return ValidationResult.FAIL, slope_diff, issues

        # Check slope magnitude alignment
        if slope_diff > 0.001:  # Threshold for slope difference
            issues.append(
                f"Large EMA slope difference: {slope_diff:.6f} "
                f"(M5={m5_signal.ema_slope:.6f}, H1={h1_signal.ema_slope:.6f})"
            )
            return ValidationResult.WARNING, slope_diff, issues

        return ValidationResult.PASS, slope_diff, issues

    def validate_adx_confirmation(
        self,
        m5_signal: MTFSignal | None,
        h1_signal: MTFSignal | None,
    ) -> tuple[ValidationResult, float, list[str]]:
        """Validate ADX threshold confirmation on both timeframes."""
        issues: list[str] = []

        if m5_signal is None or h1_signal is None:
            return ValidationResult.NO_DATA, 0.0, issues

        m5_adx_valid = m5_signal.adx >= self.min_adx_threshold
        h1_adx_valid = h1_signal.adx >= self.min_adx_threshold

        adx_ratio = min(m5_signal.adx, h1_signal.adx) / max(m5_signal.adx, h1_signal.adx)

        if not m5_adx_valid:
            issues.append(f"M5 ADX below threshold: {m5_signal.adx:.1f} < {self.min_adx_threshold}")

        if not h1_adx_valid:
            issues.append(f"H1 ADX below threshold: {h1_signal.adx:.1f} < {self.min_adx_threshold}")

        if not (m5_adx_valid and h1_adx_valid):
            return ValidationResult.FAIL, adx_ratio, issues

        # ADX values should be relatively close
        if adx_ratio < 0.5:
            issues.append(f"Large ADX difference: M5={m5_signal.adx:.1f}, H1={h1_signal.adx:.1f}")
            return ValidationResult.WARNING, adx_ratio, issues

        return ValidationResult.PASS, adx_ratio, issues

    def validate_timing_sequence(
        self,
        m5_signal: MTFSignal | None,
        h1_signal: MTFSignal | None,
    ) -> tuple[ValidationResult, float, list[str]]:
        """Validate signal timing and sequence."""
        issues: list[str] = []

        if m5_signal is None or h1_signal is None:
            return ValidationResult.NO_DATA, 0.0, issues

        timing_offset = abs(m5_signal.timestamp - h1_signal.timestamp)

        if timing_offset > self.max_timing_offset_seconds:
            issues.append(f"Large timing offset: {timing_offset:.0f}s > {self.max_timing_offset_seconds:.0f}s")
            return ValidationResult.FAIL, timing_offset, issues

        # M5 signals should generally occur before or close to H1 signals
        if m5_signal.timestamp > h1_signal.timestamp + 300:  # M5 more than 5 min after H1
            issues.append("M5 signal significantly after H1 signal (unusual timing)")
            return ValidationResult.WARNING, timing_offset, issues

        return ValidationResult.PASS, timing_offset, issues

    def validate_irb_alignment(
        self,
        m5_signal: MTFSignal | None,
        h1_signal: MTFSignal | None,
    ) -> tuple[ValidationResult, list[str]]:
        """Validate IRB bar detection alignment."""
        issues: list[str] = []

        if m5_signal is None or h1_signal is None:
            return ValidationResult.NO_DATA, issues

        # IRB detection should be consistent across timeframes
        if m5_signal.irb_detected != h1_signal.irb_detected:
            issues.append(f"IRB detection mismatch: M5={m5_signal.irb_detected}, H1={h1_signal.irb_detected}")
            return ValidationResult.WARNING, issues

        return ValidationResult.PASS, issues

    def compute_coherence_score(self, report: MTFValidationReport) -> float:
        """Compute overall coherence score (0-1)."""
        scores = []

        # Direction coherence (weight: 0.3)
        if report.direction_coherence == ValidationResult.PASS:
            scores.append((0.3, 1.0))
        elif report.direction_coherence == ValidationResult.WARNING:
            scores.append((0.3, 0.5))
        else:
            scores.append((0.3, 0.0))

        # EMA alignment (weight: 0.25)
        if report.ema_alignment == ValidationResult.PASS:
            scores.append((0.25, 1.0))
        elif report.ema_alignment == ValidationResult.WARNING:
            scores.append((0.25, 0.7))
        else:
            scores.append((0.25, 0.0))

        # ADX confirmation (weight: 0.25)
        if report.adx_confirmation == ValidationResult.PASS:
            scores.append((0.25, 1.0))
        elif report.adx_confirmation == ValidationResult.WARNING:
            scores.append((0.25, 0.6))
        else:
            scores.append((0.25, 0.0))

        # Timing sequence (weight: 0.2)
        if report.timing_sequence == ValidationResult.PASS:
            scores.append((0.2, 1.0))
        elif report.timing_sequence == ValidationResult.WARNING:
            scores.append((0.2, 0.8))
        else:
            scores.append((0.2, 0.0))

        if not scores:
            return 0.0

        weighted_sum = sum(weight * score for weight, score in scores)
        total_weight = sum(weight for weight, score in scores)

        return weighted_sum / total_weight if total_weight > 0 else 0.0

    def validate_signal_pair(
        self,
        m5_signal: MTFSignal | None,
        h1_signal: MTFSignal | None,
        symbol: str,
        timestamp: float,
    ) -> MTFValidationReport:
        """Validate a pair of M5/H1 signals."""
        report = MTFValidationReport(
            timestamp=timestamp,
            symbol=symbol,
            m5_signal=m5_signal,
            h1_signal=h1_signal,
        )

        # Validate direction coherence
        result, agreement, issues = self.validate_direction_coherence(m5_signal, h1_signal)
        report.direction_coherence = result
        report.direction_agreement = agreement
        report.issues.extend(issues)

        # Validate EMA alignment
        result, slope_diff, issues = self.validate_ema_alignment(m5_signal, h1_signal)
        report.ema_alignment = result
        report.ema_slope_diff = slope_diff
        report.issues.extend(issues)

        # Validate ADX confirmation
        result, adx_ratio, issues = self.validate_adx_confirmation(m5_signal, h1_signal)
        report.adx_confirmation = result
        report.adx_ratio = adx_ratio
        report.issues.extend(issues)

        # Validate timing sequence
        result, timing_offset, issues = self.validate_timing_sequence(m5_signal, h1_signal)
        report.timing_sequence = result
        report.timing_offset_seconds = timing_offset
        report.issues.extend(issues)

        # Validate IRB alignment
        result, issues = self.validate_irb_alignment(m5_signal, h1_signal)
        report.irb_alignment = result
        report.issues.extend(issues)

        # Compute overall coherence score
        report.coherence_score = self.compute_coherence_score(report)

        return report

    def validate_symbol(self, symbol: str) -> list[MTFValidationReport]:
        """Validate all signal pairs for a given symbol."""
        log.info(f"Validating MTF signals for {symbol}")

        # Load signals for both timeframes
        m5_signals = self.load_signals("M5")
        h1_signals = self.load_signals("H1")

        # Filter by symbol
        m5_signals = [s for s in m5_signals if s.symbol == symbol]
        h1_signals = [s for s in h1_signals if s.symbol == symbol]

        log.info(f"Found {len(m5_signals)} M5 signals, {len(h1_signals)} H1 signals")

        # Find matching signal pairs
        matches = self.find_matching_signals(m5_signals, h1_signals, symbol)

        # Validate each pair
        reports = []
        for m5_signal, h1_signal in matches:
            timestamp = (
                m5_signal.timestamp if m5_signal is not None else h1_signal.timestamp if h1_signal is not None else 0.0
            )
            report = self.validate_signal_pair(m5_signal, h1_signal, symbol, timestamp)
            reports.append(report)

        log.info(f"Generated {len(reports)} MTF validation reports for {symbol}")
        return reports

    def run_validation(self, symbols: list[str] | None = None) -> list[MTFValidationReport]:
        """Run comprehensive MTF validation."""
        if symbols is None:
            symbols = ["EURUSD"]  # Default symbol

        all_reports = []
        for symbol in symbols:
            symbol_reports = self.validate_symbol(symbol)
            all_reports.extend(symbol_reports)

        # Save reports
        self.save_reports(all_reports)

        return all_reports

    def save_reports(self, reports: list[MTFValidationReport]) -> None:
        """Save validation reports to JSONL file."""
        self.output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(self.output_path, "w") as f:
            for report in reports:
                data = {
                    "timestamp": report.timestamp,
                    "symbol": report.symbol,
                    "direction_coherence": report.direction_coherence.value,
                    "ema_alignment": report.ema_alignment.value,
                    "adx_confirmation": report.adx_confirmation.value,
                    "timing_sequence": report.timing_sequence.value,
                    "irb_alignment": report.irb_alignment.value,
                    "direction_agreement": report.direction_agreement,
                    "ema_slope_diff": report.ema_slope_diff,
                    "adx_ratio": report.adx_ratio,
                    "timing_offset_seconds": report.timing_offset_seconds,
                    "coherence_score": report.coherence_score,
                    "issues": report.issues,
                    "warnings": report.warnings,
                    "m5_signal": (
                        {
                            "timestamp": report.m5_signal.timestamp,
                            "direction": report.m5_signal.direction,
                            "ema_slope": report.m5_signal.ema_slope,
                            "adx": report.m5_signal.adx,
                            "confidence": report.m5_signal.confidence,
                        }
                        if report.m5_signal
                        else None
                    ),
                    "h1_signal": (
                        {
                            "timestamp": report.h1_signal.timestamp,
                            "direction": report.h1_signal.direction,
                            "ema_slope": report.h1_signal.ema_slope,
                            "adx": report.h1_signal.adx,
                            "confidence": report.h1_signal.confidence,
                        }
                        if report.h1_signal
                        else None
                    ),
                }
                f.write(json.dumps(data) + "\n")

        log.info(f"Saved {len(reports)} MTF validation reports to {self.output_path}")

    def generate_summary(self, reports: list[MTFValidationReport]) -> dict[str, Any]:
        """Generate validation summary statistics."""
        if not reports:
            return {"total_reports": 0}

        total_reports = len(reports)

        # Count by result type
        direction_pass = sum(1 for r in reports if r.direction_coherence == ValidationResult.PASS)
        ema_pass = sum(1 for r in reports if r.ema_alignment == ValidationResult.PASS)
        adx_pass = sum(1 for r in reports if r.adx_confirmation == ValidationResult.PASS)
        timing_pass = sum(1 for r in reports if r.timing_sequence == ValidationResult.PASS)

        # Overall pass rate (all validations pass)
        all_pass = sum(
            1
            for r in reports
            if all(
                [
                    r.direction_coherence == ValidationResult.PASS,
                    r.ema_alignment == ValidationResult.PASS,
                    r.adx_confirmation == ValidationResult.PASS,
                    r.timing_sequence == ValidationResult.PASS,
                ]
            )
        )

        # Average coherence score
        avg_coherence = sum(r.coherence_score for r in reports) / total_reports

        return {
            "total_reports": total_reports,
            "direction_coherence_pass_rate": direction_pass / total_reports,
            "ema_alignment_pass_rate": ema_pass / total_reports,
            "adx_confirmation_pass_rate": adx_pass / total_reports,
            "timing_sequence_pass_rate": timing_pass / total_reports,
            "overall_pass_rate": all_pass / total_reports,
            "average_coherence_score": avg_coherence,
            "high_coherence_signals": sum(1 for r in reports if r.coherence_score >= 0.8),
            "low_coherence_signals": sum(1 for r in reports if r.coherence_score < 0.5),
        }


def main() -> None:
    """CLI entry point for MTF validation."""
    import argparse

    parser = argparse.ArgumentParser(description="Multi-timeframe signal validation")
    parser.add_argument("--symbols", nargs="+", default=["EURUSD"], help="Symbols to validate")
    parser.add_argument("--m5-signals", type=Path, help="M5 signals JSONL file")
    parser.add_argument("--h1-signals", type=Path, help="H1 signals JSONL file")
    parser.add_argument("--output", type=Path, help="Output JSONL file")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose logging")

    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.INFO)

    validator = MTFSignalValidator(
        m5_signals_path=args.m5_signals,
        h1_signals_path=args.h1_signals,
        output_path=args.output,
    )

    reports = validator.run_validation(args.symbols)
    summary = validator.generate_summary(reports)

    print("\nMTF Validation Summary:")
    print(f"Total reports: {summary['total_reports']}")
    print(f"Direction coherence pass rate: {summary['direction_coherence_pass_rate']:.1%}")
    print(f"EMA alignment pass rate: {summary['ema_alignment_pass_rate']:.1%}")
    print(f"ADX confirmation pass rate: {summary['adx_confirmation_pass_rate']:.1%}")
    print(f"Timing sequence pass rate: {summary['timing_sequence_pass_rate']:.1%}")
    print(f"Overall pass rate: {summary['overall_pass_rate']:.1%}")
    print(f"Average coherence score: {summary['average_coherence_score']:.3f}")
    print(f"High coherence signals (≥0.8): {summary['high_coherence_signals']}")
    print(f"Low coherence signals (<0.5): {summary['low_coherence_signals']}")


if __name__ == "__main__":
    main()
