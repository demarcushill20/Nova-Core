"""Main supervisor for the autonomous skill improvement pipeline.

Orchestrates the full optimization loop:
1. Load config → 2. Discover skills → 3. Validate → 4. Baseline →
5. Generate candidates → 6. Rank on train → 7. Test best →
8. Interference check → 9. Smoke check → 10. Decision →
11. Write artifacts → 12. Monitor halt → 13. Report

Supports: dry-run, propose, promote modes.
Supports: single-skill, cluster, and batch operation.
Supports: resume after interruption via checkpoints.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import yaml

from tools.skill_optimizer.artifact_writer import (
    append_history,
    ensure_run_dir,
    generate_run_id,
    update_manifest,
    write_baseline,
    write_candidate,
    write_decision,
    write_interference,
    write_log,
    write_manifest,
    write_summary,
)
from tools.skill_optimizer.baseline_runner import run_baseline
from tools.skill_optimizer.candidate_generator import filter_valid_candidates, generate_candidates
from tools.skill_optimizer.candidate_ranker import rank_candidates, select_best
from tools.skill_optimizer.decision_engine import (
    ACCEPT,
    BatchHaltMonitor,
    Decision,
    evaluate_gates,
    load_thresholds,
)
from tools.skill_optimizer.detect_neighbors import detect_neighbors
from tools.skill_optimizer.evaluate_skill import evaluate_skill
from tools.skill_optimizer.global_smoke import run_smoke_test
from tools.skill_optimizer.interference_check import check_interference, load_neighbor_eval_set
from tools.skill_optimizer.skill_discovery import SkillRecord, discover_all, get_eligible_skills
from tools.skill_optimizer.skill_validator import check_readiness

BASE_DIR = Path(__file__).resolve().parent.parent.parent


def load_pipeline_config() -> dict:
    """Load pipeline configuration."""
    path = BASE_DIR / "configs" / "pipeline.yaml"
    if path.exists():
        return yaml.safe_load(path.read_text())
    return {}


def load_mutation_policy() -> dict:
    """Load mutation policy configuration."""
    path = BASE_DIR / "configs" / "mutation_policy.yaml"
    if path.exists():
        return yaml.safe_load(path.read_text())
    return {}


def _save_checkpoint(run_dir: Path, skill_index: int, state: dict) -> None:
    """Save a checkpoint for resume support."""
    checkpoint_path = run_dir / "checkpoint.json"
    state["skill_index"] = skill_index
    state["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%SZ")
    checkpoint_path.write_text(json.dumps(state, indent=2, default=str) + "\n")


def _load_checkpoint(run_dir: Path) -> dict | None:
    """Load a checkpoint if it exists."""
    checkpoint_path = run_dir / "checkpoint.json"
    if checkpoint_path.exists():
        return json.loads(checkpoint_path.read_text())
    return None


def optimize_skill(
    skill: SkillRecord,
    run_dir: Path,
    thresholds: dict,
    policy: dict,
    config: dict,
    model: str | None = None,
    verbose: bool = False,
) -> dict:
    """Run the full optimization loop for a single skill.

    Returns a result dict with decision, metrics, and artifacts.
    """
    log_entries: list[dict] = []
    skill_path = Path(skill.path)
    result: dict[str, Any] = {"skill": skill.name, "status": "pending"}

    def log(msg: str, **kwargs: Any) -> None:
        entry = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ"), "skill": skill.name, "message": msg, **kwargs}
        log_entries.append(entry)
        if verbose:
            print(f"  [{skill.name}] {msg}", file=sys.stderr)

    try:
        # Step 1: Validate readiness
        readiness = check_readiness(skill)
        if not readiness.ready:
            log(f"Skip: {readiness.reason}")
            result["status"] = "skipped"
            result["reason"] = readiness.reason
            return result

        log("Starting optimization")

        # Step 2: Baseline measurement
        log("Running baseline evaluation")
        baseline_result = run_baseline(skill, thresholds=thresholds, model=model, verbose=False)
        baseline_metrics = asdict(baseline_result.metrics)
        baseline_score = baseline_result.metrics.weighted_score

        write_baseline(run_dir, skill.name, baseline_result.to_dict())
        log(
            f"Baseline: P={baseline_result.metrics.precision:.3f} R={baseline_result.metrics.recall:.3f} "
            f"F1={baseline_result.metrics.f1:.3f} score={baseline_score:.4f}"
        )

        result["baseline"] = baseline_metrics

        # Step 3: Check if baseline already perfect
        if baseline_result.metrics.f1 >= 0.99 and baseline_result.metrics.fpr <= 0.01:
            log("Baseline already near-perfect, skip optimization")
            result["status"] = "skipped"
            result["reason"] = "baseline_perfect"
            return result

        # Step 4: Generate candidates
        log("Generating candidates")
        candidate_count = config.get("candidates", {}).get("count", 3)
        mutation_model = policy.get("model", {}).get("default", "claude-sonnet-4-6")

        # Build eval results dict for the mutation prompt
        eval_results = {
            "results": baseline_result.raw_results,
            "summary": {
                "passed": sum(1 for r in baseline_result.raw_results if r["pass"]),
                "failed": sum(1 for r in baseline_result.raw_results if not r["pass"]),
                "total": len(baseline_result.raw_results),
            },
        }

        candidates = generate_candidates(
            skill_name=skill.name,
            skill_path=skill_path,
            current_description=baseline_result.description,
            eval_results=eval_results,
            count=candidate_count,
            model=mutation_model,
            policy=policy,
            log_dir=run_dir / "logs",
        )

        valid_candidates = filter_valid_candidates(candidates)
        log(f"Generated {len(candidates)} candidates, {len(valid_candidates)} valid")

        if not valid_candidates:
            log("No valid candidates generated")
            result["status"] = "no_candidates"
            result["reason"] = "all candidates failed policy check"
            return result

        # Step 5: Rank candidates on train set
        log("Ranking candidates on train set")
        ranked = rank_candidates(
            valid_candidates,
            skill_path,
            baseline_score=baseline_score,
            model=model,
        )

        for rc in ranked:
            write_candidate(
                run_dir,
                skill.name,
                rc.candidate.candidate_id,
                {
                    "candidate_id": rc.candidate.candidate_id,
                    "description": rc.candidate.proposed_description[:200],
                    "train_score": rc.train_score,
                    "rank": rc.rank,
                },
            )
            log(f"Candidate {rc.candidate.candidate_id}: score={rc.train_score:.4f} (rank={rc.rank})")

        # Step 6: Select best candidate
        min_improvement = thresholds.get("hard_gates", {}).get("min_score_improvement", 0.02)
        best = select_best(ranked, baseline_score, min_improvement)

        if not best:
            log("No candidate meets minimum improvement threshold")
            result["status"] = "no_improvement"
            result["reason"] = f"best score {ranked[0].train_score:.4f} vs baseline {baseline_score:.4f}"
            return result

        log(f"Best candidate: {best.candidate.candidate_id} (score={best.train_score:.4f})")
        candidate_desc = best.candidate.proposed_description

        # Step 7: Holdout test evaluation
        log("Running holdout test evaluation")
        test_result = evaluate_skill(
            skill_name=skill.name,
            skill_path=skill_path,
            description=candidate_desc,
            split="test",
            model=model,
            thresholds=thresholds,
        )
        test_metrics = asdict(test_result.metrics)
        log(
            f"Test: P={test_result.metrics.precision:.3f} R={test_result.metrics.recall:.3f} "
            f"F1={test_result.metrics.f1:.3f} score={test_result.metrics.weighted_score:.4f}"
        )

        # Step 8: Hard negative check (anti-broadening)
        hard_neg_result = evaluate_skill(
            skill_name=skill.name,
            skill_path=skill_path,
            description=candidate_desc,
            split="hard_negatives",
            model=model,
            thresholds=thresholds,
        )
        # For hard negatives, regression means MORE false triggers
        baseline_hard_neg = evaluate_skill(
            skill_name=skill.name,
            skill_path=skill_path,
            split="hard_negatives",
            model=model,
            thresholds=thresholds,
        )
        hard_neg_regression = hard_neg_result.metrics.false_positives > baseline_hard_neg.metrics.false_positives

        # Step 9: Interference check
        log("Running interference check")
        neighbor_data = detect_neighbors(skill.name)
        all_neighbors = neighbor_data.get("merged", [])
        manual_neighbors = set(neighbor_data.get("manual", []))

        # Priority-order neighbors: manually curated with eval data first,
        # then other neighbors with eval data, then the rest.
        # This ensures high-value boundary neighbors (e.g. http-fetch) are
        # never silently excluded by an alphabetical cutoff.
        max_neighbors = 5
        with_eval: list[str] = []
        without_eval: list[str] = []
        for n in all_neighbors:
            positives = load_neighbor_eval_set(n)
            if positives:
                with_eval.append(n)
            else:
                without_eval.append(n)
        # Within each group, sort manual-curated first, then alphabetical
        with_eval.sort(key=lambda n: (0 if n in manual_neighbors else 1, n))
        without_eval.sort(key=lambda n: (0 if n in manual_neighbors else 1, n))
        neighbor_names = (with_eval + without_eval)[:max_neighbors]

        interference_report = check_interference(
            skill_name=skill.name,
            candidate_description=candidate_desc,
            neighbor_names=neighbor_names,
            model=model,
        )
        log(
            f"Interference: {interference_report.total_false_triggers}/{interference_report.total_queries} "
            f"conflicts (rate={interference_report.aggregate_conflict_rate:.3f})"
        )

        # Persist per-query interference details for diagnosis (P2)
        write_interference(run_dir, skill.name, interference_report.to_dict())

        # Step 10: Smoke test
        log("Running smoke test")
        smoke_report = run_smoke_test(
            modified_skill=skill.name,
            modified_description=candidate_desc,
            model=model,
        )
        log(f"Smoke: {smoke_report.passed}/{smoke_report.total_tests} passed")

        # Step 11: Decision
        log("Evaluating decision gates")
        decision = evaluate_gates(
            skill_name=skill.name,
            risk_level=skill.risk_level,
            baseline_metrics=baseline_metrics,
            candidate_metrics=test_metrics,
            candidate_description=candidate_desc,
            policy_violations=best.candidate.policy_violations,
            neighbor_conflict_rate=interference_report.aggregate_conflict_rate,
            smoke_regressions=smoke_report.failed,
            hard_negative_regression=hard_neg_regression,
            thresholds=thresholds,
        )

        write_decision(run_dir, skill.name, decision.to_dict())
        log(f"Decision: {decision.decision} — {decision.reason}")

        # Record results
        result["status"] = "completed"
        result["decision"] = decision.decision
        result["reason"] = decision.reason
        result["baseline_score"] = baseline_score
        result["candidate_score"] = test_result.metrics.weighted_score
        result["delta"] = decision.delta
        result["candidate_description"] = candidate_desc[:200]
        result["interference"] = interference_report.to_dict()
        result["smoke"] = smoke_report.to_dict()

        # Append to history
        append_history(
            {
                "type": "optimization",
                "skill": skill.name,
                "run_id": run_dir.name,
                "decision": decision.decision,
                "baseline_score": baseline_score,
                "candidate_score": test_result.metrics.weighted_score,
                "delta": decision.delta,
            }
        )

    except Exception as e:
        log(f"Error: {e}", error=str(e))
        result["status"] = "error"
        result["reason"] = str(e)

    finally:
        write_log(run_dir, skill.name, log_entries)

    return result


def run_optimization(
    skills: list[SkillRecord] | None = None,
    run_id: str | None = None,
    mode: str = "dry-run",
    max_skills: int | None = None,
    model: str | None = None,
    verbose: bool = False,
    resume_from: str | None = None,
) -> dict:
    """Run the full optimization pipeline.

    Args:
        skills: Skills to optimize (default: all eligible with valid datasets)
        run_id: Override run ID
        mode: Operating mode (dry-run, propose, promote)
        max_skills: Maximum skills to process
        model: Model for evaluation
        verbose: Print progress
        resume_from: Run ID to resume from
    """
    config = load_pipeline_config()
    thresholds = load_thresholds()
    policy = load_mutation_policy()

    # Override mode from config if not specified
    if mode == "dry-run":
        mode = config.get("mode", "dry-run")

    # Discover skills
    if skills is None:
        all_records = discover_all()
        eligible = get_eligible_skills(all_records)

        # Filter to skills with valid datasets
        if config.get("scope", {}).get("require_valid_datasets", True):
            ready_skills = []
            for skill in eligible:
                readiness = check_readiness(skill)
                if readiness.ready:
                    ready_skills.append(skill)
                elif verbose:
                    print(f"  Skip {skill.name}: {readiness.reason}", file=sys.stderr)
            skills = ready_skills
        else:
            skills = eligible

    # Apply scope filters
    include = config.get("scope", {}).get("include_skills", [])
    exclude = config.get("scope", {}).get("exclude_skills", [])
    if include:
        skills = [s for s in skills if s.name in include]
    if exclude:
        skills = [s for s in skills if s.name not in exclude]

    # Apply max_skills limit
    config_max = config.get("scope", {}).get("max_skills_per_batch", None)
    effective_max = max_skills or config_max
    if effective_max and len(skills) > effective_max:
        skills = skills[:effective_max]

    if not skills:
        return {"run_id": None, "error": "no eligible skills found", "results": []}

    # Set up run
    rid = resume_from or run_id or generate_run_id()
    run_dir = ensure_run_dir(rid)

    # Check for resume
    start_index = 0
    if resume_from:
        checkpoint = _load_checkpoint(run_dir)
        if checkpoint:
            start_index = checkpoint.get("skill_index", 0) + 1
            if verbose:
                print(f"Resuming from skill index {start_index}", file=sys.stderr)

    write_manifest(
        run_dir,
        {
            "type": "optimization",
            "mode": mode,
            "total_skills": len(skills),
            "model": model,
            "started": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "config": {
                "mode": mode,
                "max_skills": effective_max,
            },
        },
    )

    if verbose:
        print(f"\nOptimization run: {rid}", file=sys.stderr)
        print(f"Mode: {mode}", file=sys.stderr)
        print(f"Skills: {len(skills)}", file=sys.stderr)
        print(f"{'=' * 60}", file=sys.stderr)

    # Initialize batch halt monitor
    halt_monitor = BatchHaltMonitor(thresholds)
    results: list[dict] = []
    accepted = 0
    rejected = 0
    skipped = 0
    errors = 0

    for i, skill in enumerate(skills[start_index:], start_index):
        if halt_monitor.is_halted:
            if verbose:
                print(f"\nBATCH HALT: {halt_monitor.halt_reason}", file=sys.stderr)
            break

        if verbose:
            print(f"\n[{i + 1}/{len(skills)}] {skill.name}", file=sys.stderr)

        skill_result = optimize_skill(
            skill=skill,
            run_dir=run_dir,
            thresholds=thresholds,
            policy=policy,
            config=config,
            model=model,
            verbose=verbose,
        )

        results.append(skill_result)

        # Track decision counts
        status = skill_result.get("status", "error")
        decision_val = skill_result.get("decision", "")
        if status == "completed" and decision_val == ACCEPT:
            accepted += 1
        elif status == "completed":
            rejected += 1
        elif status in ("skipped", "no_candidates", "no_improvement", "baseline_perfect"):
            skipped += 1
        else:
            errors += 1

        # Feed to halt monitor
        if status == "completed":
            d = Decision(
                skill.name,
                decision_val,
                skill_result.get("reason", ""),
                decision_val,
            )
            halt_monitor.record_decision(d)

        # Save checkpoint
        _save_checkpoint(
            run_dir,
            i,
            {
                "run_id": rid,
                "accepted": accepted,
                "rejected": rejected,
                "skipped": skipped,
                "errors": errors,
            },
        )

    # Write final summary
    summary = {
        "run_id": rid,
        "mode": mode,
        "total_skills": len(skills),
        "processed": len(results),
        "accepted": accepted,
        "rejected": rejected,
        "skipped": skipped,
        "errors": errors,
        "halted": halt_monitor.is_halted,
        "halt_reason": halt_monitor.halt_reason,
        "completed": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "skills": [
            {
                "skill": r.get("skill"),
                "decision": r.get("decision", r.get("status")),
                "reason": r.get("reason", ""),
                "baseline_score": r.get("baseline_score", ""),
                "candidate_score": r.get("candidate_score", ""),
                "delta": r.get("delta", ""),
            }
            for r in results
        ],
    }
    write_summary(run_dir, summary)

    update_manifest(
        run_dir,
        {
            "status": "completed",
            "completed": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "accepted": accepted,
            "rejected": rejected,
            "skipped": skipped,
            "errors": errors,
        },
    )

    if verbose:
        print(f"\n{'=' * 60}", file=sys.stderr)
        print(f"Run complete: {rid}", file=sys.stderr)
        print(f"Accepted: {accepted}, Rejected: {rejected}, Skipped: {skipped}, Errors: {errors}", file=sys.stderr)

    return {
        "run_id": rid,
        "run_dir": str(run_dir),
        "mode": mode,
        "total_skills": len(skills),
        "accepted": accepted,
        "rejected": rejected,
        "skipped": skipped,
        "errors": errors,
        "halted": halt_monitor.is_halted,
        "results": results,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Run skill optimization pipeline")
    parser.add_argument("--mode", default="dry-run", choices=["dry-run", "propose", "promote"], help="Operating mode")
    parser.add_argument("--skill", default=None, help="Optimize a single skill")
    parser.add_argument("--max-skills", type=int, default=None, help="Max skills to process")
    parser.add_argument("--model", default=None, help="Model for evaluation")
    parser.add_argument("--resume", default=None, help="Resume from a previous run ID")
    parser.add_argument("--verbose", action="store_true", help="Print progress")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    # Single skill mode
    skills = None
    if args.skill:
        all_records = discover_all()
        skill = next((r for r in all_records if r.name == args.skill), None)
        if not skill:
            print(f"Skill not found: {args.skill}", file=sys.stderr)
            sys.exit(1)
        skills = [skill]

    output = run_optimization(
        skills=skills,
        mode=args.mode,
        max_skills=args.max_skills,
        model=args.model,
        verbose=args.verbose,
        resume_from=args.resume,
    )

    if args.json:
        # Strip raw_results for compact output
        for r in output.get("results", []):
            r.pop("interference", None)
            r.pop("smoke", None)
        print(json.dumps(output, indent=2, default=str))
    else:
        print(f"Run: {output.get('run_id')}")
        print(f"Mode: {output.get('mode')}")
        print(f"Skills: {output.get('total_skills')}")
        print(f"Accepted: {output.get('accepted')}")
        print(f"Rejected: {output.get('rejected')}")
        print(f"Skipped: {output.get('skipped')}")
        print(f"Errors: {output.get('errors')}")
        if output.get("halted"):
            print(f"HALTED: {output.get('halt_reason', 'unknown')}")


if __name__ == "__main__":
    main()
