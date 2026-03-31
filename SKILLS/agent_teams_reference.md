# Agent Teams — Master Reference Guide

> Internal reference for building effective Claude Code agent teams.
> Source: official docs, community guides, and NovaCore operational experience.

---

## 1. Architecture

```
┌──────────────────────────────────────────────────┐
│  TEAM LEAD (your Claude Code session)            │
│  - Creates team, spawns teammates                │
│  - Assigns tasks, reviews work                   │
│  - Synthesizes results                           │
│  - Only the lead can manage the team             │
├──────────────────────────────────────────────────┤
│  SHARED INFRASTRUCTURE                           │
│  - Task list: ~/.claude/tasks/{team-name}/       │
│  - Team config: ~/.claude/teams/{team-name}/     │
│  - Mailbox: async message delivery               │
│  - Dependencies: auto-unblock on completion      │
├──────────┬──────────┬──────────┬─────────────────┤
│ Teammate │ Teammate │ Teammate │ ...             │
│ Own ctx  │ Own ctx  │ Own ctx  │                 │
│ Own tools│ Own tools│ Own tools│                 │
└──────────┴──────────┴──────────┴─────────────────┘
```

**Key properties:**
- Each teammate has its own context window — they do NOT share the lead's conversation history
- Teammates load project context automatically (CLAUDE.md, MCP servers, skills)
- Messages are delivered async — no polling required
- Task dependencies auto-unblock when predecessors complete
- Lead is fixed for the session lifetime — no leadership transfer
- No nested teams — teammates cannot spawn their own teams

---

## 2. Setup

### Enable (required — experimental feature, Claude Code ≥ v2.1.32)

Already enabled in `/home/nova/.claude/settings.json`:

```json
{
  "env": {
    "CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS": "1"
  }
}
```

### Display Modes

Set `teammateMode` in `~/.claude.json`:

| Mode | Where | How |
|------|-------|-----|
| `auto` (default) | Split panes if tmux, in-process otherwise | Automatic |
| `in-process` | All in main terminal | Shift+Down to cycle |
| `tmux` | Dedicated panes per teammate | Click pane to interact |

```json
{ "teammateMode": "in-process" }
```

Or: `claude --teammate-mode in-process`

> **Note:** Split panes only work in tmux or iTerm2. Not VS Code terminal, Windows Terminal, or Ghostty.

---

## 3. Core Tools

| Tool | Who | Purpose |
|------|-----|---------|
| `TeamCreate` | Lead only | Create a new agent team |
| `TeamDelete` | Lead only | Remove team and resources |
| `SendMessage` | Any agent | Message a specific teammate or broadcast |
| `TaskCreate` | Any agent | Add a task to the shared list |
| `TaskUpdate` | Any agent | Update task status (in_progress, completed, blocked) |
| `TaskList` | Any agent | View all tasks and statuses |
| `TaskGet` | Any agent | Get details of a specific task |

### Task States

```
pending → in_progress → completed
                ↓
              blocked (auto-unblocks when dependency completes)
```

---

## 4. Permission Model

- Teammates inherit the lead's permission settings at spawn
- Cannot set per-teammate permissions at spawn time
- Can change teammate permissions after spawning
- Pre-approve common operations in settings before spawning to reduce prompts
- If lead runs `--dangerously-skip-permissions`, all teammates do too

### Teammate Modes

| Mode | Behavior | Use When |
|------|----------|----------|
| **default** | Full file access + tool use | Implementation tasks |
| **plan** | Read-only until plan approved | Architecture, refactoring |
| **delegate** | Restricted permissions | Focused delegation |

### Plan Approval Workflow

```
Teammate plans (read-only) → Sends plan to lead → Lead approves/rejects
                                                        ↓
                                              Approved: teammate implements
                                              Rejected: teammate revises
```

---

## 5. Communication Patterns

### Direct Message
Send to one specific teammate. Use for targeted instructions, feedback, or follow-ups.

### Broadcast
Send to all teammates simultaneously. Use sparingly — costs scale with team size. Best for:
- Critical constraint changes
- Shared interface definitions
- Emergency course corrections

### Idle Notifications
Teammates automatically notify the lead when they finish. The lead does NOT need to poll.

### Hooks (Quality Gates)

