---
name: sequential-thinking
description: "Structured step-by-step reasoning for complex problems using the Sequential Thinking MCP server. Provides auditable thought chains with branching, revision, and dynamic extension. Invoke explicitly via /sequential-thinking for hard problems."
disable-model-invocation: false
allowed-tools:
  - mcp__sequential-thinking__sequentialthinking
activation:
  keywords: [think through, reason step by step, break down, analyze carefully, think hard, complex problem]
  when:
    - Problem requires multi-step reasoning with dependencies between steps
    - Architecture or design decision with multiple trade-offs
    - Debugging a complex issue with multiple possible causes
    - Planning a multi-phase implementation
    - User explicitly asks to "think through" or "reason about" something
tool_doctrine:
  reasoning:
    workflow:
      - estimate_steps_before_starting
      - one_clear_thought_per_step
      - revise_when_evidence_contradicts
      - branch_to_explore_alternatives
      - converge_to_a_conclusion
output_contract:
  required:
    - summary
    - total_thoughts
    - conclusion
    - verification
    - confidence
---

# Sequential Thinking

## When to use

- **Architecture decisions**: Choosing between approaches with multiple trade-offs (e.g., "should we use Redis or Postgres for this?")
- **Complex debugging**: Narrowing down a bug with multiple possible root causes through systematic elimination
- **Implementation planning**: Breaking a large task into ordered steps with dependencies
- **Risk analysis**: Evaluating the consequences of a change across multiple system components
- **Trade-off analysis**: When the answer isn't obvious and requires weighing competing concerns

## When NOT to use

- Simple factual questions with clear answers
- Straightforward code changes with obvious implementations
- Tasks where you already know the approach
- Quick lookups or file reads
- Any task that can be solved in 1-2 mental steps

## Inputs

- **problem**: The problem statement or question to reason through (required)
- **estimated_steps**: How many thinking steps you expect to need. Default: 5. Range: 3-15.
- **mode**: `linear` (step-by-step), `exploratory` (with branching), or `elimination` (narrowing down). Default: `linear`

## Workflow

### Step 1 -- Frame the Problem

State the problem clearly in the first thought. Include:
- What we know (constraints, facts)
- What we need to decide or discover
- Why this requires structured reasoning

```
Tool: mcp__sequential-thinking__sequentialthinking
Args: {
  "thought": "Problem: [clear statement]. Known constraints: [list]. We need to determine: [goal]. This requires structured reasoning because [reason].",
  "nextThoughtNeeded": true,
  "thoughtNumber": 1,
  "totalThoughts": 5
}
```

### Step 2-N -- Reason Through Steps

Each subsequent thought should:
- Build on previous thoughts
- Address one specific aspect of the problem
- State any new information or conclusions

```
Tool: mcp__sequential-thinking__sequentialthinking
Args: {
  "thought": "Considering [aspect]: [analysis]. This means [implication]. Combined with thought 1, we can conclude [intermediate conclusion].",
  "nextThoughtNeeded": true,
  "thoughtNumber": 2,
  "totalThoughts": 5
}
```

### Revision (when needed)

If new evidence contradicts an earlier thought:

```
Tool: mcp__sequential-thinking__sequentialthinking
Args: {
  "thought": "Revising thought 2: [what changed and why]. The corrected understanding is [new conclusion].",
  "nextThoughtNeeded": true,
  "thoughtNumber": 4,
  "totalThoughts": 6,
  "isRevision": true,
  "revisesThought": 2
}
```

### Branching (exploratory mode)

To explore an alternative path:

```
Tool: mcp__sequential-thinking__sequentialthinking
Args: {
  "thought": "Alternative approach: [description]. Exploring this because [reason].",
  "nextThoughtNeeded": true,
  "thoughtNumber": 4,
  "totalThoughts": 7,
  "branchFromThought": 3,
  "branchId": "alternative-a"
}
```

### Final Thought -- Conclude

The last thought must synthesize all reasoning into a clear conclusion:

```
Tool: mcp__sequential-thinking__sequentialthinking
Args: {
  "thought": "Conclusion: Based on the analysis in thoughts 1-4, the best approach is [decision]. Key reasons: [list]. Risks: [list]. Next steps: [list].",
  "nextThoughtNeeded": false,
  "thoughtNumber": 5,
  "totalThoughts": 5
}
```

