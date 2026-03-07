# Safe-Source Rules for Obsidian Memory

## Source hierarchy

| Source | Role | Example |
|--------|------|---------|
| `STATE/` | Runtime truth — current task state, PID files, flags | `STATE/running/0042.pid` |
| `TASKS/`, `LOGS/` | Operational records — task lifecycle, execution logs | `TASKS/0042.inprogress` |
| `MEMORY/` | Machine-artifact memory — auto-saved patterns and context | `MEMORY/MEMORY.md` |
| **Obsidian vault** | **Durable open memory — human + Nova-Core shared knowledge** | `Architecture/decisions.md` |

## Rules

1. **STATE/ is authoritative for runtime state.** Never use an Obsidian note to determine whether a task is running, a service is healthy, or a flag is set. Always check STATE/ directly.

2. **Obsidian notes are durable, not live.** They capture knowledge, decisions, research, and patterns. They may be outdated relative to current runtime state. Treat them as "best known context" not "current truth".

3. **Human-authored notes are first-class.** The vault is shared with the human operator. Notes may be written, edited, or reorganized by a human at any time. Never assume all vault content was machine-generated.

4. **Do not correct vault notes via read skill.** If a vault note appears outdated or incorrect, report the finding to the user. Do not silently discard or override it.

5. **Prefer narrow retrieval.** Read only what is needed. The vault may contain personal notes, drafts, or unrelated content. Respect boundaries by searching narrowly and reading selectively.

6. **Cite your sources.** When vault findings inform a response, cite the note path so the user can verify and update if needed.

7. **Degrade gracefully.** If the vault is unavailable (MCP server down, sync issues), report the failure clearly and continue without vault data. Never block on vault access.
