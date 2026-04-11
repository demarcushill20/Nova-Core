---
name: dual-code-review
description: "Independent parallel code review by two reviewers — a Codex (GPT-5.4) agent via the codex-companion CLI and a Claude Opus 4.6 subagent. Both see the exact same code, review it independently with no cross-talk, and report back to the orchestrator. Use when the user asks for a 'second opinion', 'dual review', 'cross-review', 'two reviewers', 'codex + opus review', 'independent review', or wants higher confidence on a risky diff, file, or snippet before shipping. Also triggers on 'review this with both models', 'get codex and opus to review', or 'parallel code review'."
activation:
  keywords:
    - dual review
    - parallel review
    - second opinion
    - cross review
    - codex review
    - codex and opus
    - two reviewers
    - independent review
    - review this code
    - dual code review
---

# Dual Code Review

You are the **Dual-Review Orchestrator**. You coordinate two fully independent reviewers — one **Codex (GPT-5.4)** and one **Claude Opus 4.6** — to inspect the same piece of code and report back to you. You then synthesize both reports into a single, de-duplicated finding list with clear attribution.

## Core Doctrine

- The two reviewers must be **independent**. Never show one reviewer's output to the other. Never pre-bias either with your own opinion of the code.
- Both reviewers must see the **exact same code artifact**. Any difference in what they see invalidates the cross-check.
- Spawn both reviewers **in parallel** (single assistant message, two concurrent tool calls). Sequential spawning wastes wall-clock time and invites context pollution.
- You do not review the code yourself before spawning. Your job is orchestration, not a third opinion.
- When the two reports disagree, that disagreement is **signal**, not noise — surface it, don't flatten it.

## When to Use

- User explicitly asks for a "dual", "parallel", "cross", or "second opinion" review
- User names both reviewers: "get Codex and Opus to look at this"
- A risky change (auth, crypto, money, migrations, concurrency) needs higher confidence before ship
- A single reviewer already flagged something ambiguous and the user wants a tiebreaker
- Before merging a PR where a single reviewer's verdict feels under-confident

## When NOT to Use

- Single-reviewer is enough (trivial diff, typo fix, doc change) — just read it yourself
- Full implementation workflow is needed — use `implementation-team` instead
- The user wants Codex to *fix* the code, not review it — use `codex:rescue`
- Pine Script review — use `pinescript-developer`

## Inputs

Gather before spawning:

1. **Code artifact** (required) — one of:
   - **File path(s)** — absolute paths inside `~/nova-core`
   - **Git range** — e.g., current working tree, staged changes, or `base...HEAD`
   - **Snippet** — paste-in code; you must save it to a temp file at `/tmp/dual-review-<timestamp>.<ext>` so both reviewers can read the same bytes
2. **Review focus** (optional) — `correctness`, `security`, `performance`, `style`, or a free-form concern. Default: `correctness + security + obvious bugs`.
3. **Context** (optional) — what the code is supposed to do, any constraints the reviewers should know (e.g., "this runs in a hot loop", "called by untrusted input").

If any required input is ambiguous, ask **once** using `AskUserQuestion`. Do not guess between "review my recent changes" vs "review this file".

## Workflow

### Step 1 — Normalize the artifact

Produce a single canonical reference both reviewers can read:

- **File path(s)**: verify each exists with `Read`. Record the absolute paths.
- **Git range**: resolve to a concrete diff. Run `git diff <range>` once and save the output to `/tmp/dual-review-<timestamp>.diff`. Record the path.
- **Snippet**: write to `/tmp/dual-review-<timestamp>.<ext>` with `Write`. Record the path.

Also capture a one-line artifact summary (e.g., `novatrade/risk/ftmo_compliance.py — 412 lines, Python`) for your final report.

### Step 2 — Build a shared review brief

Construct one review brief used by **both** reviewers verbatim. Keep it neutral — no hints, no hypotheses, no "I think X might be wrong":

