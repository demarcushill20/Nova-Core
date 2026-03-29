"""Calibration & after-action learning for the Intelligent Dynamic Block Scheduler.

Phase 5: Analyses execution history to compute per-category bias multipliers,
detect recurring estimation patterns, and generate human-readable reports.
Calibration tables persist to STATE/scheduler/calibration.json for use by
the block packer when correcting future time estimates.
"""

from __future__ import annotations

import json
import logging
import math
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import mean, stdev

from pydantic import BaseModel, ConfigDict, Field, model_validator

from utils.scheduler.execution_log import ExecutionRecord
from utils.scheduler.work_unit import TaskClass, WorkMode

log = logging.getLogger("nova.scheduler.calibration")

# ---------------------------------------------------------------------------
# Default paths
# ---------------------------------------------------------------------------

_DEFAULT_CALIBRATION_PATH = Path("STATE/scheduler/calibration.json")

# Bias multiplier clamp range
_BIAS_MIN = 0.5
_BIAS_MAX = 3.0


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class CalibrationEntry(BaseModel):
    """Calibration data for a single (task_class, work_mode) pair."""

    model_config = ConfigDict(extra="forbid")

    task_class: TaskClass
    work_mode: WorkMode
    sample_count: int
    mean_variance_ratio: float
    std_variance_ratio: float
    bias_multiplier: float  # clamped to [0.5, 3.0]
    last_updated: str


class CalibrationTable(BaseModel):
    """Calibration lookup table with per-pair entries and a global fallback."""

    model_config = ConfigDict(extra="forbid")

    entries: dict[str, CalibrationEntry] = Field(default_factory=dict)
    global_bias: float = 1.0
    last_computed: str = ""

    @model_validator(mode="after")
    def _clamp_global_bias(self) -> CalibrationTable:
        """Clamp global_bias to [0.5, 3.0] at the model level."""
        self.global_bias = max(_BIAS_MIN, min(_BIAS_MAX, self.global_bias))
        return self


# ---------------------------------------------------------------------------
# Key helper
# ---------------------------------------------------------------------------


def _pair_key(task_class: TaskClass, work_mode: WorkMode) -> str:
    """Build the dict key for a (task_class, work_mode) pair."""
    return f"{task_class.value}_{work_mode.value}"


def _clamp_bias(value: float) -> float:
    """Clamp a bias multiplier to [0.5, 3.0]."""
    return max(_BIAS_MIN, min(_BIAS_MAX, value))


# ---------------------------------------------------------------------------
# Compute calibration
# ---------------------------------------------------------------------------


def compute_calibration(
    records: list[ExecutionRecord],
    window: int = 30,
) -> CalibrationTable:
    """Compute a calibration table from execution records.

    Groups records by (task_class, work_mode). For each pair, uses the
    last ``window`` records to compute mean and std of variance_ratio.
    Pairs with fewer than 3 samples fall back to the global average.

    Parameters
    ----------
    records:
        Execution records (chronological order, most recent last).
    window:
        Maximum number of records per pair to use.

    Returns
    -------
    CalibrationTable with entries for each observed pair.
    """
    if not records:
        return CalibrationTable(
            global_bias=1.0,
            last_computed=datetime.now(timezone.utc).isoformat(),
        )

    # Group by pair
    groups: dict[str, list[ExecutionRecord]] = defaultdict(list)
    for rec in records:
        key = _pair_key(rec.task_class, rec.work_mode)
        groups[key].append(rec)

    # Global variance ratios (all records, windowed per-pair)
    all_ratios: list[float] = []

    now_iso = datetime.now(timezone.utc).isoformat()
    entries: dict[str, CalibrationEntry] = {}

    for key, recs in groups.items():
        # Use last `window` records
        windowed = recs[-window:]
        ratios = [r.variance_ratio for r in windowed if math.isfinite(r.variance_ratio)]
        all_ratios.extend(ratios)

        sample_count = len(ratios)
        mean_vr = mean(ratios)
        std_vr = stdev(ratios) if len(ratios) >= 2 else 0.0

        entries[key] = CalibrationEntry(
            task_class=windowed[0].task_class,
            work_mode=windowed[0].work_mode,
            sample_count=sample_count,
            mean_variance_ratio=mean_vr,
            std_variance_ratio=std_vr,
            bias_multiplier=_clamp_bias(mean_vr),
            last_updated=now_iso,
        )

    # Global bias = mean of all windowed variance ratios
    global_bias = _clamp_bias(mean(all_ratios)) if all_ratios else 1.0

    # For entries with <3 samples, override bias_multiplier with global
    for entry in entries.values():
        if entry.sample_count < 3:
            entry.bias_multiplier = global_bias

    return CalibrationTable(
        entries=entries,
        global_bias=global_bias,
        last_computed=now_iso,
    )


