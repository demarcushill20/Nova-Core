---
name: finishing-a-development-branch
description: "Use when implementation is complete, tests pass, and you need to decide how to integrate the work. Runs pre-ship gates (tests, clean tree) then presents merge/PR/keep options. Invoke on 'finish this branch', 'wrap this up', 'are we ready to ship', or before /ship on a feature branch."
source:
  upstream: obra/superpowers
  tag: v6.2.0
  commit: 3dcbd5c4b48e02263fbf4a3c01e3fe4f81d584d9
  path: skills/finishing-a-development-branch/SKILL.md
  license: MIT
---

# Finishing a Development Branch

## Overview

Guide completion of development work by running pre-ship gates, presenting clear options, and handling the chosen workflow.

**Core principle:** Verify tests → Detect environment → Present options → Execute choice → Clean up.

**Announce at start:** "I'm using the finishing-a-development-branch skill to complete this work."

> **NovaCore adaptations (vendored from Superpowers v6.2.0):**
> - This skill is a **pre-`/ship` gate**. `/ship` handles the checkpoint → commit → push mechanics. This skill answers "should we ship at all, and how?"
> - Option 2 (Ship + PR) executes the `/ship` contract (`.claude/commands/ship.md` steps 1-3) **inline** using an announce-and-continue pattern, then owns `gh pr create` with a plan-tracker-aware PR draft. See Option 2 below for the full flow.
> - **⚠ DEVIATION OVERLAP (v6.2.0 re-pull):** upstream v6.2.0 changed Option 2 to be forge-agnostic ("forge's tooling — its CLI if one is available, or the creation URL most forges print when you push"). NovaCore deliberately keeps `gh pr create` as the standard path because the `/ship` contract requires it. If support for non-GitHub forges becomes relevant, this deviation needs operator review.
> - Upstream references to `subagent-driven-development` and `executing-plans` as callers are replaced with NovaCore's `implementation-team`.
> - v6.2.0 re-pull: adopted Step 2 environment detection (GIT_DIR/GIT_COMMON check + WORKTREE_PATH capture); adopted removal of "Discard" from the main menu (now explicit-request only); adopted provenance-based cleanup in Step 6.

## Step 1: Verify Tests

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

## Step 2: Detect Environment

```bash
GIT_DIR=$(cd "$(git rev-parse --git-dir)" 2>/dev/null && pwd -P)
GIT_COMMON=$(cd "$(git rev-parse --git-common-dir)" 2>/dev/null && pwd -P)
# Capture now, while still inside the workspace — Step 5 changes directory
# before cleanup (Step 6) needs this value
WORKTREE_PATH=$(git rev-parse --show-toplevel)
```

This determines which menu to show and how cleanup works:

| State | Menu | Cleanup |
|-------|------|---------|
| `GIT_DIR == GIT_COMMON` (normal repo) | Standard 3 options | No worktree to clean up |
| `GIT_DIR != GIT_COMMON`, named branch | Standard 3 options | Provenance-based (see Step 6) |
| `GIT_DIR != GIT_COMMON`, detached HEAD | Reduced 2 options (no merge) | Externally managed — leave in place |

## Step 3: Determine Base Branch

The base branch is whatever this work forked from — usually named in the plan, the conversation, or the branch's upstream. If it is not already known, ask: "This branch split from `<your best guess>` — is that correct?" Confirm before merging: merging into the wrong base is expensive to undo.

## Step 4: Present Options

**Normal repo and named-branch worktree — present exactly these 3 options:**

```
Implementation complete. What would you like to do?

1. Merge back to <base-branch> locally
2. Push and create a Pull Request (runs /ship)
3. Keep the branch as-is (I'll handle it later)

Which option?
```

**Detached HEAD — present exactly these 2 options:**

```
Implementation complete. You're on a detached HEAD (externally managed workspace).

1. Push as new branch and create a Pull Request
2. Keep as-is (I'll handle it later)

Which option?
```

Present the menu exactly as written — concise, with every option coming from the list above. Discarding the work happens only in response to the operator explicitly asking for it (see "If the operator asks to discard the work" below). Wait for their answer; the integration decision is theirs.

