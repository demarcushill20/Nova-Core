# Skill Optimizer — Operator Runbook

## Quick Start

### 1. Single-skill dry-run (Phase A)

```bash
python3 -m tools.skill_optimizer.optimize_all_skills \
  --skill web-research \
  --mode dry-run \
  --verbose
```

### 2. Check skill readiness

```bash
python3 -m tools.skill_optimizer.skill_discovery
python3 -m tools.skill_optimizer.validate_datasets --validate
```

### 3. Run baselines

```bash
python3 -m tools.skill_optimizer.baseline_runner --verbose
```

### 4. Small cluster dry-run (Phase B)

```bash
python3 -m tools.skill_optimizer.optimize_all_skills \
  --max-skills 3 \
  --mode dry-run \
  --verbose
```

### 5. Full dry-run (Phase C)

```bash
python3 -m tools.skill_optimizer.optimize_all_skills \
  --mode dry-run \
  --verbose
```

### 6. Propose mode (Phase D)

```bash
python3 -m tools.skill_optimizer.optimize_all_skills \
  --mode propose \
  --verbose
```

### 7. Promote mode (Phase E)

```bash
python3 -m tools.skill_optimizer.optimize_all_skills \
  --mode promote \
  --verbose
```

### 8. Rollback

```bash
python3 -m tools.skill_optimizer.rollback --run-id <run_id>
python3 -m tools.skill_optimizer.rollback --run-id <run_id> --dry-run
```

## Architecture

```
Layer A (Source of Truth)
  .claude/skills/       — 42 prompt skills
  SKILLS/               — 4 execution skills

Layer B (Optimization Primitives)
  .claude/skills/skill-creator/scripts/
    run_eval.py         — trigger testing via claude -p
    run_loop.py         — eval + improve loop
    improve_description.py — LLM-based mutation

Layer C (Supervisor / Orchestration)
  tools/skill_optimizer/
    skill_discovery.py     — canonical discovery + classification
    skill_validator.py     — pre-flight readiness checks
    validate_datasets.py   — dataset quality gate
    evaluate_skill.py      — wraps run_eval + F1/precision scoring
    baseline_runner.py     — batch baseline measurement
    candidate_generator.py — policy-constrained mutation
    candidate_ranker.py    — train/dev ranking
    decision_engine.py     — hard gates + accept/reject
    detect_neighbors.py    — multi-source neighbor map
    interference_check.py  — cross-skill conflict testing
    global_smoke.py        — ecosystem regression check
    optimize_all_skills.py — main supervisor
    git_promoter.py        — feature branch promotion
    rollback.py            — run-id-based revert
    artifact_writer.py     — structured persistence
    reporting.py           — markdown reports
    visualize_trends.py    — text-based trend analysis

Layer D (Governance / Config)
  configs/
    skill_registry.yaml    — skill inventory
    thresholds.yaml        — scoring + hard gates
    mutation_policy.yaml   — allowed mutations
    pipeline.yaml          — operating mode
  governance/
    PROGRAM.md             — program charter
    ACCEPTANCE_RULES.md    — accept/reject rules
    CONFLICT_POLICY.md     — neighbor conflict handling
    ROLLBACK_POLICY.md     — rollback procedures
    DATASET_POLICY.md      — holdout discipline
```

## Safety Controls

1. **Frozen skills:** 5 critical + 4 execution = 9 frozen
2. **Hard gates:** 8 ordered reject conditions
3. **Batch halt:** Consecutive failure + acceptance rate monitors
4. **Anti-broadening:** Hard negative regression check
5. **Zero-tolerance smoke:** Global smoke test
6. **Feature-branch only:** Never touches main
7. **Dry-run default:** Pipeline starts in dry-run mode

## Scheduling (Phase F)

Only after manual proof of Phases A-E. Example systemd timer:

```ini
# /etc/systemd/system/skill-optimizer.timer
[Unit]
Description=Skill optimizer dry-run

[Timer]
OnCalendar=weekly
Persistent=true

[Install]
WantedBy=timers.target
```

```ini
# /etc/systemd/system/skill-optimizer.service
[Unit]
Description=Skill optimizer run

[Service]
Type=oneshot
User=nova
WorkingDirectory=/home/nova/nova-core
ExecStart=/usr/bin/python3 -m tools.skill_optimizer.optimize_all_skills --mode dry-run --verbose
StandardOutput=append:/home/nova/nova-core/LOGS/skill_optimizer_cron.log
StandardError=append:/home/nova/nova-core/LOGS/skill_optimizer_cron.log
```

**Important:** Scheduling must trigger dry-run or propose first. Never unattended promote.