# ---------------------------------------------------------------------------
# Lookup helpers
# ---------------------------------------------------------------------------


def get_bias_multiplier(
    table: CalibrationTable,
    task_class: TaskClass,
    work_mode: WorkMode,
) -> float:
    """Look up the bias multiplier for a (task_class, work_mode) pair.

    Returns the entry's bias_multiplier if found and sample_count >= 3,
    otherwise returns the global bias (clamped to [0.5, 3.0]).
    """
    key = _pair_key(task_class, work_mode)
    entry = table.entries.get(key)
    if entry is not None and entry.sample_count >= 3:
        return entry.bias_multiplier
    return _clamp_bias(table.global_bias)


def correct_estimate(
    estimated_min: float,
    task_class: TaskClass,
    work_mode: WorkMode,
    table: CalibrationTable,
) -> float:
    """Apply calibration bias to a raw time estimate.

    Returns estimated_min * bias_multiplier for the given pair.
    """
    multiplier = get_bias_multiplier(table, task_class, work_mode)
    return estimated_min * multiplier


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def save_calibration(
    table: CalibrationTable,
    path: Path | None = None,
) -> Path:
    """Save calibration table to STATE/scheduler/calibration.json.

    Uses atomic write (tempfile + rename) to prevent corruption.
    """
    target = path or _DEFAULT_CALIBRATION_PATH
    target.parent.mkdir(parents=True, exist_ok=True)

    data = table.model_dump_json(indent=2)

    fd = tempfile.NamedTemporaryFile(  # noqa: SIM115 — atomic write pattern
        mode="w",
        encoding="utf-8",
        dir=str(target.parent),
        suffix=".tmp",
        delete=False,
    )
    try:
        fd.write(data)
        fd.flush()
        fd.close()
        Path(fd.name).replace(target)
    except Exception:
        try:
            Path(fd.name).unlink(missing_ok=True)
        except OSError:
            pass
        raise

    log.debug("Saved calibration table to %s", target)
    return target


def load_calibration(path: Path | None = None) -> CalibrationTable:
    """Load calibration table from STATE/scheduler/calibration.json.

    Returns an empty table if the file does not exist or is corrupt.
    """
    target = path or _DEFAULT_CALIBRATION_PATH

    if not target.exists():
        return CalibrationTable()

    try:
        raw = target.read_text(encoding="utf-8")
        return CalibrationTable.model_validate_json(raw)
    except (json.JSONDecodeError, ValueError, OSError) as exc:
        log.warning("Failed to load calibration from %s: %s", target, exc)
        return CalibrationTable()


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def generate_calibration_report(
    table: CalibrationTable,
    records: list[ExecutionRecord],
) -> str:
    """Generate a Markdown calibration report.

    Shows bias by category, over/under-estimation leaders, sample counts,
    and trend information if enough data is available.
    """
    lines: list[str] = []
    lines.append("# Calibration Report")
    lines.append("")
    lines.append(f"**Last computed:** {table.last_computed or 'N/A'}")
    lines.append(f"**Global bias:** {_clamp_bias(table.global_bias):.3f}")
    lines.append(f"**Total records analysed:** {len(records)}")
    lines.append("")

    if not table.entries:
        lines.append("_No calibration entries yet. Need more execution data._")
        return "\n".join(lines)

    # Bias by pair table
    lines.append("## Bias by Task Type / Work Mode")
    lines.append("")
    lines.append("| Task Class | Work Mode | Samples | Mean Ratio | Std | Bias Multiplier |")
    lines.append("|------------|-----------|---------|------------|-----|-----------------|")

    # Sort entries by bias_multiplier descending (most over-estimated first)
    sorted_entries = sorted(
        table.entries.values(),
        key=lambda e: e.bias_multiplier,
        reverse=True,
    )

    for entry in sorted_entries:
        lines.append(
            f"| {entry.task_class.value} | {entry.work_mode.value} "
            f"| {entry.sample_count} | {entry.mean_variance_ratio:.3f} "
            f"| {entry.std_variance_ratio:.3f} | {entry.bias_multiplier:.3f} |"
        )

    lines.append("")

    # Most over-estimated (bias > 1.0)
    over = [e for e in sorted_entries if e.bias_multiplier > 1.05]
    under = [e for e in sorted_entries if e.bias_multiplier < 0.95]

    if over:
        lines.append("## Most Under-Estimated (tasks take longer than planned)")
        lines.append("")
        for entry in over[:5]:
            pct = (entry.bias_multiplier - 1.0) * 100
            lines.append(
                f"- **{entry.task_class.value}/{entry.work_mode.value}**: "
                f"+{pct:.0f}% over estimate ({entry.sample_count} samples)"
            )
        lines.append("")

    if under:
        lines.append("## Most Over-Estimated (tasks finish faster than planned)")
        lines.append("")
        for entry in reversed(under[-5:]):
            pct = (1.0 - entry.bias_multiplier) * 100
            lines.append(
                f"- **{entry.task_class.value}/{entry.work_mode.value}**: "
                f"-{pct:.0f}% under estimate ({entry.sample_count} samples)"
            )
        lines.append("")

    # Trend analysis (compare first half vs second half of records)
    if len(records) >= 10:
        mid = len(records) // 2
        first_half = records[:mid]
        second_half = records[mid:]
        first_mean = mean(r.variance_ratio for r in first_half)
        second_mean = mean(r.variance_ratio for r in second_half)

        lines.append("## Trend")
        lines.append("")
        lines.append(f"- First half mean ratio: {first_mean:.3f}")
        lines.append(f"- Second half mean ratio: {second_mean:.3f}")

        if second_mean < first_mean * 0.95:
            lines.append("- **Improving**: estimates are becoming more accurate")
        elif second_mean > first_mean * 1.05:
            lines.append("- **Degrading**: estimates are becoming less accurate")
        else:
            lines.append("- **Stable**: estimation accuracy is consistent")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Pattern detection