## Tool Usage Rules

- **One clear idea per thought.** Don't pack multiple analyses into a single step.
- **Reference earlier thoughts by number.** "As established in thought 2..." keeps the chain traceable.
- **Revise rather than contradict.** If thought 3 invalidates thought 1, use the revision mechanism.
- **Extend rather than rush.** If you need more steps, set `needsMoreThoughts: true` and increase `totalThoughts`.
- **Always conclude.** The final thought must have `nextThoughtNeeded: false` and a clear decision/answer.
- **Keep thoughts concise.** Each thought should be 2-4 sentences. If it's longer, split it.

## Failure Handling

- If the MCP server is unreachable: fall back to inline reasoning (no tool calls), note it in the contract.
- If you realize mid-chain the problem is simpler than expected: conclude early with `nextThoughtNeeded: false`.
- If you hit 15 thoughts without convergence: force a conclusion summarizing what is known and what remains uncertain.

## Outputs / Contract

After completing the thought chain, present the results:

```
## Sequential Thinking Contract
summary: <problem and conclusion in one line>
total_thoughts: <number of thoughts used>
revisions: <number of revisions, or 0>
branches: <number of branches explored, or 0>
conclusion: <the final decision or answer>
key_reasons:
  - <reason 1>
  - <reason 2>
risks:
  - <risk 1, or "none identified">
verification: <how the conclusion was validated>
confidence: <high | medium | low>
```

## Examples

### Example 1: Architecture decision (linear mode)

**Problem**: "Should nova-core use SQLite or PostgreSQL for task metadata?"

| # | Thought | Key Point |
|---|---------|-----------|
| 1 | Frame: single-operator VPS, 4 CPU, 7.7GB RAM. Need to store task metadata, execution history, metrics. | Constraints established |
| 2 | SQLite: zero config, file-based, perfect for single-writer. Handles 100K+ rows easily. | SQLite pros |
| 3 | PostgreSQL: powerful but needs a daemon, 50-100MB RAM overhead, config management. | PostgreSQL cons for this setup |
| 4 | nova-core's access pattern: single writer (watcher), occasional reads (heartbeat, dashboard). No concurrent writes. | Access pattern favors SQLite |
| 5 | Conclusion: SQLite. Single-writer pattern, no daemon overhead, backup is just copying a file. Migrate to PG only if multi-agent needs concurrent writes. | Decision made |

**Contract**:
```
summary: SQLite over PostgreSQL for task metadata — single-writer pattern, zero overhead
total_thoughts: 5
revisions: 0
branches: 0
conclusion: Use SQLite for task metadata storage
key_reasons:
  - Single-writer access pattern (watcher only)
  - Zero daemon overhead on resource-constrained VPS
  - Backup is a file copy, fits existing tar backup skill
risks:
  - Must migrate if Phase 7 multi-agent needs concurrent writes
verification: Validated against nova-core's actual access patterns in watcher.py
confidence: high
```

### Example 2: Debugging with elimination

**Problem**: "Telegram bot stops responding after 4 hours"

| # | Thought | Key Point |
|---|---------|-----------|
| 1 | Three possible causes: memory leak, connection timeout, rate limiting | Hypotheses listed |
| 2 | Check memory: heartbeat shows stable RSS at ~180MB over 24h. Eliminates memory leak. | Memory leak eliminated |
| 3 | Check connections: python-telegram-bot uses long polling with 30s timeout. After network blip, it may not reconnect. | Connection timeout is plausible |
| 4 | Check rate limiting: bot handles ~20 msgs/day, well under Telegram's 30 msg/s limit. Eliminates rate limiting. | Rate limiting eliminated |
| 5 | Conclusion: Connection timeout after network interruption. Fix: add exponential backoff retry in the polling loop. | Root cause identified |

**Contract**:
```
summary: Telegram bot timeout caused by missing reconnect logic after network blips
total_thoughts: 5
revisions: 0
branches: 0
conclusion: Add exponential backoff retry to polling loop
key_reasons:
  - Memory stable (eliminates leak)
  - Rate well under limits (eliminates throttling)
  - Long polling has no auto-reconnect on network interruption
risks:
  - none identified
verification: Corroborated by python-telegram-bot issue #3847
confidence: high
```
