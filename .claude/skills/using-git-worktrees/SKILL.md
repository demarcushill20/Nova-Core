---
name: using-git-worktrees
description: "Use when starting feature work that needs isolation from the current workspace or before executing an implementation plan. Creates an isolated git worktree with systematic directory selection, ignore-verification, dependency install, and clean test baseline. Invoke on 'worktree this', 'isolate this work', or before implementation-team picks up a risky plan."
source:
  upstream: obra/superpowers
  tag: v5.0.7
  commit: 1f20bef3f59b85ad7b52718f822e37c4478a3ff5
  path: skills/using-git-worktrees/SKILL.md
  license: MIT
---

# Using Git Worktrees

## Overview

Git worktrees create isolated workspaces sharing the same repository, allowing work on multiple branches simultaneously without switching.

**Core principle:** systematic directory selection + safety verification = reliable isolation.

**Announce at start:** "I'm using the using-git-worktrees skill to set up an isolated workspace."

> **NovaCore adaptations (vendored from Superpowers v5.0.7):**
> - Default directory preference for NovaCore repos is `.worktrees/` (hidden, project-local). If a CLAUDE.md preference exists, it wins.
> - The "fix broken things immediately" rule (add missing `.gitignore` entry + commit) is preserved as a generic principle.

## Directory Selection Process

Follow this priority order:

### 1. Check Existing Directories

```bash
ls -d .worktrees 2>/dev/null     # Preferred (hidden)
ls -d worktrees 2>/dev/null      # Alternative
```

**If found:** use that directory. If both exist, `.worktrees` wins.

### 2. Check CLAUDE.md

```bash
grep -i "worktree.*director" CLAUDE.md 2>/dev/null
```

**If a preference is specified:** use it without asking.

### 3. Ask the Operator

If no directory exists and no CLAUDE.md preference:

```
No worktree directory found. Where should I create worktrees?

1. .worktrees/ (project-local, hidden)
2. ~/.config/nova-core/worktrees/<project-name>/ (global location)

Which would you prefer?
```

## Safety Verification

### For Project-Local Directories (.worktrees or worktrees)

**Verify directory is ignored before creating the worktree:**

```bash
git check-ignore -q .worktrees 2>/dev/null || git check-ignore -q worktrees 2>/dev/null
```

**If NOT ignored:**

Fix broken things immediately:
1. Add appropriate line to `.gitignore`.
2. Commit the change.
3. Proceed with worktree creation.

**Why critical:** prevents accidentally committing worktree contents to the repository.

### For Global Directory (`~/.config/nova-core/worktrees`)

No `.gitignore` verification needed — outside the project entirely.

## Creation Steps

### 1. Detect Project Name

```bash
project=$(basename "$(git rev-parse --show-toplevel)")
```

### 2. Create Worktree

```bash
case $LOCATION in
  .worktrees|worktrees)
    path="$LOCATION/$BRANCH_NAME"
    ;;
  ~/.config/nova-core/worktrees/*)
    path="~/.config/nova-core/worktrees/$project/$BRANCH_NAME"
    ;;
esac

git worktree add "$path" -b "$BRANCH_NAME"
cd "$path"
```

### 3. Run Project Setup

Auto-detect and run setup:

```bash
# Python (NovaCore default)
if [ -f requirements.txt ]; then pip install -r requirements.txt; fi
if [ -f pyproject.toml ]; then poetry install 2>/dev/null || pip install -e .; fi

# Node
if [ -f package.json ]; then npm install; fi

# Rust
if [ -f Cargo.toml ]; then cargo build; fi

# Go
if [ -f go.mod ]; then go mod download; fi
```

### 4. Verify Clean Baseline

Run tests to ensure the worktree starts clean:

```bash
pytest               # NovaCore default
# or project-appropriate: npm test, cargo test, go test ./...
```

**If tests fail:** report failures, ask whether to proceed or investigate.

**If tests pass:** report ready.

### 5. Report Location

```
Worktree ready at <full-path>
Tests passing (<N> tests, 0 failures)
Ready to implement <feature-name>
```

## Quick Reference

| Situation | Action |
|-----------|--------|
| `.worktrees/` exists | Use it (verify ignored) |
| `worktrees/` exists | Use it (verify ignored) |
| Both exist | Use `.worktrees/` |
| Neither exists | Check CLAUDE.md → ask operator |
| Directory not ignored | Add to `.gitignore` + commit |
| Tests fail during baseline | Report failures + ask |
| No `pyproject.toml` / `package.json` / `Cargo.toml` | Skip dependency install |

## Common Mistakes

### Skipping ignore verification

- **Problem:** worktree contents get tracked, polluting git status.
- **Fix:** always use `git check-ignore` before creating a project-local worktree.

### Assuming directory location

- **Problem:** creates inconsistency, violates project conventions.
- **Fix:** follow priority: existing > CLAUDE.md > ask.

### Proceeding with failing tests

- **Problem:** can't distinguish new bugs from pre-existing issues.
- **Fix:** report failures; get explicit permission to proceed.

### Hardcoding setup commands

- **Problem:** breaks on projects using different tools.
- **Fix:** auto-detect from project files.

## Red Flags

**Never:**
- Create worktree without verifying it's ignored (project-local)
- Skip baseline test verification
- Proceed with failing tests without asking
- Assume directory location when ambiguous
- Skip CLAUDE.md check

**Always:**
- Follow directory priority: existing > CLAUDE.md > ask
- Verify directory is ignored for project-local
- Auto-detect and run project setup
- Verify clean test baseline

## Integration

**Called by:**
- `brainstorming` — when a design is approved and implementation follows
- `implementation-team` — before executing any plan task
- Any skill needing an isolated workspace

**Pairs with:**
- `finishing-a-development-branch` — cleanup after work complete
