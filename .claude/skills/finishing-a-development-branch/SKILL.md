---
name: finishing-a-development-branch
description: "Use when implementation is complete, tests pass, and you need to decide how to integrate the work. Runs pre-ship gates (tests, clean tree) then presents merge/PR/keep/discard options. Invoke on 'finish this branch', 'wrap this up', 'are we ready to ship', or before /ship on a feature branch."
source:
  upstream: obra/superpowers
  tag: v5.0.7
  commit: 1f20bef3f59b85ad7b52718f822e37c4478a3ff5
  path: skills/finishing-a-development-branch/SKILL.md
  license: MIT
---

# Finishing a Development Branch

## Overview

Guide completion of development work by running pre-ship gates, presenting clear options, and handling the chosen workflow.

**Core principle:** verify tests → present options → execute choice → clean up.

**Announce at start:** "I'm using the finishing-a-development-branch skill to complete this work."

> **NovaCore adaptations (vendored from Superpowers v5.0.7):**
> - This skill is a **pre-`/ship` gate**. `/ship` handles the checkpoint → commit → push mechanics. This skill answers "should we ship at all, and how?"
> - Option 2 (Ship + PR) executes the `/ship` contract (`.claude/commands/ship.md` steps 1-3) **inline** using an announce-and-continue pattern, then owns `gh pr create` with a plan-tracker-aware PR draft. See Option 2 below for the full flow.
> - Upstream references to `subagent-driven-development` and `executing-plans` as callers are replaced with NovaCore's `implementation-team`.

## The Process

### Step 1: Verify Tests

Before presenting options, verify tests pass:

```bash
pytest               # NovaCore default
# or project-appropriate: npm test, cargo test, go test ./...
```

**If tests fail:**

```
Tests failing (<N> failures). Must fix before completing:

[Show failures]

Cannot proceed with merge/PR until tests pass.
```

Stop. Don't proceed to Step 2.

**If tests pass:** continue to Step 2.

### Step 2: Determine Base Branch

```bash
git merge-base HEAD main 2>/dev/null || git merge-base HEAD master 2>/dev/null
```

Or ask: "This branch split from main — is that correct?"

### Step 3: Present Options

Present exactly these 4 options:

```
Implementation complete. What would you like to do?

1. Merge back to <base-branch> locally
2. Push and create a Pull Request (runs /ship)
3. Keep the branch as-is (I'll handle it later)
4. Discard this work

Which option?
```

Don't add explanation — keep options concise.

### Step 4: Execute Choice

#### Option 1: Merge Locally

```bash
git checkout <base-branch>
git pull
git merge <feature-branch>
pytest   # or project test command
git branch -d <feature-branch>
```

Then: cleanup worktree (Step 5).

#### Option 2: Ship + PR (push via `/ship` contract, then create PR)

> **Delegation model:** the `/ship` slash command cannot be invoked from inside a skill. Instead, this subsection executes the `/ship` contract inline — the canonical specification lives in `.claude/commands/ship.md` and this skill follows it. Any change to `ship.md` step semantics must be reflected here in the same commit.

**Announce:**

> "→ Running /ship (checkpoint → commit → push), then creating PR."

**Execute the `/ship` contract, steps 1-3:**

1. **Fusion Memory checkpoint** — follow `.claude/commands/ship.md` Step 1 verbatim (get last checkpoint → recent events → compose summary → `create_checkpoint` with `session-YYYY-MM-DD[-N]` id → verify).
2. **Git commit** — follow `.claude/commands/ship.md` Step 2 (review status/diff, stage specific paths, HEREDOC commit message with Co-Authored-By, verify). **Skip this step entirely if `git status --porcelain` is empty.**
3. **Git push** — follow `.claude/commands/ship.md` Step 3. **On rejection (stderr contains `rejected` or `non-fast-forward`), see "Error handling" below.** **Skip this step only if the branch already exists on origin and has no unpushed commits** (`git rev-parse --verify origin/<branch>` succeeds AND `git rev-list --count origin/<branch>..HEAD` returns 0). On a brand-new local branch not yet on origin, always push.

**Build the PR draft:**

*Title — fallback chain, first hit wins:*
1. Linked plan title, if `plan-tracker` returns a plan whose branch metadata matches the current branch. Format: `<plan-type>: <plan title>`.
2. Last commit subject, as-is.
3. Branch name, humanized (kebab-case → spaces, first letter capitalized).

Cap the title at 70 characters. Truncate with ellipsis on overflow.

*Body — two templates:*

- **Template A (plan-tracker linked).** Sections:
  - `## Summary` — plan's summary field (first paragraph).
  - `## Changes` — `git log --oneline <base>..HEAD` reformatted as bullets.
  - `## Test Plan` — plan's verification steps if present; else the default checklist (`- [ ] pytest passes`, `- [ ] manual smoke`).
- **Template B (no plan linkage).** Sections:
  - `## Summary` — group commits in `<base>..HEAD` by conventional-commit prefix (`feat` / `fix` / `refactor` / `chore` / `docs` / `test` / other). Emit one bullet per distinct prefix, joining subjects with `; `. Cap at 3 bullets; priority `feat` → `fix` → everything else.
  - `## Test Plan` — default checklist.

