# git-ops Reference

Detailed rules, philosophy, and operational guidelines for the git-ops skill.

## Git as Audit Trail

In NovaCore, git is not just version control — it is the primary audit trail. Every meaningful action produces a commit. This means:

- Commits must be atomic and self-describing.
- History must never be rewritten (`rebase`, `filter-branch`, `commit --amend` on published commits).
- The log must be readable as a chronological record of what the agent did and why.
- Force operations destroy audit history and are forbidden by default.

## Forbidden Operations

The runner enforces git safety through an allowlist of subcommands and a denylist of patterns. Forbidden operations return exit code 126.

| Operation | Pattern | Reason |
|---|---|---|
| Force push | `--force`, `-f` | Destroys remote history |
| Force with lease | `--force-with-lease` | Still overwrites remote |
| Hard reset | `reset --hard` | Discards uncommitted work |
| Destructive clean | `clean -fd`, `clean -fx` | Deletes untracked files |
| Rebase | `rebase` | Rewrites commit history |
| Filter branch | `filter-branch` | Rewrites entire history |
| Merge strategy ours | `merge --strategy=ours` | Silently discards changes |

Any git subcommand not in the allowlist is also blocked.

### Allowlist

```
status, diff, log, add, commit, branch, checkout, show, switch,
restore, fetch, pull, merge, tag, rev-parse, stash, remote
```

## Safe Operation Sequence

The standard sequence for committing work:

```
git status          # 1. Understand current state
git diff            # 2. Review what changed
git add <paths>     # 3. Stage specific files
git commit -m "..."  # 4. Commit with clear message
git push origin main # 5. Push (only if appropriate)
```

Never skip steps 1–2. Staging blindly leads to accidental commits of secrets or generated files.

## Commit Message Discipline

Format: `<type>: <summary>`

| Type | Use When |
|---|---|
| `feat` | New feature or capability |
| `fix` | Bug fix |
| `docs` | Documentation changes |
| `test` | Adding or updating tests |
| `refactor` | Code restructuring without behavior change |
| `chore` | Maintenance, config, or tooling |

Rules:
- Summary line under 72 characters.
- Use imperative mood ("add feature" not "added feature").
- Body (optional) separated by blank line, explains *why* not *what*.
- Never include secrets or credentials in commit messages.

## Branch Hygiene

- `main` is the primary branch. All work lands on `main` unless the user requests otherwise.
- Feature branches: `feature/<name>` — short-lived, merged back promptly.
- Use `git switch -c <branch>` to create new branches (preferred over `checkout -b`).
- Delete merged branches to keep the ref list clean: `git branch -d <branch>` (lowercase `-d`, not `-D`).
- Never delete `main`.

## Divergence Handling (Non-Destructive)

When local and remote have diverged:

1. **Fetch first**: `git fetch origin` to update remote refs without modifying the working tree.
2. **Inspect**: `git log --oneline HEAD..origin/main` to see what the remote has that local does not.
3. **Merge**: `git merge origin/main` to integrate remote changes. This preserves both histories.
4. **Resolve conflicts manually** if they arise — never use `--force` or `--ours` to skip resolution.
5. **Never** `git reset --hard origin/main` — this discards local commits.

## Files to Never Commit

- `.env`, `*.env`, `.env.*` — environment secrets
- `credentials.json`, `token.json` — API credentials
- `STATE/tool_audit.jsonl` — runtime state (in `.gitignore`)
- `LOGS/` — ephemeral logs (in `.gitignore`)
- `__pycache__/`, `.pyc` — Python bytecode (in `.gitignore`)
- Any file containing `ghp_`, `github_pat_`, `xoxb-`, or other token prefixes
