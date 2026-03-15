"""Baseline measurement runner for the skill optimization pipeline.

Runs evaluation on the current (unmodified) description for each skill
and persists the results as baselines. Supports caching to avoid
redundant re-evaluation.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import yaml

from tools.skill_optimizer.artifact_writer import (
    append_history,
    ensure_run_dir,
    generate_run_id,
    get_cached_baseline,
    write_baseline,
    write_manifest,
    write_summary,
)
from tools.skill_optimizer.evaluate_skill import EvalResult, evaluate_skill, load_thresholds
from tools.skill_optimizer.skill_discovery import SkillRecord, discover_all, get_eligible_skills
from tools.skill_optimizer.validate_datasets import validate_all

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def load_pipeline_config() -> dict:
    """Load pipeline configuration."""
    path = BASE_DIR / "configs" / "pipeline.yaml"
    if path.exists():
        return yaml.safe_load(path.read_text())
    return {}


def run_baseline(
    skill: SkillRecord,
    thresholds: dict | None = None,
    model: str | None = None,
    verbose: bool = False,
) -> EvalResult:
    """Run baseline evaluation for a single skill on train split."""
    if verbose:
        print(f"  Baseline: {skill.name}", file=sys.stderr)

    return evaluate_skill(
        skill_name=skill.name,
        skill_path=Path(skill.path),
        split="train",
        num_workers=10,
        timeout=30,
        runs_per_query=3,
        trigger_threshold=0.5,
        model=model,
        thresholds=thresholds,
    )


def run_all_baselines(
    skills: list[SkillRecord] | None = None,
    run_id: str | None = None,
    cache_ttl_hours: float = 24.0,
    force: bool = False,
    model: str | None = None,
    verbose: bool = False,
    validate_first: bool = True,
) -> dict:
    """Run baselines for all eligible skills with valid datasets.

    Returns a dict with run_id, results per skill, and summary stats.
    """
    if skills is None:
        skills = get_eligible_skills()

    thresholds = load_thresholds()

    # Validate datasets first (if enabled)
    if validate_first:
        skill_dicts = [{"name": s.name, "path": s.path, "neighbors": s.neighbors} for s in skills]
        validate_all(skill_dicts)  # raises on critical issues

    # Filter to skills with enough data
    skills_with_data = []
    for skill in skills:
        eval_dir = BASE_DIR / "evals" / skill.name
        train_file = eval_dir / "train.jsonl"
        if train_file.exists() and train_file.stat().st_size > 0:
            skills_with_data.append(skill)
        elif verbose:
            print(f"  Skip {skill.name}: no train data", file=sys.stderr)

    if not skills_with_data:
        return {"run_id": None, "error": "no skills with valid datasets", "results": {}}

    # Set up run directory
    rid = run_id or generate_run_id()
    run_dir = ensure_run_dir(rid)

    write_manifest(
        run_dir,
        {
            "type": "baseline",
            "skills_total": len(skills_with_data),
            "model": model,
            "started": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
    )

    results: dict[str, dict] = {}
    cached = 0
    evaluated = 0
    errors = 0

    for skill in skills_with_data:
        # Check cache
        if not force:
            cached_baseline = get_cached_baseline(skill.name, cache_ttl_hours)
            if cached_baseline:
                if verbose:
                    print(f"  Cache hit: {skill.name}", file=sys.stderr)
                results[skill.name] = cached_baseline
                cached += 1
                continue

        # Run baseline
        try:
            result = run_baseline(skill, thresholds=thresholds, model=model, verbose=verbose)
            result_dict = result.to_dict()
            result_dict["raw_results"] = result.raw_results

            # Persist
            write_baseline(run_dir, skill.name, result_dict)

            # Append to history
            append_history(
                {
                    "type": "baseline",
                    "skill": skill.name,
                    "run_id": rid,
                    "metrics": result_dict["metrics"],
                    "description": result.description[:200],
                    "split": "train",
                }
            )

            results[skill.name] = result_dict
            evaluated += 1

            if verbose:
                m = result.metrics
                print(
                    f"    P={m.precision:.2f} R={m.recall:.2f} F1={m.f1:.2f} "
                    f"FPR={m.fpr:.2f} score={m.weighted_score:.3f}",
                    file=sys.stderr,
                )

        except Exception as e:
            errors += 1
            results[skill.name] = {"error": str(e)}
            if verbose:
                print(f"  ERROR: {skill.name}: {e}", file=sys.stderr)

    # Write summary
    summary = {
        "run_id": rid,
        "type": "baseline",
        "total_skills": len(skills_with_data),
        "evaluated": evaluated,
        "cached": cached,
        "errors": errors,
        "completed": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "skills": [
            {
                "skill": name,
                "decision": "baseline",
                "reason": "initial measurement",
                "baseline_score": r.get("metrics", {}).get("weighted_score", "N/A")
                if isinstance(r, dict) and "error" not in r
                else "error",
                "candidate_score": "",
                "delta": "",
            }
            for name, r in results.items()
        ],
    }
    write_summary(run_dir, summary)

    # Update manifest as complete
    from tools.skill_optimizer.artifact_writer import update_manifest

    update_manifest(
        run_dir,
        {
            "status": "completed",
            "completed": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "evaluated": evaluated,
            "cached": cached,
            "errors": errors,
        },
    )

    return {
        "run_id": rid,
        "run_dir": str(run_dir),
        "total_skills": len(skills_with_data),
        "evaluated": evaluated,
        "cached": cached,
        "errors": errors,
        "results": results,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Run baseline evaluations")
    parser.add_argument("--skill", default=None, help="Run baseline for a single skill")
    parser.add_argument("--force", action="store_true", help="Force re-evaluation (ignore cache)")
    parser.add_argument("--model", default=None, help="Model for evaluation")
    parser.add_argument("--verbose", action="store_true", help="Print progress")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    if args.skill:
        # Single skill mode
        all_records = discover_all()
        skill = next((r for r in all_records if r.name == args.skill), None)
        if not skill:
            print(f"Skill not found: {args.skill}", file=sys.stderr)
            sys.exit(1)
        skills = [skill]
    else:
        skills = None  # will discover eligible

    output = run_all_baselines(
        skills=skills,
        force=args.force,
        model=args.model,
        verbose=args.verbose,
    )

    if args.json:
        # Remove raw_results for compact output
        for _name, r in output.get("results", {}).items():
            if isinstance(r, dict):
                r.pop("raw_results", None)
        print(json.dumps(output, indent=2, default=str))
    else:
        print(f"Run ID: {output.get('run_id')}")
        evald = output.get("evaluated", 0)
        cached_n = output.get("cached", 0)
        errs = output.get("errors", 0)
        print(f"Evaluated: {evald}, Cached: {cached_n}, Errors: {errs}")


if __name__ == "__main__":
    main()
