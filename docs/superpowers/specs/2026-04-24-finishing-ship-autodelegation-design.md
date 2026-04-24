---
spec_id: 2026-04-24-finishing-ship-autodelegation
status: draft
owners: nova-core
related_skill: .claude/skills/finishing-a-development-branch/SKILL.md
related_command: .claude/commands/ship.md
related_skill_dep: .claude/skills/ship-rebase-conflict-resolution/SKILL.md
phase2_wiring: .claude/skills/_vendored/SUPERPOWERS.md
---

# Finishing-Branch → /ship Auto-Delegation

## 1. Context and problem

The `finishing-a-development-branch` skill (vendored from Superpowers v5.0.7) presents a 4-option menu after pre-ship gates pass. Option 2 is labeled "Push and create a Pull Request (runs /ship)". Today the skill body documents `/ship` as the preferred path with raw `git push` + `gh pr create` as a fallback; programmatic delegation was marked **deferred** in `_vendored/SUPERPOWERS.md` because the harness has no clean way for a skill to invoke a slash command.

A second gap: `/ship` itself stops at **push** — it does not create a PR. So "Option 2 runs /ship" was only half true.

This spec closes both gaps.

## 2. Design decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | Option 2 = push **and** PR via a **delegation chain**: the finishing skill invokes the `/ship` contract inline, then owns `gh pr create` itself. | Keeps `/ship` a stable narrow primitive still used at end-of-session and after milestones. Puts PR-shaped finish logic in the skill the operator already opted into with that intent. |
| D2 | Delegation mechanism = **announce-and-continue, inline execution of `.claude/commands/ship.md`**. | No nested slash-command plumbing (unverified harness feature). `ship.md` stays canonical — the finishing skill references it by path, does not copy step bodies. Drift is visible. |
| D3 | PR draft = **hybrid**: assemble from plan-tracker (if linked) → commits-since-base → branch name; show draft; single-round operator confirm. | Push is YOLO; externally-visible PR metadata deserves a 5-second read-over. Catches stale plan summaries and terse commit messages without adding ceremony. |
| D4 | Failure handling = **reuse existing infrastructure**: rebase → `ship-rebase-conflict-resolution`; pre-commit hook fail → stop; `gh pr create` fail → report both states; clean tree → skip checkpoint+commit. | Matches CLAUDE.md Git Safety Protocol. No new rollback machinery. Partial-ship states always reported as ground truth. |

## 3. Interaction flow

```
Operator picks Option 2
  │
  ▼
Announce: "→ Running /ship (checkpoint → commit → push), then creating PR"
  │
  ▼
Execute the steps in .claude/commands/ship.md (1–3) inline:
    1. Fusion Memory checkpoint
    2. git commit (skip if no changes; HEREDOC commit message)
    3. git push
         └─ if push rejected → hand off to ship-rebase-conflict-resolution,
            then retry push once; resume at step 4
  │
  ▼
Build PR draft (§5):
    title ← plan-tracker linkage → last-commit subject → branch name
    body  ← Template A (plan-linked) or Template B (commits-based)
  │
  ▼
Show draft in a fenced block, prompt: "Ship this, or edit?"
  │
  ├─ "ship" / "yes" / "y" / "ok"  → gh pr create
  ├─ edit instructions            → apply, re-render, loop
  └─ "cancel" / "abort"           → stop; report remote state (branch pushed, no PR)
  │
  ▼
Report: pushed <branch>, PR #<N> <url>, checkpoint <id>
```

**Key properties.** Single linear path. Two operator interaction points: picking Option 2 (existing) and confirming the PR draft (new, one round-trip). Worktree stays put — matches existing Option 2 convention so PR-follow-up commits have somewhere to land.

## 4. Component changes

Three files touched. No new files. No new slash command. No new skill.

### 4.1 `.claude/skills/finishing-a-development-branch/SKILL.md`

- **Step 3 (option menu)**: no change; wording already says "runs /ship".
- **Step 4 / Option 2**: replace the current fallback-first block with the linear flow in §3. Option 2 body becomes: announcement line → reference to `.claude/commands/ship.md` steps 1–3 (canonical spec; no copy-paste) → PR draft rules (§5) → draft-confirm loop → `gh pr create` → final report format.
- **Step 5 (cleanup)**: no change. Option 2 preserves the worktree.
- **Quick Reference table**: relabel Option 2 from `Create PR (/ship)` to `Ship + PR`.

