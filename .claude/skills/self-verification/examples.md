# self-verification Examples

## Example 1: Successful File Modification Verification

**Scenario:** After editing `tools/skills.py` to fix a regex pattern.

**Workflow:**

1. Re-read `tools/skills.py` after the edit.
2. Confirm the regex on line 42 now reads `r"\b(?:bash|sudo)\b"` as intended.
3. Confirm no other lines changed (diff shows single-line edit).
4. File exists, non-zero size.

**Output Contract:**

```
## CONTRACT
summary: Verified tools/skills.py edit — regex pattern updated correctly
checks_performed:
  - Re-read file after write (pass)
  - Diff shows single-line change on line 42 (pass)
  - File exists with non-zero size (pass)
result: pass
confidence: high
```

---

## Example 2: Shell Command Verification

**Scenario:** After running `pip install requests` inside the sandbox.

**Workflow:**

1. Check exit code of `pip install requests` — exit 0.
2. Check stderr for warnings — none.
3. Run follow-up: `pip show requests` — confirms package installed, version 2.31.0.
4. Confirm no system-level changes (pip installed to user/venv, not system site-packages).

**Output Contract:**

```
## CONTRACT
summary: Verified pip install requests — package present at expected version
checks_performed:
  - Exit code 0 (pass)
  - No stderr warnings (pass)
  - pip show requests confirms version 2.31.0 (pass)
result: pass
confidence: high
```

---

## Example 3: Git Commit Verification

**Scenario:** After committing changes to three files.

**Workflow:**

1. Run `git log -1 --oneline` — confirms hash `a1b2c3d` with expected message.
2. Run `git diff HEAD~1 --stat` — shows exactly 3 files changed.
3. Run `git status` — working tree clean, no unstaged changes.
4. Confirm no secrets in committed files (grep for token prefixes).

**Output Contract:**

```
## CONTRACT
summary: Verified git commit a1b2c3d — 3 files committed, working tree clean
checks_performed:
  - git log confirms commit hash and message (pass)
  - git diff --stat shows 3 files (pass)
  - git status shows clean working tree (pass)
  - No secret patterns in committed content (pass)
result: pass
confidence: high
```

---

## Example 4: Contract Validation Failure

**Scenario:** A task-execution skill output is missing the `verification` field.

**Workflow:**

1. Parse the output contract from `OUTPUT/0010_generate_report_20260302_143012.md`.
2. Check required fields: `summary` (present), `task_id` (present), `status` (present), `verification` (MISSING).
3. Mark contract as invalid.

**Output Contract:**

```
## CONTRACT
summary: Contract validation failed — missing 'verification' field in task output
checks_performed:
  - summary field present (pass)
  - task_id field present (pass)
  - status field present (pass)
  - verification field present (fail)
result: fail
confidence: high
```

---

## Example 5: Partial Verification with Reduced Confidence

**Scenario:** After a file edit, the read-after-write succeeds but the diff check is inconclusive because the file is too large to fully diff.

**Workflow:**

1. Re-read `OUTPUT/0009_log_triage_20260302_150045.md` — file exists, non-zero size.
2. Check for expected header line "# Task: 0009_log_triage" — present.
3. Check for contract footer — present.
4. Full diff comparison skipped (file exceeds 50 KB, only sampled key sections).
5. Cannot confirm no unintended changes outside sampled regions.

**Output Contract:**

```
## CONTRACT
summary: Partial verification of large output file — key sections confirmed, full diff skipped
checks_performed:
  - File exists with non-zero size (pass)
  - Expected header present (pass)
  - Contract footer present (pass)
  - Full diff comparison (skipped — file too large)
result: partial
confidence: medium
```
