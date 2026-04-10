# git-ops Examples

## Example 1: Inspect Repository Safely

**User request:** "Show me what's changed in the repo"

**Workflow:**

1. Run `git status` to see staged, unstaged, and untracked files.
2. Run `git diff` to see unstaged changes.
3. Run `git diff --cached` to see staged changes.
4. Report findings to the user.

**Output Contract:**

```
## CONTRACT
summary: Inspected repository — 3 files modified, 1 untracked
git_commands_executed:
  - git status (exit 0)
  - git diff (exit 0)
  - git diff --cached (exit 0)
verification: All three commands returned exit 0; output reviewed for current state
```

---

## Example 2: Small Commit and Push

**User request:** "Commit the changes to tools/skills.py and push"

**Workflow:**

1. Run `git status` to confirm `tools/skills.py` is modified.
2. Run `git diff tools/skills.py` to review the change.
3. Stage: `git add tools/skills.py`.
4. Commit: `git commit -m "fix: update skill selection heuristics"`.
5. Push: `git push origin main`.
6. Verify: `git log -1 --oneline` to confirm commit.

**Output Contract:**

```
## CONTRACT
summary: Committed and pushed fix to tools/skills.py
git_commands_executed:
  - git status (exit 0)
  - git diff tools/skills.py (exit 0)
  - git add tools/skills.py (exit 0)
  - git commit -m "fix: update skill selection heuristics" (exit 0)
  - git push origin main (exit 0)
  - git log -1 --oneline (exit 0)
verification: git log confirms commit hash matches; push succeeded with no errors
```

---

## Example 3: Blocked Operation — Rebase Denied

**User request:** "Rebase main onto feature branch"

**Workflow:**

1. Runner checks `git rebase main` against deny patterns.
2. Pattern `\brebase\b` matches — **BLOCKED**.
3. Command never executes. Exit code 126 returned.
4. Report: rebase is forbidden because it rewrites history. Suggest `git merge` as a safe alternative.

**Output Contract:**

```
## CONTRACT
summary: Refused git rebase — blocked by runner deny pattern (history rewrite)
git_commands_executed:
  - git rebase main (exit 126, BLOCKED)
verification: Command was never executed; suggested git merge as alternative
```

---

## Example 4: Branch Switch with Uncommitted Changes

**User request:** "Switch to the feature/logging branch"

**Workflow:**

1. Run `git status` — finds uncommitted changes that would be overwritten.
2. Stash changes: `git stash`.
3. Switch branch: `git switch feature/logging`.
4. Confirm: `git branch --show-current` returns `feature/logging`.
5. Inform user their changes are stashed and can be restored with `git stash pop`.

**Output Contract:**

```
## CONTRACT
summary: Stashed uncommitted changes and switched to feature/logging
git_commands_executed:
  - git status (exit 0)
  - git stash (exit 0)
  - git switch feature/logging (exit 0)
  - git branch --show-current (exit 0)
verification: Current branch confirmed as feature/logging; stash list shows saved entry
```

---

## Example 5: Tag a Release

**User request:** "Tag the current commit as v0.2.0"

**Workflow:**

1. Run `git status` to confirm working tree is clean.
2. Run `git log -1 --oneline` to verify HEAD is the intended commit.
3. Create tag: `git tag v0.2.0`.
4. Verify: `git tag -l v0.2.0` confirms the tag exists.
5. Push tag: `git push origin v0.2.0`.

**Output Contract:**

```
## CONTRACT
summary: Tagged current commit as v0.2.0 and pushed tag to remote
git_commands_executed:
  - git status (exit 0)
  - git log -1 --oneline (exit 0)
  - git tag v0.2.0 (exit 0)
  - git tag -l v0.2.0 (exit 0)
  - git push origin v0.2.0 (exit 0)
verification: git tag -l confirms v0.2.0 exists; push succeeded
```
