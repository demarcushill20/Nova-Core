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
> - Option 2 (push + PR) documents `/ship` as the preferred path. Programmatic auto-delegation (this skill invoking `/ship` directly) is **deferred** (tracked in `.claude/skills/_vendored/SUPERPOWERS.md`) — for now, the operator triggers `/ship` manually after this skill's gates pass.
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

#### Option 2: Push and Create PR

> **NovaCore wiring:** prefer handing off to `/ship`, which handles checkpoint → commit → push in a standardized flow. Only drop to raw `git push` + `gh pr create` when `/ship` is unavailable.

```bash
# Standard path:
# → invoke /ship

# Fallback:
git push -u origin <feature-branch>
gh pr create --title "<title>" --body "$(cat <<'EOF'
## Summary
<2-3 bullets of what changed>

## Test Plan
- [ ] <verification steps>
EOF
)"
```

Then: cleanup worktree (Step 5).

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

**For Options 1, 2, 4:**

Check if in a worktree:

```bash
git worktree list | grep $(git branch --show-current)
```

If yes:

```bash
git worktree remove <worktree-path>
```

**For Option 3:** keep the worktree.

## Quick Reference

| Option | Merge | Push | Keep Worktree | Cleanup Branch |
|--------|-------|------|---------------|----------------|
| 1. Merge locally | ✓ | - | - | ✓ |
| 2. Create PR (/ship) | - | ✓ | ✓ | - |
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
- `/ship` — the actual push + PR mechanic for Option 2 (NovaCore wiring)