# ---------------------------------------------------------------------------


def detect_patterns(  # noqa: C901
    records: list[ExecutionRecord],
    min_samples: int = 5,
) -> list[str]:
    """Detect recurring estimation patterns from execution history.

    Returns a list of human-readable pattern strings.  Only patterns
    with at least ``min_samples`` supporting records are reported.
    """
    if not records:
        return []

    patterns: list[str] = []

    # Group by work_mode
    by_mode: dict[str, list[float]] = defaultdict(list)
    for rec in records:
        by_mode[rec.work_mode.value].append(rec.variance_ratio)

    # Group by task_class
    by_class: dict[str, list[float]] = defaultdict(list)
    for rec in records:
        by_class[rec.task_class.value].append(rec.variance_ratio)

    # Pattern: work_mode averages
    for mode, ratios in sorted(by_mode.items()):
        if len(ratios) < min_samples:
            continue
        avg = mean(ratios)
        if avg > 1.1:
            patterns.append(f"{mode.upper()} tasks average {avg:.1f}x estimated duration")
        elif avg < 0.9:
            pct = (1.0 - avg) * 100
            patterns.append(f"{mode.upper()} tasks are typically overestimated by {pct:.0f}%")

    # Pattern: high variance modes
    for mode, ratios in sorted(by_mode.items()):
        if len(ratios) < min_samples:
            continue
        if len(ratios) >= 2:
            sd = stdev(ratios)
            # Absolute threshold: flag any mode with std > 0.5 as high-variance
            if sd > 0.5:
                patterns.append(f"{mode.upper()} tasks have high variance (std={sd:.2f})")
                continue
            # Check if this is the highest variance among all modes with enough samples
            other_stds = [
                stdev(rs) for m, rs in by_mode.items() if m != mode and len(rs) >= min_samples and len(rs) >= 2
            ]
            if other_stds and sd > max(other_stds):
                patterns.append(f"{mode.upper()} tasks have highest variance (std={sd:.2f})")

    # Pattern: task class overestimation
    for cls, ratios in sorted(by_class.items()):
        if len(ratios) < min_samples:
            continue
        avg = mean(ratios)
        if avg < 0.9:
            pct = (1.0 - avg) * 100
            patterns.append(f"{cls.upper()} tasks are typically overestimated by {pct:.0f}%")
        elif avg > 1.1:
            patterns.append(f"{cls.upper()} tasks average {avg:.1f}x estimated duration")

    return patterns
