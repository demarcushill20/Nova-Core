# Task 0012 — Heartbeat Developer Plan

**Completed:** 2026-03-05T16:39:56Z
**Task:** Create a step-by-step detailed developer plan for implementing the NovaCore heartbeat system

## Summary

Produced a comprehensive, implementation-ready developer plan for the NovaCore heartbeat system. The plan covers:

1. **Architecture:** Standalone `heartbeat.py` script triggered by a 30-minute systemd timer. Zero changes to existing watcher/telegram/notifier code.
2. **7 Health Checks:** Service health (3 services), disk usage, Claude binary, task queue + orphan detection, stale worker PIDs, contract failure metrics.
3. **HEARTBEAT.md:** Overwritten each run with a markdown checklist — human-readable, machine-parseable.
4. **Alerting:** Telegram notification on failure (silent on success). Uses stdlib `urllib.request` — no new dependencies.
5. **Self-repair:** Synthetic task injection into `TASKS/` for service failures. Watcher picks it up automatically.
6. **Deployment:** Two systemd units (`.timer` + `.service`), `enable` + `start`, manual verification checklist.
7. **Risk assessment** and **future enhancements** section.

The plan includes full code for every function in `heartbeat.py`, systemd unit file contents, deploy commands, and a verification matrix.

## Files Created

| File | Path | Description |
|------|------|-------------|
| Developer Plan | `WORK/heartbeat_developer_plan.md` | Full implementation plan with code, systemd units, verification steps |

## Files Modified

None.

## Key Design Decisions

- **Standalone process** — heartbeat.py is independent of watcher.py. No coupling, no shared state, can be deployed/rolled back independently.
- **System Python** — uses `/usr/bin/python3` (no venv), matching `watcher.py` pattern.
- **stdlib only** — no pip installs required. `urllib.request` for HTTP, `os.statvfs` for disk, `subprocess` for systemctl.
- **Silent when healthy** — only alerts on failure (OpenClaw pattern). Reduces notification fatigue.
- **Rate-limited repair** — checks for existing `hb_*_self_repair.inprogress` before injecting new repair tasks.

## Next Steps

1. Execute the plan: create `heartbeat.py` (Session 1)
2. Manual test, verify HEARTBEAT.md and Telegram alerts
3. Create and enable systemd units (Session 2)
4. Soak test over 2-3 timer cycles

## CONTRACT
summary: Created detailed developer plan for NovaCore heartbeat system
task_id: 0012_Can_you_create_a_step_by_step_detailed_developer_plan_on_how_would_you_do_this__
status: done
verification: Plan file exists at WORK/heartbeat_developer_plan.md with complete implementation code, systemd units, and verification checklist
