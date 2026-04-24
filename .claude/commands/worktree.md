# Worktree

Invoke the `using-git-worktrees` skill to create an isolated git worktree for a feature branch, with project setup and a clean test baseline.

## Usage

```
/worktree <branch-name>
```

## What it does

1. **Directory selection:** checks for an existing `.worktrees/` (preferred) or `worktrees/` directory → falls back to `CLAUDE.md` preference → finally asks the operator
2. **Safety check:** verifies the worktree directory is git-ignored; if not, adds the `.gitignore` entry and commits
3. **Create:** `git worktree add <path> -b <branch-name>`
4. **Setup:** auto-detects project type (`pyproject.toml`, `requirements.txt`, `package.json`, `Cargo.toml`, `go.mod`) and runs the appropriate install
5. **Baseline:** runs the project test suite (`pytest` by default) to confirm the worktree starts clean

## Pairs with

- `/ship` — commit + push when work is complete
- `finishing-a-development-branch` — decides how to integrate the work (merge/PR/keep/discard) before `/ship`

## See also

- `.claude/skills/using-git-worktrees/SKILL.md` — full skill contract
