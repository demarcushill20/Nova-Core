# Execution Skill Template

Use this template when creating a new `SKILLS/<name>/SKILL.md` execution skill for the autonomous pipeline.

---

```markdown
---
name: {{skill-name}}
version: 1.0.0
description: "{{What this skill does in the autonomous pipeline}}"
---

# {{Skill Title}}

# When To Use

- {{Trigger condition 1 — what system state or event triggers this}}
- {{Trigger condition 2}}
- {{Trigger condition 3}}

# Workflow

1. **{{Discover/Receive}}** — {{how the skill gets its input}}
2. **{{Read/Parse}}** — {{understand the input before acting}}
3. **{{Validate}}** — {{check preconditions}}
4. **{{Execute}}** — {{the core work}}
5. **{{Write output}}** — {{persist results before marking done}}
6. **{{Complete}}** — {{update state / lifecycle transition}}
7. **{{Log}}** — {{write execution summary}}

# Tool Usage Rules

- All operations must stay within `~/nova-core`.
- {{Constraint 1 — explain why}}
- {{Constraint 2 — explain the reasoning}}
- Always read inputs before acting — never act on filename or metadata alone.
- Write output files **before** updating lifecycle state.
- Log every major action to `LOGS/`.

# Verification

After every execution:

1. {{Confirm output file exists with non-zero size}}
2. {{Confirm lifecycle state is correct (e.g., .done or .failed)}}
3. {{Confirm log file exists with execution details}}
4. {{Domain-specific verification step}}

# Failure Handling

- **{{Failure scenario 1}}**: {{recovery action — rename to .failed, log error, write failure report}}
- **{{Failure scenario 2}}**: {{recovery action}}
- **{{Crash during execution}}**: {{how the watcher detects and recovers orphaned state}}
- **{{Output write fails}}**: {{keep in-progress state, log the failure}}

# Output Contract

Every execution must end with a machine-checkable contract:

\```
## CONTRACT
summary: <one-line description of what was done>
task_id: <identifier>
status: <done | failed>
files_changed: <comma-separated paths, or "none">
verification: <how correctness was confirmed>
confidence: <low | medium | high>
\```
```

---

## Template Notes

- **name**: kebab-case, must match directory name under `SKILLS/`
- **version**: semver. Start at 1.0.0.
- **Workflow**: 6-8 steps. Always include read-before-act, write-before-complete, and log steps.
- **Tool Usage Rules**: Be explicit about safety boundaries. Execution skills run autonomously — mistakes aren't caught by a human in the loop.
- **Verification**: Must be deterministic — checkable by the supervisor skill.
- **Failure Handling**: Cover every failure mode. Execution skills must handle crashes gracefully since there's no human to intervene.
- **Output Contract**: Required fields: summary, status, verification, confidence. Add domain-specific fields as needed.
- **Target length**: 60-120 lines. Execution skills should be tighter than prompt skills.