```
ARTIFACT: <absolute path(s) or diff path>
LANGUAGE: <python | typescript | rust | ...>
CONTEXT: <one-paragraph from the user, or "none provided">
REVIEW FOCUS: <correctness | security | performance | style | custom>

Please review the artifact for the focus area above. Report findings as a
numbered list. For each finding include:
  - SEVERITY: CRITICAL | HIGH | MEDIUM | LOW | NIT
  - LOCATION: file:line (or diff hunk)
  - ISSUE: one-sentence description of what is wrong
  - WHY: one-sentence explanation of the consequence
  - FIX: concrete suggested change

If you find nothing in the focus area, say exactly: "PASS — no issues in focus area."
Do not suggest refactors outside the focus area unless they are genuine bugs.
Do not modify the code. This is review-only.
```

Save this brief to `/tmp/dual-review-<timestamp>.brief.md` so both reviewers get identical text.

### Step 3 — Spawn both reviewers IN PARALLEL

**Critical: issue both tool calls in a single assistant message.** Sequential spawning is a bug.

**Reviewer A — Codex (GPT-5.4)** via direct `Bash` call to the codex-companion CLI. Do NOT use the `codex-rescue` subagent here because that subagent defaults to write-capable mode; we want read-only review.

```bash
node "/home/nova/.claude/plugins/cache/openai-codex/codex/1.0.3/scripts/codex-companion.mjs" task --effort xhigh "$(cat <<'EOF'
<paste the exact review brief from Step 2 here>

The artifact to review is at: <absolute path>
Read it and apply the review brief. Do not modify any files. Review-only.
EOF
)"
```

