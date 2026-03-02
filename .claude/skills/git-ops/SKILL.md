---
name: git-ops
description: "Manage git operations within nova-core: init, stage, commit, branch, diff, log, and status."
---

# git-ops

## When to use

- Initializing a git repository in `~/nova-core`.
- Committing completed work or checkpoints.
- Creating or switching branches for experimental work.
- Reviewing diffs or history.

## Rules / Safety

- All git operations scoped to `~/nova-core`.
- Never force-push without explicit user instruction.
- Never run `git reset --hard` or `git clean -fd` without explicit user instruction.
- Commit messages must be concise and descriptive.
- Do not commit secrets, credentials, or `.env` files.
- Do not amend published commits.

## Workflow

1. Run `git status` to assess current state.
2. Stage relevant files with `git add <paths>` (avoid `git add -A` blindly).
3. Commit with a clear message: `git commit -m "<type>: <summary>"`.
4. For branches: `git checkout -b <branch-name>` from current HEAD.
5. Log the operation to `LOGS/git.log`.

## Output format

```
[git-ops] STATUS: <clean | N files changed>
[git-ops] COMMIT: <short-hash> <message>
[git-ops] BRANCH: created <branch-name> from <base>
```
