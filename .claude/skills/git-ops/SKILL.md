---
name: git-ops
description: "Perform safe Git operations while respecting NovaCore git safety rules, deny patterns, and audit discipline."
activation:
  keywords:
    - git
    - commit
    - branch
    - merge
    - push
    - pull
    - stage
    - checkout
    - diff
    - log
    - stash
    - tag
  when:
    - Git status, diff, log, branch, commit, or push requested
    - Repository inspection required
    - Changes need committing under safety constraints
tool_doctrine:
  repo:
    workflow:
      - prefer_status_diff_first
      - no_force_push
      - no_rebase
      - no_reset_hard
      - no_clean_fd
output_contract:
  required:
    - summary
    - git_commands_executed
    - verification
    - confidence
---

# When To Use

- Reviewing repository state (status, diff, log, show).
- Committing completed work or checkpoints.
- Creating, switching, or listing branches.
- Pushing committed changes to the remote.
- Tagging releases or stashing in-progress work.

# Workflow

1. **Inspect first** — always run `git status` and `git diff` before making changes. Understand current state before acting.
2. **Stage deliberately** — use `git add <specific-paths>`, never `git add -A` blindly. Exclude secrets and generated files.
3. **Commit clearly** — write a concise message following `<type>: <summary>` convention.
4. **Push safely** — only after confirming the commit is correct. Never force-push.
5. **Log** — record the operation in the audit trail.

For forbidden operations, commit message discipline, and divergence handling, see `reference.md`.

# Tool Usage Rules

- All git operations are scoped to `~/nova-core`.
- **Allowed subcommands**: `status`, `diff`, `log`, `add`, `commit`, `branch`, `checkout`, `show`, `switch`, `restore`, `fetch`, `pull`, `merge`, `tag`, `rev-parse`, `stash`, `remote`.
- **Denied by runner**: `--force`, `reset --hard`, `clean -fd`, `rebase`, `filter-branch`, `--force-with-lease`, `merge --strategy=ours`.
- Never commit secrets, credentials, `.env` files, or token values.
- Do not amend published commits.
- Commit messages must follow `<type>: <summary>` — types include `feat`, `fix`, `docs`, `test`, `refactor`, `chore`.

# Verification

After every git operation:

1. Run `git status` to confirm the working tree is in the expected state.
2. After a commit, run `git log -1 --oneline` to verify the commit hash and message.
3. After a push, confirm the remote ref matches the local HEAD.
4. After a branch operation, confirm you are on the expected branch with `git branch --show-current`.

# Failure Handling

- **Exit code 126** (blocked by runner): the git command matched a deny pattern. Do not retry; use an allowed alternative.
- **Merge conflict**: do not use `--force` or `--ours` to resolve. Report the conflict and let the user decide.
- **Diverged branches**: use `git pull` (merge strategy) or `git fetch` + manual resolution. Never `reset --hard` to discard local work.
- **Detached HEAD**: report the state clearly. Use `git switch <branch>` to reattach.
- **Push rejected** (non-fast-forward): fetch and merge first. Never force-push.
- **Uncommitted changes blocking checkout**: stash first with `git stash`, switch, then `git stash pop`.

# Output Contract

Every git-ops execution must end with a machine-checkable contract:

```
## CONTRACT
summary: <one-line description of what was done>
git_commands_executed:
  - <command> (exit <code>)
verification: <how correctness was confirmed>
confidence: <high | medium | low>
```

See `examples.md` for concrete instances.
