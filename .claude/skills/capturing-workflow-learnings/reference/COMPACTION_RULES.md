# Compaction Rules for Workflow Learnings

## Purpose

Workflow learnings must be compact and reusable. These rules govern how raw execution results are transformed into durable vault notes.

## Rules

### 1. Summarize, do not transcript

- A 500-line execution log becomes 5-10 bullet points
- A 20-step implementation becomes 3-5 key decisions
- A debugging session becomes the root cause + fix + prevention guidance

### 2. Lead with actionable guidance

The "Reusable Guidance" section is the most important part. It should answer: "If someone encounters a similar task next time, what should they do differently or the same?"

Bad: "We encountered several issues during implementation."
Good: "When adding new task classes to the multi-agent path, expect confidence calibration issues in tests. Test texts need at least 2 keyword matches for a 0.5 threshold with 13 patterns."

### 3. Include enough context to stand alone

The learning note must be understandable without the original task file, execution log, or session transcript. Include:
- What type of task was performed
- What approach was taken
- Why decisions were made (not just what was decided)

### 4. Omit session-specific details

Remove unless they ARE the lesson:
- Exact timestamps
- Transient file paths (e.g., `/tmp/build_abc123/`)
- Intermediate debugging steps that didn't lead to the solution
- Tool output that is only meaningful in the original context

### 5. Preserve quantitative outcomes

Keep concrete metrics when available:
- Test counts (before/after)
- Performance numbers
- Error rates
- Lines of code changed

### 6. One learning per note

Each note should capture one coherent workflow outcome. If a session produced two unrelated learnings, create two notes.

### 7. Size targets

- **Minimum**: 200 bytes — anything shorter is probably too thin
- **Target**: 500-1500 bytes — compact but complete
- **Maximum**: 34816 bytes (vault limit) — but aim much lower
- If you need more than 2000 bytes, consider whether you are transcript-dumping

## Anti-patterns

| Anti-pattern | Why it's bad | Fix |
|-------------|-------------|-----|
| Pasting full execution logs | Not reusable, too noisy | Extract the 3-5 key insights |
| Vague platitudes ("testing is important") | Not actionable | Specific guidance ("test at the confidence boundary") |
| Missing context | Can't understand without original task | Add task summary and approach |
| Multiple unrelated learnings in one note | Hard to search and retrieve | Split into separate notes |
| Recording speculative conclusions | May be wrong, pollutes vault | Only capture verified outcomes |
