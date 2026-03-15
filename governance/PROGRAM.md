# Autonomous Skill Improvement Program

## Purpose

This program automates the optimization of NovaCore skill descriptions using
a Karpathy-style autoresearch loop: mutate → evaluate → measure → accept/reject → loop.

## Scope

- **In scope:** Prompt-trigger skill descriptions (42 skills in `.claude/skills/`)
- **Out of scope:** Execution skills (`SKILLS/`), tool permissions, output contracts,
  safety rules, skill body content

## Operating Principles

1. **Code runs the optimizer, Claude designs it.** The pipeline is deterministic
   Python code. Claude is invoked only for mutation generation (via `claude -p`).

2. **Conservative by default.** The system rejects more than it accepts. A rejected
   candidate costs nothing; a bad promotion can cascade.

3. **Ecosystem over individual.** A local improvement that harms neighbors is rejected.

4. **Auditability over cleverness.** Every decision is logged with machine-readable
   reason codes and human-readable explanations.

5. **Reversibility always.** Every accepted change can be rolled back by run ID.

## Frozen Skills

These skills are excluded from auto-optimization:

| Skill | Reason |
|-------|--------|
| skill-creator | Meta-skill — recursive optimization is unsafe |
| task-execution | Core pipeline — mutation risks task integrity |
| self-verification | Safety-critical — validates all outputs |
| auditing-obsidian-memory-safety | Governance — enforces memory write safety |
| semgrep-security | Security — scans for vulnerabilities |
| All execution skills (4) | Frozen in rollout 1 |

## Rollout Strategy

1. **Phase A:** Single-skill dry-run (manual trigger)
2. **Phase B:** Small cluster dry-run (3-5 related skills)
3. **Phase C:** All-eligible dry-run
4. **Phase D:** Propose mode (writes artifacts, no git promotion)
5. **Phase E:** Promote mode (creates feature branches for review)
6. **Phase F:** Scheduled runs (systemd timer, dry-run default)

Each phase requires manual verification before advancing to the next.

## Authority

- The pipeline never merges to main. It creates feature branches only.
- An operator must review and merge any proposed changes.
- The pipeline can be halted at any time by changing `mode: "dry-run"` in
  `configs/pipeline.yaml`.