## Step 5: Execute Choice

### Option 1: Merge Locally

```bash
MAIN_ROOT=$(git -C "$(git rev-parse --git-common-dir)/.." rev-parse --show-toplevel)
cd "$MAIN_ROOT"

git checkout <base-branch>
git pull
git merge <feature-branch>

pytest   # or project test command
```

If tests fail on the merged result: stop, leave the worktree and branch in place, and investigate — nothing has been pushed, so the merge is local and recoverable.

Once the merged result is green: clean up the worktree (Step 6), then delete the branch:

```bash
git branch -d <feature-branch>
```

### Option 2: Ship + PR (push via `/ship` contract, then create PR)

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

**Base branch:** reuse the value computed in Step 3 of this skill. Do not recompute.

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

Then: cleanup worktree (Step 6). Option 2 **keeps** the worktree (PR follow-up commits have somewhere to land).

### Option 3: Keep As-Is

Report: "Keeping branch `<name>`. Worktree preserved at `<path>`."

### If the operator asks to discard the work

This path exists only as a response to an explicit operator request to throw the work away. Confirm first:

```
This will permanently delete:
- Branch <name>
- All commits: <commit-list>
- Worktree at <path>

Type 'discard' to confirm.
```

Wait for that exact confirmation. When it arrives:

```bash
MAIN_ROOT=$(git -C "$(git rev-parse --git-common-dir)/.." rev-parse --show-toplevel)
cd "$MAIN_ROOT"
```

Then clean up the worktree (Step 6) and force-delete the branch:

```bash
git branch -D <feature-branch>
```

## Step 6: Cleanup Workspace

**Runs for Option 1 and confirmed discards.** Options 2 and 3 always preserve the worktree. Both callers have already changed directory to the main repo root — worktree removal must run from outside the worktree — and use the `GIT_DIR`/`GIT_COMMON`/`WORKTREE_PATH` values captured in Step 2, from before that directory change.

**If `GIT_DIR == GIT_COMMON`:** Normal repo, no worktree to clean up. Done.

**If `WORKTREE_PATH` is under `.worktrees/` or `worktrees/`:** Superpowers/NovaCore created this worktree — we own cleanup:

```bash
git worktree remove "$WORKTREE_PATH"
git worktree prune  # Self-healing: clean up any stale registrations
```

**Otherwise:** The host environment owns this workspace — leave it in place. If your platform provides a workspace-exit tool (e.g. `ExitWorktree`), use it.

## Quick Reference

| Option | Merge | Push | Keep Worktree | Cleanup Branch |
|--------|-------|------|---------------|----------------|
| 1. Merge locally | ✓ | - | - | ✓ |
| 2. Ship + PR | - | ✓ | ✓ | - |
| 3. Keep as-is | - | - | ✓ | - |
| Discard (explicit request only) | - | - | - | ✓ (force) |

## Common Mistakes

**Skipping test verification**
- **Problem:** merge broken code, create failing PR.
- **Fix:** always verify tests before offering options.

**Open-ended questions**
- **Problem:** "what should I do next?" → ambiguous.
- **Fix:** present exactly 3 structured options (or 2 for detached HEAD).

**Automatic worktree cleanup**
- **Problem:** remove worktree when it might still be needed (Options 2, 3).
- **Fix:** only cleanup for Option 1 and confirmed discards.

**No confirmation for discard**
- **Problem:** accidentally delete work.
- **Fix:** require typed "discard" confirmation.

## Red Flags

**Never:**
- Proceed with failing tests
- Merge without verifying tests on the result
- Delete work without confirmation
- Force-push without explicit request
- Offer "Discard" as a menu option — wait for the operator to ask

**Always:**
- Verify tests before offering options
- Present exactly 3 options (or 2 for detached HEAD)
- Get typed confirmation for discards
- Clean up worktree for Option 1 and confirmed discards only

## Integration

**Called by:**
- `implementation-team` — after all plan tasks complete

**Pairs with:**
- `using-git-worktrees` — cleans up the worktree created by that skill
- `/ship` — Option 2 executes the `/ship` contract inline (checkpoint → commit → push), then owns `gh pr create`
