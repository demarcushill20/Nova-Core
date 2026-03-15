"""Skill evaluation wrapper for the autonomous skill improvement pipeline.

Wraps the vendored run_eval.py to add:
- Precision / recall / F1 / FPR / FNR computation
- Weighted score calculation per thresholds.yaml
- Structured result output compatible with the decision engine
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent.parent
THRESHOLDS_PATH = BASE_DIR / "configs" / "thresholds.yaml"

# Import vendored eval runner
sys.path.insert(0, str(BASE_DIR / ".claude" / "skills" / "skill-creator"))
from scripts.run_eval import find_project_root, run_eval  # noqa: E402
from scripts.utils import parse_skill_md  # noqa: E402


@dataclass
class EvalMetrics:
    """Computed metrics from an evaluation run."""

    true_positives: int = 0
    true_negatives: int = 0
    false_positives: int = 0
    false_negatives: int = 0
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    fpr: float = 0.0  # false positive rate
    fnr: float = 0.0  # false negative rate
    specificity: float = 0.0  # 1 - FPR
    accuracy: float = 0.0
    weighted_score: float = 0.0
    paraphrase_consistency: float = 1.0  # placeholder until paraphrase eval exists
    neighbor_conflict_rate: float = 0.0  # filled by interference check

    @property
    def total(self) -> int:
        return self.true_positives + self.true_negatives + self.false_positives + self.false_negatives


@dataclass
class EvalResult:
    """Complete evaluation result for a skill."""

    skill_name: str
    description: str
    split: str  # "train", "test", "hard_negatives"
    metrics: EvalMetrics
    raw_results: list[dict] = field(default_factory=list)
    elapsed_seconds: float = 0.0
    timestamp: str = ""
    model: str | None = None

    def to_dict(self) -> dict:
        d = {
            "skill_name": self.skill_name,
            "description": self.description,
            "split": self.split,
            "metrics": asdict(self.metrics),
            "elapsed_seconds": self.elapsed_seconds,
            "timestamp": self.timestamp,
            "model": self.model,
            "result_count": len(self.raw_results),
        }
        return d


def load_thresholds() -> dict:
    """Load thresholds config."""
    if THRESHOLDS_PATH.exists():
        return yaml.safe_load(THRESHOLDS_PATH.read_text())
    return {}


def compute_metrics(
    results: list[dict],
    thresholds: dict | None = None,
    runs_per_query: int = 3,
    trigger_threshold: float = 0.5,
) -> EvalMetrics:
    """Compute precision/recall/F1/FPR from raw eval results.

    Each result dict has: query, should_trigger, trigger_rate, triggers, runs, pass.
    """
    tp = fp = tn = fn = 0

    for r in results:
        should = r["should_trigger"]
        rate = r.get("trigger_rate", r["triggers"] / r["runs"] if r["runs"] > 0 else 0)
        triggered = rate >= trigger_threshold

        if should and triggered:
            tp += 1
        elif should and not triggered:
            fn += 1
        elif not should and triggered:
            fp += 1
        else:
            tn += 1

    # Compute standard metrics
    precision = tp / (tp + fp) if (tp + fp) > 0 else 1.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 1.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    fpr = fp / (fp + tn) if (fp + tn) > 0 else 0.0
    fnr = fn / (fn + tp) if (fn + tp) > 0 else 0.0
    specificity = 1.0 - fpr
    total = tp + tn + fp + fn
    accuracy = (tp + tn) / total if total > 0 else 0.0

    # Weighted score
    if thresholds is None:
        thresholds = load_thresholds()
    weights = thresholds.get("scoring", {}).get("weights", {})

    w_f1 = weights.get("f1", 0.30)
    w_precision = weights.get("precision", 0.20)
    w_recall = weights.get("recall", 0.15)
    w_specificity = weights.get("specificity", 0.15)
    w_paraphrase = weights.get("paraphrase_consistency", 0.10)
    w_neighbor = weights.get("neighbor_conflict_rate", 0.10)

    # Paraphrase and neighbor are placeholders until those checks run
    paraphrase_consistency = 1.0
    neighbor_conflict_rate = 0.0

    weighted_score = (
        w_f1 * f1
        + w_precision * precision
        + w_recall * recall
        + w_specificity * specificity
        + w_paraphrase * paraphrase_consistency
        + w_neighbor * (1.0 - neighbor_conflict_rate)
    )

    return EvalMetrics(
        true_positives=tp,
        true_negatives=tn,
        false_positives=fp,
        false_negatives=fn,
        precision=round(precision, 4),
        recall=round(recall, 4),
        f1=round(f1, 4),
        fpr=round(fpr, 4),
        fnr=round(fnr, 4),
        specificity=round(specificity, 4),
        accuracy=round(accuracy, 4),
        weighted_score=round(weighted_score, 4),
        paraphrase_consistency=paraphrase_consistency,
        neighbor_conflict_rate=neighbor_conflict_rate,
    )


def load_eval_set(path: Path) -> list[dict]:
    """Load a JSONL eval set."""
    records: list[dict] = []
    if not path.exists():
        return records
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            records.append(json.loads(line))
    return records


def evaluate_skill(
    skill_name: str,
    skill_path: Path,
    description: str | None = None,
    split: str = "train",
    evals_dir: Path | None = None,
    num_workers: int = 10,
    timeout: int = 30,
    runs_per_query: int = 3,
    trigger_threshold: float = 0.5,
    model: str | None = None,
    thresholds: dict | None = None,
) -> EvalResult:
    """Evaluate a skill on a specific dataset split.

    Wraps vendored run_eval.py with added metric computation.
    """
    base_evals = evals_dir or (BASE_DIR / "evals")
    eval_file = base_evals / skill_name / f"{split}.jsonl"

    eval_set = load_eval_set(eval_file)
    if not eval_set:
        return EvalResult(
            skill_name=skill_name,
            description=description or "",
            split=split,
            metrics=EvalMetrics(),
            elapsed_seconds=0.0,
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            model=model,
        )

    # Parse skill to get name and description if not overridden
    name, orig_desc, _ = parse_skill_md(skill_path)
    desc = description or orig_desc

    project_root = find_project_root()

    t0 = time.time()
    raw_output = run_eval(
        eval_set=eval_set,
        skill_name=name,
        description=desc,
        num_workers=num_workers,
        timeout=timeout,
        project_root=project_root,
        runs_per_query=runs_per_query,
        trigger_threshold=trigger_threshold,
        model=model,
    )
    elapsed = time.time() - t0

    metrics = compute_metrics(
        raw_output["results"],
        thresholds=thresholds,
        runs_per_query=runs_per_query,
        trigger_threshold=trigger_threshold,
    )

    return EvalResult(
        skill_name=skill_name,
        description=desc,
        split=split,
        metrics=metrics,
        raw_results=raw_output["results"],
        elapsed_seconds=round(elapsed, 2),
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        model=model,
    )