Both templates end with: `Generated by finishing-a-development-branch skill. Edit before merging if needed.`

**Base branch:** reuse the value computed in Step 2 of this skill. Do not recompute.

**Plan-linkage lookup:** best-effort. If `plan-tracker` errors or takes more than ~3 seconds, silently fall through to Template B. Plan-tracker must never block shipping.

**Empty-body fallback:** if every source fails, emit `_No description — see commits on branch._` — never an empty string.

**Draft-confirm loop:**

Render title + body in a single fenced block, then prompt:

> "Ship this PR, or edit?"

- **`ship` / `yes` / `y` / `ok`** → run `gh pr create --title "<title>" --body "<body>"`.
- **edit instructions** → apply the dictated changes, re-render the draft, ask again. Loop.
- **`cancel` / `abort`** → stop. Report remote state (branch pushed, no PR created).

No free-form prose editor — for prose-length edits, cancel here and run `gh pr create` manually against the pushed branch.

**Error handling:**

| Mode | Detection | Action |
|---|---|---|
| Push rejected (diverged remote) | `git push` exits non-zero with `rejected` / `non-fast-forward` | Invoke `ship-rebase-conflict-resolution` via the Skill tool. On return, retry `git push` exactly once. If the retry fails, fatal: report post-rebase state (local branch, remote sha, rebase outcome), **skip PR draft entirely**, stop. |
| Pre-commit hook fails | `git commit` exits non-zero with hook output | Surface hook output verbatim. Report "Commit blocked by pre-commit hook. Fix the issue and re-run the finishing skill." Stop. Never `--no-verify`. Never `--amend`. |
| `gh pr create` fails | `gh` exits non-zero after successful push | Capture stderr. Report both states: `Push: succeeded → <branch> on origin (<sha>)` / `PR: FAILED — <stderr>`. Suggest manual retry (`gh pr create` or re-run skill). No rollback of the push. |
| Clean tree, nothing to commit / push | `git status --porcelain` empty and `git rev-list --count origin/<branch>..HEAD == 0` | Skip checkpoint, commit, push — jump directly to PR draft against whatever is on the remote. |

**Cross-cutting invariant:** any partial-ship state (push succeeded but PR failed; rebase succeeded but retry push failed) must be reported as ground truth before stopping. No optimistic messages.

**Final report on success:**

```
## Shipped
- Branch: <branch> pushed to origin (<sha>)
- PR:     #<N> <url>
- Checkpoint: <session_id> (seq: <last_event_seq>)
```

Then: cleanup worktree (Step 5). Option 2 **keeps** the worktree (matches existing convention; PR follow-up commits have somewhere to land).

#### Option 3: Keep As-Is

Report: "Keeping branch <name>. Worktree preserved at <path>."

Don't cleanup worktree.

#### Option 4: Discard

**Confirm first:**

```
This will permanently delete:
- Branch <name>
- All commits: <commit-list>
- Worktree at <path>

Type 'discard' to confirm.
```

Wait for exact confirmation.

If confirmed:

```bash
git checkout <base-branch>
git branch -D <feature-branch>
```

Then: cleanup worktree (Step 5).

### Step 5: Cleanup Worktree

**For Options 1, 4:**

Check if in a worktree:

```bash
git worktree list | grep $(git branch --show-current)
```

If yes:

```bash
git worktree remove <worktree-path>
```

**For Options 2, 3:** keep the worktree.

## Quick Reference

| Option | Merge | Push | Keep Worktree | Cleanup Branch |
|--------|-------|------|---------------|----------------|
| 1. Merge locally | ✓ | - | - | ✓ |
| 2. Ship + PR | - | ✓ | ✓ | - |
| 3. Keep as-is | - | - | ✓ | - |
| 4. Discard | - | - | - | ✓ (force) |

## Common Mistakes

**Skipping test verification**
- **Problem:** merge broken code, create failing PR.
- **Fix:** always verify tests before offering options.

**Open-ended questions**
- **Problem:** "what should I do next?" → ambiguous.
- **Fix:** present exactly 4 structured options.

**Automatic worktree cleanup**
- **Problem:** remove worktree when it might still be needed (Options 2, 3).
- **Fix:** only cleanup for Options 1 and 4.

**No confirmation for discard**
- **Problem:** accidentally delete work.
- **Fix:** require typed "discard" confirmation.

## Red Flags

**Never:**
- Proceed with failing tests
- Merge without verifying tests on the result
- Delete work without confirmation
- Force-push without explicit request

**Always:**
- Verify tests before offering options
- Present exactly 4 options
- Get typed confirmation for Option 4
- Clean up worktree for Options 1 & 4 only

## Integration

**Called by:**
- `implementation-team` — after all plan tasks complete

**Pairs with:**
- `using-git-worktrees` — cleans up the worktree created by that skill
- `/ship` — Option 2 executes the `/ship` contract inline (checkpoint → commit → push), then owns `gh pr create`