```json
{
  "hooks": {
    "TeammateIdle": "exit 2 to send feedback and keep working",
    "TaskCreated": "exit 2 to prevent creation and send feedback",
    "TaskCompleted": "exit 2 to prevent completion and send feedback"
  }
}
```

---

## 6. Team Sizing & Task Design

### Team Size Guidelines

| Team Size | Best For |
|-----------|----------|
| 2-3 | Focused parallel work, review + implement |
| 3-5 | Feature development, multi-layer changes |
| 5+ | Large-scale research, competing hypotheses |

**Rule of thumb:** 5-6 tasks per teammate. If you have 15 tasks, spawn 3 teammates.

### Task Sizing

| Size | Problem | Fix |
|------|---------|-----|
| Too small | Coordination overhead > benefit | Bundle related work |
| Too large | Teammates work too long without check-ins | Break into deliverables |
| Just right | Self-contained unit, clear deliverable | One function, one test file, one review |

### File Ownership (CRITICAL)

**Two teammates editing the same file = overwrites.** Always ensure each teammate owns a distinct set of files.

```
✓ Teammate A: src/auth/login.py, src/auth/token.py
✓ Teammate B: src/api/routes.py, src/api/middleware.py
✓ Teammate C: tests/test_auth.py, tests/test_api.py

✗ Teammate A: src/auth/login.py
✗ Teammate B: src/auth/login.py  ← CONFLICT
```

---

## 7. Effective Patterns

### Pattern 1: Parallel Review (3 Lenses)

```
Lead: "Review PR #142 with three perspectives"
├── Teammate A: Security review
├── Teammate B: Performance review
└── Teammate C: Test coverage review

Each reports independently → Lead synthesizes
```

**Why:** Single reviewers gravitate to one concern. Parallel review ensures thoroughness.

### Pattern 2: Competing Hypotheses (Debug)

```
Lead: "App crashes after one message. Investigate competing theories."
├── Teammate A: Connection lifecycle hypothesis
├── Teammate B: Memory leak hypothesis
├── Teammate C: Race condition hypothesis
├── Teammate D: Configuration error hypothesis
└── Teammate E: External dependency hypothesis

Teammates debate and disprove each other → Consensus emerges
```

**Why:** Fights anchoring bias. One investigator finds a plausible-but-wrong explanation and stops; multiple investigators actively challenging each other find the real root cause.

### Pattern 3: Feature Development (Layer Split)

```
Lead: "Add user authentication"
├── Teammate A (Backend): JWT flow, DB schema, API endpoints
├── Teammate B (Frontend): Login UI, token storage, auth guards
└── Teammate C (Tests): Unit + integration tests for both layers

Coordinate on interface contract → Implement independently
```

**Why:** Clear separation prevents file conflicts. Interface contract prevents integration surprises.

### Pattern 4: Research + Implement

```
Lead: "Research and implement caching strategy"
├── Teammate A (Researcher): Survey options, benchmarks, tradeoffs
├── Teammate B (Architect): Design cache layer based on research
└── Teammate C (Implementer): Build it after architect produces spec

Dependency chain: A → B → C (auto-unblocks)
```

**Why:** Research informs design, design informs implementation. Dependencies ensure correct ordering.

### Pattern 5: Plan-Then-Execute (Gated)

```
Lead: "Refactor auth module. Require plan approval."
├── Teammate (plan mode): Analyzes code, proposes refactoring plan
│   → Lead reviews plan, approves
└── Teammate (exits plan mode): Implements approved plan
```

**Why:** Prevents wasted work on wrong approach. Read-only planning catches issues early.

### Pattern 6: Red Team / Blue Team

```
Lead: "Implement and security-test the new API endpoint"
├── Teammate A (Blue): Implement the endpoint with security best practices
└── Teammate B (Red): Attack Teammate A's implementation, find vulnerabilities

B reports issues → A fixes → B re-tests
```

**Why:** Adversarial testing catches issues that the implementer is blind to.

---

## 8. Agent Teams vs. Subagents — Decision Matrix

| Factor | Agent Teams | Subagents (Agent tool) |
|--------|-------------|------------------------|
| Context | Own full context window | Own context, results summarized back |
| Communication | Direct peer messaging | Report to caller only |
| Coordination | Shared task list, self-coordinate | Caller manages all work |
| Persistence | Long-running, own session | Short-lived, result returned |
| File editing | Full capability | Full capability |
| Cost | ~3-4x tokens per teammate | Lower, results compressed |
| Best for | Complex collaborative work | Focused independent tasks |