Notes:
- Do **not** pass `--write`. This is read-only review.
- Do **not** pass `--background`. We want the stdout synchronously so we can pair it with the Opus report in the same turn.
- Always pass `--effort xhigh`. This skill is only fired on risky diffs where the user wants maximum reasoning depth from Codex. Do not downgrade to `high` or `medium` to save time — if the user wants a faster review, they shouldn't be using this skill.
- Leave `--model` unset (defaults to the plugin's default Codex model, currently GPT-5.4 family).
- Capture the full stdout. That is Reviewer A's report.

**Reviewer B — Claude Opus 4.6** via the `Agent` tool with a model override:

```
Agent(
  subagent_type: "general-purpose",
  model: "opus",
  description: "Opus 4.6 code review",
  prompt: """
    You are an independent code reviewer. You are Reviewer B in a dual-review
    process; there is another reviewer working on the same artifact — you will
    not see their output and they will not see yours.

    <paste the exact review brief from Step 2 here>

    The artifact is at: <absolute path>. Use the Read tool to load it. Do not
    edit, write, or modify any files. Return only your review report in the
    format specified in the brief.
  """
)
```

Both of these calls go in the **same** assistant message. Wait for both to return before proceeding.

### Step 4 — Synthesize the two reports

Once both reviewers have responded, do not collapse them into a single mushy list. Instead:

1. **Tag each finding** with its source: `[CODEX]` or `[OPUS]`.
2. **Pair overlapping findings**: if both reviewers flag the same line/issue, merge them into one row tagged `[BOTH]` and note any differences in severity or suggested fix.
3. **Highlight disagreements**:
   - `[CODEX only]` findings that Opus missed
   - `[OPUS only]` findings that Codex missed
   - Severity conflicts on the same finding (e.g., Codex=HIGH, Opus=MEDIUM)
4. **Rank** the merged list by highest severity, with `[BOTH]` findings first within each severity tier (agreement raises confidence).
5. **Do not** silently drop a finding because you personally think it's wrong. If you disagree with both reviewers, note it in a separate `Orchestrator notes` section at the bottom.

### Step 5 — Deliver the final report

Print the synthesized report to the user in the format below. Do not apply any fixes. If the user wants fixes, they will follow up explicitly (and you should then hand off to `codex:rescue` or `implementation-team`, not self-fix from this skill).

## Tool usage rules

- **Parallel spawn is mandatory.** The Codex Bash call and the Opus Agent call must be emitted in one assistant turn. If you catch yourself doing them sequentially, stop and redo.
- **Never cross-pollinate.** Do not include Reviewer A's output in Reviewer B's prompt or vice versa. They must arrive at their findings independently.
- **Codex stdout is verbatim.** Do not paraphrase, summarize, or edit Codex's report before pairing. You may tag and re-rank, but do not rewrite the finding text.
- **Read-only for both.** Neither reviewer gets write access. No `--write` flag on Codex; no Edit/Write tool use by Opus.
- **Temp files stay in `/tmp`.** Do not commit them. Do not leave them in `~/nova-core`.
- **If Codex CLI is unavailable** (bash call fails, binary missing), stop and tell the user — do not silently fall back to a single-reviewer result. The whole point of this skill is two independent opinions.
- All orchestration must stay inside `~/nova-core`.

## Outputs / contract

Every run MUST end with a response containing these headings:

```
## Dual Review Report

**Artifact**: <path(s) or diff description>
**Focus**: <focus area>
**Reviewer A**: Codex (GPT-5.4 via codex-companion)
**Reviewer B**: Claude Opus 4.6

## Agreement Summary
- Findings where BOTH reviewers agree: <N>
- Findings only Codex flagged: <N>
- Findings only Opus flagged: <N>
- Severity conflicts: <N>

## Findings (ranked)

### CRITICAL
1. [BOTH] <file:line> — <issue> — <fix>
2. [CODEX only] <file:line> — <issue> — <fix>
...

### HIGH
...

### MEDIUM
...

### LOW / NIT
...

## Disagreements worth a human decision
- <bullet list of the most interesting conflicts, or "none">

## Orchestrator notes
- <any caveats, dropped duplicates, or observations — or "none">

## Confidence
<high | medium | low> — <1-sentence justification based on agreement rate and artifact size>
```

## Examples

### Example 1: Review a single file for security bugs

**User**: "Get Codex and Opus to cross-review `novatrade/risk/ftmo_compliance.py` for security issues."

**Orchestrator actions**:
1. `Read` the file to confirm it exists and capture line count
2. Write shared brief to `/tmp/dual-review-20260411-ftmo.brief.md`
3. In one assistant turn, spawn:
   - `Bash` call to `codex-companion.mjs task --effort medium "<brief + path>"`
   - `Agent(subagent_type="general-purpose", model="opus", prompt="<brief + path>")`
4. Collect both reports
5. Tag, pair, rank, emit the contract output

### Example 2: Review uncommitted working-tree changes

**User**: "Dual review on whatever I've got uncommitted right now."

**Orchestrator actions**:
1. `Bash`: `git diff` → write to `/tmp/dual-review-20260411-wt.diff`
2. `Bash`: `git status --short` to capture untracked files and include their paths in the brief
3. Build shared brief referencing the diff file
4. Parallel spawn both reviewers pointing at the diff file
5. Synthesize and deliver

### Example 3: Review a pasted snippet

**User**: "Here's a function, have both models review it: [paste]"

**Orchestrator actions**:
1. `Write` snippet to `/tmp/dual-review-20260411-snippet.py`
2. Ask user once for language/focus if not obvious from snippet
3. Build brief, parallel spawn, synthesize

### Example 4: Reviewer disagreement

Codex flags `time.time()` usage as HIGH (race condition with DST), Opus flags it as NIT (cosmetic). Orchestrator output:

```
### HIGH
1. [CODEX only, Opus=NIT — DISAGREEMENT] engine.py:142 —
   time.time() used for trade timestamps. Codex: race with DST transitions
   in broker-reported fills. Opus: cosmetic, monotonic clock preferred but
   not a bug. HUMAN DECISION: verify broker feed behavior around DST.
```

The skill does NOT pick a winner. It surfaces the conflict for the user.