### 4.2 `.claude/commands/ship.md`

No behavioral change. Add one sentence under "Important": `This contract is also followed inline by the finishing-a-development-branch skill (Option 2).` Forward reference only — makes drift visible to anyone editing either file.

### 4.3 `.claude/skills/_vendored/SUPERPOWERS.md`

Flip the `finishing-a-development-branch` row in the "Phase 2 wiring" table from ⏳ to ✅. Describe the delegation model (announce-and-continue, inline execution of `ship.md`, skill owns `gh pr create`). Append a line under "Deviations → `finishing-a-development-branch`" recording the Option 2 expansion as a NovaCore adaptation beyond upstream v5.0.7.

### 4.4 Untouched on purpose

- `ship-rebase-conflict-resolution/SKILL.md` — invoked as-is via the Skill tool.
- `plan-tracker` — read-only usage (optional PR-body lookup), no writes.

## 5. PR draft generation

### 5.1 Title (fallback chain, first hit wins)

1. **Linked plan's title** — if `plan-tracker` returns a plan whose branch metadata matches the current branch. Format: `<plan-type>: <plan title>`.
2. **Last commit subject** — as-is.
3. **Branch name humanized** — kebab-case → spaces, first letter capitalized. Last-resort.

Cap at 70 chars (matches CLAUDE.md PR guidance). Truncate with ellipsis on overflow.

### 5.2 Body

**Template A — plan-tracker linked.**
- `## Summary` — plan's summary field (first paragraph)
- `## Changes` — commits since base branch as bullets (`git log --oneline base..HEAD`)
- `## Test Plan` — plan's verification steps if present; else default checklist (`- [ ] pytest passes`, `- [ ] manual smoke`)

**Template B — no plan linkage.**
- `## Summary` — group commits in `base..HEAD` by conventional-commit prefix (`feat` / `fix` / `refactor` / `chore` / `docs` / `test` / other). Emit one bullet per distinct prefix, joining the commit subjects with `; `. Cap at 3 bullets total — if there are more distinct prefixes, prioritize `feat` → `fix` → everything else.
- `## Test Plan` — default checklist

Both end with a one-line footer: `Generated by finishing-a-development-branch skill. Edit before merging if needed.`

### 5.3 Draft-confirm loop

Three response classes honored:

- **Confirm:** `ship` / `yes` / `y` / `ok` → `gh pr create` with the draft as-is.
- **Edit:** apply dictated changes, re-render, ask again. Loop.
- **Cancel:** `cancel` / `abort` → stop; push has happened; report remote state.

No free-form editor — if the operator wants prose-length edits, they cancel here and run `gh pr create` manually against the pushed branch.

### 5.4 Implementation notes

- Base branch for `git log base..HEAD` is already computed in the finishing skill's Step 2. Reuse, don't recompute.
- Plan-linkage lookup is **best-effort**: if `plan-tracker` errors or takes more than ~3 seconds, fall through to Template B silently. Plan-tracker must never block shipping.
- Empty-body fallback: never emit empty. If every source fails, body = `_No description — see commits on branch._`.

## 6. Error handling

### 6.1 Push rejected (remote diverged)

Detection: `git push` exits non-zero with `rejected` or `non-fast-forward` in stderr.

Action: announce → invoke `ship-rebase-conflict-resolution` via the Skill tool → on its return, retry `git push` exactly once. If the retry fails, fatal: report the post-rebase state (local branch, remote sha, rebase outcome), **skip PR draft entirely**, stop. Operator resolves manually.

### 6.2 Pre-commit hook fails

Detection: `git commit` exits non-zero with hook output in stderr.

Action: surface hook output verbatim → report "Commit blocked by pre-commit hook. Fix the issue and re-run the finishing skill." → stop. Nothing has been pushed. **Never** `--no-verify`. **Never** `--amend` a prior commit.

### 6.3 `gh pr create` fails

Detection: `gh` exits non-zero after a successful push.

