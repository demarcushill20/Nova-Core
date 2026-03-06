# Skill: code_improve

Autonomous code improvement skill. Uses NovaCore's semantic repo tools to
locate, analyze, patch, and verify code improvements in a self-contained
engineering loop.

---

## Frontmatter

```yaml
name: code_improve
version: 1.0.0
description: >
  Discover code quality issues, propose minimal improvements, apply patches,
  and verify correctness — all within the repo sandbox using semantic tools.

tools:
  - repo.search
  - repo.files.read
  - repo.files.patch
  - repo.diff
  - contracts.validate
  - shell.run

tool_rules:
  - "diff-first edits only"
  - "never edit outside repo sandbox"
  - "always validate output contract"

output_contract:
  - summary
  - files_changed
  - verification
  - confidence
```

---

## When To Use

- A task requests improving existing code quality (readability, correctness,
  performance, consistency).
- A task identifies a specific file or pattern to refine.
- As part of a scheduled autonomous improvement sweep.
- When another skill delegates a "clean up" or "refactor" sub-task.

Do **not** use for:
- New feature development (use task-execution instead).
- Destructive operations (deletions, rewrites from scratch).
- Changes outside `~/nova-core`.

---

## Workflow

### 1. Discover candidate file

Use `repo.search` to locate code matching the improvement target.

```
Tool: repo.search
Args: { "query": "<pattern or keyword>", "path": "<optional subdir>" }
```

Select the highest-priority match based on:
- Frequency of the pattern (more occurrences → higher impact).
- Proximity to core logic (tools/, watcher.py, etc.).
- Avoidance of generated or vendored files.

### 2. Inspect file

Use `repo.files.read` to load the full file content.

```
Tool: repo.files.read
Args: { "path": "<relative path from step 1>" }
```

Analyze the content for:
- Code smells (duplicated logic, inconsistent naming, missing error handling).
- Correctness issues (off-by-one, unclosed resources, unhandled edge cases).
- Performance opportunities (unnecessary re-computation, O(n²) where O(n) suffices).
- Style/consistency deviations from project conventions.

### 3. Propose minimal improvement

Before editing, formulate the change as a precise description:
- What: the specific code transformation.
- Why: the concrete benefit (bug fix, readability, performance).
- Scope: which lines are affected.
- Risk: what could break (imports, callers, tests).

**Rule**: prefer the smallest correct change. One improvement per invocation.
Do not refactor surrounding code or add unrelated fixes.

### 4. Apply edit

Use `repo.files.patch` with structured replace operations.

```
Tool: repo.files.patch
Args: {
  "path": "<relative path>",
  "operations": [
    { "type": "replace", "old": "<exact existing text>", "new": "<replacement text>" }
  ]
}
```

Rules:
- `old` must be an exact substring of the current file content.
- `new` must differ from `old` (no-op patches are rejected).
- Preserve surrounding whitespace and indentation exactly.
- One logical change per operation; chain multiple operations only when
  they form a single atomic improvement.

### 5. Inspect result

Use `repo.diff` to confirm the change is minimal and correct.

```
Tool: repo.diff
Args: { "path": "<relative path>" }
```

Verify:
- Only intended lines changed.
- No unintended whitespace or formatting drift.
- The diff is small and reviewable.

If the diff shows unintended changes, **stop and report failure** rather
than attempting to fix forward.

### 6. Run verification

Use `shell.run` to execute the relevant test suite or linter.

```
Tool: shell.run
Args: { "command": "python -m pytest tests/ -x -q --tb=short", "timeout": 120 }
```

Acceptable verification commands (choose the most relevant):
- `python -m pytest tests/ -x -q --tb=short` — unit tests.
- `python -m pytest tests/test_<module>.py -x -q` — targeted test file.
- `python -c "import <module>"` — smoke test for syntax/import errors.
- `python -m py_compile <file>` — syntax-only check when no tests exist.

If verification fails:
- Record the failure in the contract.
- Do **not** attempt automatic rollback (the diff is already recorded).
- Set confidence to `low`.

### 7. Validate contract

Use `contracts.validate` to check the output report.

```
Tool: contracts.validate
Args: { "text": "<full output including ## CONTRACT block>" }
```

The contract must pass validation before the skill reports success.
If validation fails, fix the contract format and re-validate.

### 8. Produce final report

Assemble the complete output with all sections:

```markdown
# code_improve: <one-line summary>

## Target
- **File**: <path>
- **Issue**: <what was wrong>
- **Category**: <bug | readability | performance | consistency | style>

## Change
<description of what was changed and why>

## Diff
```diff
<paste diff output>
```

## Verification
- **Command**: <what was run>
- **Result**: <pass | fail>
- **Details**: <relevant output excerpt>

## CONTRACT
summary: <one-line description of what was improved>
files_changed: <path> (replace)
verification: <command and result>
confidence: <high | medium | low>
```

---

## Confidence Scoring

| Level  | Criteria |
|--------|----------|
| high   | Tests pass, diff is minimal, change is mechanical/obvious |
| medium | Tests pass but change involves logic, OR no targeted tests exist |
| low    | Tests fail, verification inconclusive, or change has risk |

---

## Failure Handling

- **No candidates found**: Report "no improvement candidates" with confidence `high` (correct negative).
- **Patch fails** (old text not found): Re-read the file to check for drift, report the mismatch.
- **Diff shows unintended changes**: Report failure, do not attempt to fix.
- **Tests fail after patch**: Record failure, include the test output, set confidence `low`.
- **Contract validation fails**: Fix the contract block and re-validate (up to 2 retries).

---

## Tool Doctrine

1. **Read before write** — always inspect the file before patching.
2. **Diff-first edits only** — formulate the exact old/new strings before calling patch.
3. **Never edit outside repo sandbox** — all paths must be relative to `~/nova-core`.
4. **Minimal change** — one improvement per invocation; resist scope creep.
5. **Verify after every mutation** — diff, then test, then validate contract.
6. **Honest confidence** — score reflects actual evidence, not optimism.
7. **Fail loudly** — if anything goes wrong, report it clearly in the contract.
