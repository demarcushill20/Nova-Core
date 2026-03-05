# Task 0011: Heartbeat First or Persistent Memory?

**Question:** Should we give Nova-Core a heartbeat first, or persistent memory?

**Answer: Heartbeat first.** Here's the full analysis.

---

## Current State Assessment

| Capability | Status | What Exists |
|---|---|---|
| **Heartbeat** | **Nothing** | Zero health-check, no timer, no proactive self-monitoring |
| **Persistent Memory** | **Partial** | `MEMORY/` dir exists, `STATE/` has runtime state, Claude auto-memory works in interactive sessions — but **nothing injects memory into watcher subprocess prompts** |

---

## Recommendation: Build the Heartbeat First

### Why Heartbeat Wins Priority

1. **Survivability before intelligence.** A heartbeat makes Nova-Core self-aware of its own health. Without it, a silent failure in watcher.py or the Telegram bridge goes unnoticed until the user checks manually. Memory is useless if the system is dead and nobody knows.

2. **Smallest useful increment.** A heartbeat is a single systemd timer + a lightweight Python script. It can ship in 30 minutes. Persistent memory requires modifying the dispatch prompt template, designing a retrieval strategy, and testing injection into subprocess calls — a multi-session effort.

3. **Unlocks monitoring for everything else.** Once we have a heartbeat, it becomes the anchor for all future proactive behaviors: memory compaction, task queue health, disk space, service uptime, API quota tracking. Memory is one consumer of the heartbeat, not the other way around.

4. **OpenClaw validated this order.** The OpenClaw project (our closest reference architecture) built their HEARTBEAT.md pattern first and runs it every 30 minutes. Their memory system came later. We documented this in `MEMORY/openclaw_research.md`.

5. **Failure mode asymmetry.** Without heartbeat: silent death, missed tasks, stale state — user discovers hours later. Without memory: tasks still execute correctly, just without cross-session context — a quality-of-life gap, not a reliability gap.

---

## Heartbeat: Implementation Sketch

### Architecture

```
novacore-heartbeat.timer (systemd, every 30min)
    |
    v
novacore-heartbeat.service
    ExecStart: /usr/bin/python3 /home/nova/nova-core/heartbeat.py
    |
    v
heartbeat.py:
    1. Check service health (watcher, telegram, notifier)
    2. Check disk space, task queue depth, orphaned .inprogress files
    3. Check last OUTPUT timestamp (stale worker detection)
    4. Write HEARTBEAT.md with timestamped checklist
    5. If anything is unhealthy → send Telegram alert
    6. Optionally: inject a synthetic "health-check" task for self-repair
```

### Heartbeat Checklist (HEARTBEAT.md)

```markdown
# NovaCore Heartbeat
Last check: 2026-03-05T16:30:00Z

- [x] novacore-watcher: active (pid 12345, uptime 2h)
- [x] novacore-telegram: active (pid 12346, uptime 2h)
- [x] novacore-telegram-notifier: active (pid 12347, uptime 2h)
- [x] Disk: 45% used (12GB free)
- [x] Task queue: 0 pending, 0 orphaned .inprogress
- [x] Last output: 3 min ago
- [x] Claude binary: /usr/bin/claude accessible
```

### Files to Create

| File | Purpose |
|---|---|
| `heartbeat.py` | Health-check script |
| `HEARTBEAT.md` | Last-known status (overwritten each run) |
| `/etc/systemd/system/novacore-heartbeat.timer` | 30-minute trigger |
| `/etc/systemd/system/novacore-heartbeat.service` | Service unit |

### Estimated Effort: ~1 task (30-60 min of Claude time)

---

## Persistent Memory: What Comes After

Once the heartbeat is stable, memory is the next priority. The plan:

1. **Memory injection into watcher.py** — when dispatching a task, read relevant `MEMORY/*.md` files and append them to the prompt via `--append-system-prompt`.
2. **Memory-write skill** — a skill that lets task executions append learnings to `MEMORY/` in a structured format.
3. **Memory retrieval strategy** — keyword/tag-based matching to avoid injecting irrelevant context (token budget matters for `claude -p` calls).
4. **Heartbeat-driven compaction** — the heartbeat can periodically check if MEMORY files are bloated and trigger a compaction task.

---

## Decision Matrix

| Factor | Heartbeat | Persistent Memory |
|---|---|---|
| Complexity | Low (1 script + timer) | Medium (prompt engineering + retrieval) |
| Impact | Reliability + visibility | Quality + continuity |
| Dependency | None | Benefits from heartbeat for compaction |
| Risk if delayed | Silent failures | Repeated work, no learning |
| Time to ship | ~1 session | ~2-3 sessions |
| **Priority** | **1st** | **2nd** |

---

## TL;DR

**Build the heartbeat first.** It's smaller, it's foundational, and it protects everything else we build — including the memory system that comes next. A system that can't tell you it's broken will never reliably remember anything.

---

## CONTRACT
summary: Analyzed heartbeat vs persistent memory priority; recommended heartbeat first with implementation sketch
task_id: 0011_Should_we_give_Nova-Core_a_heartbeat_first_or_persistent_memory_
status: done
verification: Reviewed all three services, MEMORY/ contents, STATE/ files, systemd config, openclaw research; confirmed zero heartbeat infrastructure exists and partial memory infrastructure exists