### When to Use Agent Teams
- Teammates need to share findings and challenge each other
- Work requires discussion, debate, or iterative refinement
- Tasks have complex dependencies
- You want parallel implementation with coordination
- Debugging with competing hypotheses

### When to Use Subagents
- Quick focused tasks (search, review, test run)
- Result is what matters, not the process
- No inter-agent communication needed
- Cost efficiency matters
- Simple parallelism without coordination

### When to Use Neither (Single Session)
- Sequential work with heavy interdependence
- Simple tasks that don't benefit from parallelism
- Routine operations

---

## 9. NovaCore-Specific Team Templates

### Template: NovaTrade Strategy Development

```
Lead: "Develop and validate a new trading strategy"
├── Teammate A (Researcher): Backtest parameter space, find viable configs
├── Teammate B (Implementer): Code the strategy engine changes
├── Teammate C (Tester): Write comprehensive test suite
└── Teammate D (Reviewer): Security + risk review of execution path

Dependencies: A → B → C (parallel with D reviewing B's output)
```

### Template: System Health Audit

```
Lead: "Full system health audit"
├── Teammate A: Service staleness check + systemd status
├── Teammate B: NovaTrade live loop diagnostics
├── Teammate C: Memory system health (Fusion Memory + Obsidian)
└── Teammate D: Test suite run + coverage report

All parallel → Lead synthesizes report
```

### Template: Multi-Agent Implementation (from v32 Playbook)

```
Lead (Chief Orchestrator): Validates plan, coordinates phases
├── Teammate A (Implementer): Writes code per approved plan
├── Teammate B (Reviewer): Independent critical review of A's code
└── Teammate C (Verifier): Runs tests, checks integration

Sequence: A implements → B reviews → A fixes → C verifies
Loop until B and C both approve
```

### Template: Research Sprint

```
Lead: "Research prop firm rules for automated trading"
├── Teammate A: FTMO rules, limits, restrictions
├── Teammate B: FundedNext rules and comparison
├── Teammate C: E8 Funding rules and comparison
├── Teammate D: Regulatory and compliance considerations
└── Teammate E: Technical integration requirements

All parallel → Lead synthesizes comparison matrix
```

---

## 10. Anti-Patterns (What NOT to Do)

| Anti-Pattern | Problem | Fix |
|--------------|---------|-----|
| **Too many teammates** | Token cost explodes, coordination overhead | Cap at 5 unless research-only |
| **Overlapping file ownership** | Teammates overwrite each other | Assign distinct file sets |
| **No context in spawn prompt** | Teammates lack task-specific details | Include paths, constraints, goals |
| **Fire and forget** | Teams drift, waste tokens | Check in periodically, redirect |
| **Broadcast everything** | Every message multiplied by team size | Use targeted messages |
| **One task per teammate** | Wasted teammate overhead | Bundle 5-6 tasks per teammate |
| **Lead does the work** | Defeats the purpose | "Wait for teammates to complete" |
| **Sequential tasks in parallel** | Dependencies violated | Use task dependencies |
| **Resuming with in-process mates** | /resume doesn't restore them | Start fresh team after resume |

---

## 11. Limitations (Current — Experimental)

1. **No session resumption** with in-process teammates — `/resume` and `/rewind` don't restore them
2. **Task status can lag** — teammates sometimes forget to mark tasks complete
3. **Shutdown can be slow** — teammates finish current requests before stopping
4. **One team per session** — clean up before starting a new one
5. **No nested teams** — teammates cannot spawn their own teams
6. **Lead is fixed** — no promotion or leadership transfer
7. **Permissions set at spawn** — can't set per-teammate modes at creation time
8. **Split panes** only in tmux/iTerm2

---

## 12. Cost Optimization

- **Start small**: 2-3 teammates, scale up if needed
- **Use Sonnet/Haiku for teammates** when Opus isn't required (research, testing)
- **Bundle tasks**: 5-6 per teammate reduces per-teammate overhead
- **Time-box**: Set expectations for completion, redirect early if off-track
- **Prefer subagents** for quick focused work that doesn't need inter-agent communication
- **Kill idle teammates** — don't let them sit consuming context

---

*Last updated: 2026-03-31*
*Sources: code.claude.com/docs/en/agent-teams, community guides, NovaCore operational patterns*