Action: capture stderr → report both states:

```
Push: succeeded → <branch> on origin (<sha>)
PR:   FAILED — <gh stderr>
Retry: `gh pr create` manually, or re-run this skill.
```

No rollback of the push.

### 6.4 No changes to commit

Detection: `git status --porcelain` is empty.

Action: skip checkpoint, skip commit. If `git rev-list --count origin/<branch>..HEAD == 0` also, skip push — jump directly to PR draft against whatever is on the remote. If there are unpushed commits but no staged changes, skip commit only, push + PR normally. Mirrors `/ship`'s existing rule.

### 6.5 Cross-cutting invariant

Any step that leaves the branch in a partially-shipped state **always** reports the ground truth of what is on the remote before stopping. No optimistic messages. Operator needs to know whether re-running is safe or manual cleanup is required.

## 7. Acceptance criteria

### 7.1 Happy path (must be demonstrated before this feature is considered shipped)

Given a feature branch with 1–3 commits, diverged from `main`, clean working tree, with a plan linked in `plan-tracker`, the finishing skill invoked with Option 2 must produce all of:

1. A single announcement line naming the `/ship` handoff.
2. A Fusion Memory checkpoint visible via `mcp__nova-memory__get_last_checkpoint` immediately after.
3. Successful push — branch visible on remote via `git ls-remote origin <branch>`.
4. A PR draft whose title matches the linkage rule in §5.1 and whose body uses Template A.
5. Single-round operator confirm with `ship` → PR created, visible via `gh pr view <N>`.
6. Final report naming branch, PR # and URL, and checkpoint ID.

### 7.2 Edge-case verification matrix (documented, exercised opportunistically)

| Case | Verification trigger |
|---|---|
| A. Remote diverged | When a real divergence is encountered in normal work. Confirm rebase skill is invoked, one retry occurs. |
| B. Pre-commit hook fails | When a real hook failure occurs. Confirm output is verbatim and no `--no-verify`/`--amend` is suggested. |
| C. `gh pr create` fails | Synthesizable only on a throwaway clone (break `GH_TOKEN`). **Never on the main VPS or against the real remote.** Confirm both-states report matches §6.3. |
| D. Clean tree, nothing to commit | Run the skill on a branch already pushed with no local changes. Confirm it jumps to PR draft. |

### 7.3 Bootstrap consideration

This feature will eventually be used to ship itself. To avoid the broken-fix-ships-itself risk, the **first** PR introducing the feature ships via the **existing fallback path** (raw `git push` + `gh pr create`). The **second** feature PR is the first dogfood run of the new Option 2 flow. The implementation plan must reflect this.

## 8. Out of scope

- True programmatic slash-command invocation from inside skills (harness feature, unverified; not required for this feature).
- Extending `/ship` itself with a `--pr` mode flag.
- Plan-tracker writes (read-only usage only).
- Modifying `ship-rebase-conflict-resolution/SKILL.md`.
- Free-form PR body editing inside the draft-confirm loop.
- Visual Companion / HTML preview (out of scope for NovaCore's vendoring).

## 9. Provenance

- Brainstorm session: 2026-04-24 (Claude Opus 4.7).
- Closes Phase 2 wiring item ⏳ for `finishing-a-development-branch` in `.claude/skills/_vendored/SUPERPOWERS.md`.
- Depends on: `.claude/skills/finishing-a-development-branch/SKILL.md` (Superpowers v5.0.7), `.claude/commands/ship.md`, `.claude/skills/ship-rebase-conflict-resolution/SKILL.md`.

## 10. Open risks

- The finishing skill executing `ship.md`'s contract inline is still interpretation by Claude — there is no static check that the two specs stay aligned. The forward-reference sentence in `ship.md` is the only drift guard. Mitigation: any future edit to `ship.md` that changes step semantics must update the finishing skill's Option 2 in the same commit.
- Plan-tracker schema evolution could silently degrade Template A to always-falling-through. Mitigation: Template B is the graceful default; no user-visible failure.
- The harness's behavior when a skill invokes another skill (§6.1 via the Skill tool) is not exercised in vendored skills today. First real rebase in a finishing-skill run is the verification point.
