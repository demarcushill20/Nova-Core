---
name: ship-rebase-conflict-resolution
description: "Resolve merge conflicts during ship/push when remote has diverged (e.g., merged PR), using a deterministic rebase-and-resolve strategy."
activation:
  keywords:
    - ship
    - push rejected
    - non-fast-forward
    - merge conflict
    - rebase conflict
    - diverged
    - behind remote
    - pull rebase
  when:
    - "git push fails with non-fast-forward during /ship"
    - "Remote main has new commits from a merged PR or direct push"
    - "Rebase produces merge conflicts that need resolution"
tool_doctrine:
  git:
    workflow:
      - fetch_before_rebase
      - resolve_conflicts_file_by_file
      - verify_after_each_resolution
      - never_force_push
output_contract:
  required:
    - conflict_files
    - resolution_strategy_per_file
    - verification
    - final_push_status
---

# Ship Rebase Conflict Resolution

Deterministic strategy for resolving merge conflicts when `git push` fails during `/ship` because the remote has diverged (typically from a merged PR).

## When This Applies

- `/ship` step 3 (`git push`) fails with `! [rejected] ... (non-fast-forward)`
- `git status` shows the local branch is behind `origin/main`
- A PR was merged on GitHub while local work was in progress

## Step-by-Step Resolution

### 1. Diagnose Divergence

```bash
git fetch origin
git log --oneline HEAD..origin/main   # what remote has that we don't
git log --oneline origin/main..HEAD   # what we have that remote doesn't
```

Understand which commits diverged and what files they touch before attempting resolution.

### 2. Rebase onto Remote

```bash
git rebase origin/main
```

If the runner blocks `git rebase`, use `git pull --rebase` as the equivalent:

```bash
git pull --rebase origin main
```

### 3. Resolve Conflicts (per-file heuristics)

When rebase stops at a conflict, for each conflicted file:

**a. Read the conflict markers** — use `git diff` and the Read tool to inspect each conflicted file. Understand both sides.

**b. Apply resolution heuristic (in priority order)**

| Scenario | Strategy | Rationale |
|----------|----------|-----------|
| Local file was the primary change, remote only touched unrelated sections | Keep local (ours), manually merge remote's unrelated hunks | Preserves intent of both sides |
| Remote file is a structural refactor (renamed fields, new signatures) | Accept remote (theirs) as base, re-apply local logic on top | Structural changes are harder to merge piecemeal |
| Both sides changed the same lines with different intent | Manual merge — read both, synthesize correct result | No heuristic can automate semantic conflicts |
| Conflict is in generated/config files (lock files, heartbeats, timestamps) | Accept whichever is newer, or regenerate | These files have no semantic merge value |
| MEMORY/ or LOGS/ files conflict | Accept local — these are session-specific | Remote memory state is from a different session |

**c. Mark resolved and continue**

```bash
git add <resolved-file>
git rebase --continue
```

Repeat for each commit that has conflicts.

### 4. Verify Resolution

```bash
git log --oneline -10          # confirm commit history looks clean
git diff origin/main --stat    # confirm only expected files differ
python -m pytest tests/ -x --tb=short 2>&1 | tail -20   # if tests exist
```

### 5. Push

```bash
git push origin main
```

Should now succeed as fast-forward. If rejected again, re-fetch — another commit may have landed.

### 6. Report

Append to `/ship` report:

```
## Conflict Resolution
- Remote divergence: <N> commits from PR #<number>
- Conflicts: <list of files>
- Strategy: <per-file strategy used>
- Verification: tests passed / diff reviewed
- Push: success (fast-forward)
```

## Error Handling

| Error | Recovery |
|-------|----------|
| `rebase --continue` fails with new conflicts | Resolve new conflict, `git add`, `git rebase --continue` again |
| Rebase produces broken code (tests fail) | `git rebase --abort`, fall back to `git merge origin/main` |
| Runner blocks `git rebase` | Use `git pull --rebase origin main` instead |
| Push still rejected after rebase | `git fetch origin && git rebase origin/main` again |
| Conflict in a file you didn't modify | Accept remote: `git checkout --theirs <file> && git add <file>` |

## Anti-Patterns

- **Never force-push** to resolve divergence
- **Never blindly `git checkout --ours .`** — discards all remote changes
- **Never skip conflict markers** — `<<<<<<<` in a committed file will break the codebase
- **Don't rebase with uncommitted changes** — stash first

## Output Contract

```
## CONTRACT
conflict_files:
  - <file>: <ours|theirs|manual>
resolution_strategy_per_file:
  - <file>: <one-line explanation>
verification: <tests passed | diff reviewed | N/A>
final_push_status: <success | failed (reason)>
```
