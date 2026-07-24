# Superpowers — Vendored Provenance

This document records the upstream provenance, cherry-pick decisions, and deviations for the seven skills vendored from the **Superpowers** plugin into NovaCore's `.claude/skills/` tree.

## Upstream

- **Repository:** [obra/superpowers](https://github.com/obra/superpowers)
- **License:** MIT
- **Pinned tag:** `v6.2.0`
- **Pinned tag SHA:** `0e5cc50e782429b95f933e46443898435b8b37a8`
- **Pinned commit SHA:** `3dcbd5c4b48e02263fbf4a3c01e3fe4f81d584d9`
- **Release date:** 2026-07-23
- **Vendored into NovaCore:** 2026-04-24
- **Last re-pulled:** 2026-07-24

## Install approach: vendor, don't install

NovaCore is a governed runtime. Skills must be reviewable in-tree, pinned to a known upstream version, and allowed to diverge (softer language, Fusion Memory wiring, TASKS/OUTPUT integration). The alternative — `/plugin install superpowers@claude-plugins-official` — would auto-update upstream, risk conflicts with NovaCore's own skills, and import upstream's "mandatory workflow" posture which clashes with NovaCore's path-choice autonomy policy.

Re-pull cadence: **quarterly**, or sooner if upstream ships a security patch or a skill NovaCore wants.

## Cherry-pick decisions (applied 2026-04-24)

Of the 14 skills in upstream `skills/` at v5.0.7, NovaCore vendored **7** and deliberately skipped **7**.

### Vendored (7)

| Upstream skill | NovaCore path | Notes |
|---|---|---|
| `brainstorming` | `.claude/skills/brainstorming/SKILL.md` | Visual Companion stripped (out of scope); text-only brainstorming |
| `test-driven-development` | `.claude/skills/test-driven-development/SKILL.md` | Iron Law preserved; Python/pytest-default examples |
| `writing-plans` | `.claude/skills/writing-plans/SKILL.md` | Execution handoff retargeted to `implementation-team` |
| `systematic-debugging` | `.claude/skills/systematic-debugging/SKILL.md` | 4-phase + 3-attempt escalation preserved; Phase 2 will wire to Critic agent + `memory-store` |
| `using-git-worktrees` | `.claude/skills/using-git-worktrees/SKILL.md` | Default `.worktrees/` directory; pytest-default baseline check |
| `finishing-a-development-branch` | `.claude/skills/finishing-a-development-branch/SKILL.md` | Option 2 delegates to `/ship` (Phase 2 wires this fully) |
| `dispatching-parallel-agents` | `.claude/skills/dispatching-parallel-agents/SKILL.md` | Cost-optimized model-selection guidance added (v5 pattern) |

### Skipped (7) with rationale

| Upstream skill | Why skipped |
|---|---|
| `executing-plans` | Thin alias for plan execution. NovaCore's `implementation-team` is the canonical orchestrator — no need for a duplicate entry point. |
| `subagent-driven-development` | Redundant with `implementation-team`. Upstream itself removed a subagent-review loop in v5.0.6 (doubled execution time, no quality gain) — NovaCore's dual-model setup (`dual-code-review`) already covers the strong version. |
| `requesting-code-review` | NovaCore's `dual-code-review` (Codex GPT-5.4 + Opus 4.6 in parallel) is strictly stronger. Upstream's own community proposal (issue #730) asks for exactly this — NovaCore already has it. |
| `receiving-code-review` | Same reason as `requesting-code-review`. |
| `writing-skills` | Redundant with NovaCore's `skill-creator`. |
| `using-superpowers` | Meta-dispatcher skill. Replaced by a much thinner `.claude/commands/superpowers.md` intro command (Phase 3). |
| `verification-before-completion` | Content is being **merged into** NovaCore's existing `self-verification` skill in Phase 2, not vendored as a parallel skill. |

### Deliberately out of scope

| Upstream component | Reason |
|---|---|
| **Visual Companion** (browser-served HTML mockup dashboard) | Infrastructure (local web server), not a SKILL.md. NovaCore can revisit as a separate optional component later. |
| **`EnterPlanMode` hook interception** (v5) | Overrides Claude Code's native plan mode. NovaCore already has `implementation-team` + `plan-tracker`; duplicating governance is aggressive and unnecessary. |
| **Brainstorm-server state hardening** (v5.0.6 fix) | N/A — Visual Companion is out of scope. |

## Deviations from upstream

Every deviation from upstream content is listed here. Each entry is an intentional NovaCore adaptation, not a bug.

### Global
- **Phrasing:** upstream "your human partner" → NovaCore "operator". Applied in every vendored skill.
- **Posture:** upstream frames skills as mandatory workflows. NovaCore's path-choice autonomy policy frames skills as recommended. The HARD-GATE in `brainstorming` and the Iron Law in `test-driven-development` are the **exceptions** — they remain absolute. The surrounding "MUST" language is softened elsewhere.
- **Frontmatter:** each vendored SKILL.md includes a `source:` block pinning the upstream tag, commit sha, and path. NovaCore skill validators should treat this as provenance metadata, not behavior.

### `brainstorming`
- **Visual Companion section removed** entirely (browser HTML infrastructure, out of scope).
- **Checklist item 2** ("Offer visual companion") removed.
- **Process-flow diagram** simplified — the visual-questions branch is gone.
- **Spec output path** is still `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md` as a Phase 1 default. Phase 2 wiring retargets to vault via `plan-tracker`.

### `test-driven-development`
- **Language/tooling examples** rewritten to Python + pytest (NovaCore default stack).
- **Iron Law preserved verbatim.** Exceptions still require operator approval.
- **Pine Script note** added pointing to `pinescript-developer` (no runtime test framework exists for Pine).

### `writing-plans`
- **Required header** changed from `superpowers:subagent-driven-development (recommended) or superpowers:executing-plans` → `implementation-team`.
- **Execution handoff** rewritten: single path (hand off to `implementation-team`) instead of upstream's 2-option fork.
- **Example code blocks** converted to Python/pytest.

### `systematic-debugging`
- **4-phase + 3-attempt escalation preserved verbatim** (the core value of the skill).
- **Phase 2 wiring note added**: the 3+ failure escalation will route to the Critic agent and log via `memory-store` (category `debug`). Phase 1 handles manually.
- **Pointer added** to `sequential-thinking` for non-bug structured reasoning.

### `using-git-worktrees`
- **Default global directory** changed from `~/.config/superpowers/worktrees/` → `~/.config/nova-core/worktrees/`.
- **Setup auto-detection** reordered to try Python (pyproject.toml / requirements.txt) first, since NovaCore is Python-default.
- **Baseline test command** defaults to `pytest`.
- **Attribution rewrite:** upstream's "Jesse's rule 'Fix broken things immediately'" is rendered as a generic principle (no name attribution).

### `finishing-a-development-branch`
- **Option 2** ("Push and create a Pull Request") now delegates to `/ship` as the standard path. Raw `git push` + `gh pr create` is a documented fallback. Phase 2 wiring makes the `/ship` handoff concrete.
- **Caller list** updated: `implementation-team` replaces `subagent-driven-development` and `executing-plans`.
- **Baseline test command** defaults to `pytest`.
- **Option 2 expanded beyond upstream scope.** Upstream's Option 2 is "push + create PR" with `git push` + `gh pr create` inline. NovaCore's Option 2 additionally (a) runs a Fusion Memory checkpoint via the `/ship` contract, (b) assembles a plan-tracker-aware PR draft with a fallback chain, (c) gates PR creation on a single-round operator confirm, and (d) delegates remote-divergence handling to the `ship-rebase-conflict-resolution` skill. Recorded here as a NovaCore adaptation.

### `dispatching-parallel-agents`
- **Cost-optimized model-selection guidance added** (adopted from upstream v5's "use cheapest capable model" pattern): Haiku 4.5 default for bounded mechanical tasks, escalate to Sonnet/Opus only when the subtask's reasoning profile requires it.
- **Budget-rules reference** added pointing to `AGENTS/orchestrator/AGENT.md` (Phase 2 wiring makes this a hard gate).
- **`implementation-team` callout** distinguishes the governed multi-agent workflow from this skill's narrower ad-hoc parallel-dispatch scope.

## Phase 2 wiring (applied 2026-04-24)

The vendored skills are now wired into NovaCore's memory and orchestration stack:

- ✅ `writing-plans` → output retargeted through `plan-tracker` to the Obsidian vault at `10-plans/plan-<plan_id>.md` with the `type: implementation-plan` schema; optionally queues a `TASKS/<plan_id>.md` entry. Execution handoff invokes `implementation-team`.
- ✅ `systematic-debugging` → 3+ failure escalation now dispatches the **Critic agent** (`AGENTS/critic/AGENT.md`) with the full evidence trail, logs the root cause via **`memory-store`** (`memory_type: research`, tag `debug`), and presents the verdict to the operator before fix #4. See the new "3+ Failed Fixes — Escalation" section in `systematic-debugging/SKILL.md`.
- ✅ `verification-before-completion` content → **merged into `self-verification`** as the "Evidence-Before-Claims Gate" section (Iron Law, gate function, rationalization table, red-flag patterns). `self-verification/SKILL.md` frontmatter records the merge provenance under `merged_from:`.
- ✅ `finishing-a-development-branch` → Option 2 executes the `/ship` contract (`.claude/commands/ship.md` steps 1-3) **inline** using an announce-and-continue pattern (no nested slash-command invocation), then the skill owns `gh pr create` with a plan-tracker-aware PR draft and one-round operator confirm. Failure modes delegate to the existing `ship-rebase-conflict-resolution` skill. Contract and acceptance criteria: `docs/superpowers/specs/2026-04-24-finishing-ship-autodelegation-design.md`.
- ⏳ `dispatching-parallel-agents` → budget rules from `AGENTS/orchestrator/AGENT.md` are referenced in the skill as an expectation. Making them a **hard gate** (programmatic budget enforcement) is deferred until the orchestrator exposes a budget-check hook.

The remaining ⏳ item (`dispatching-parallel-agents`) is left as a content-level reference rather than a programmatic gate because it depends on an orchestrator budget API that does not yet exist. It can be upgraded without re-touching the vendored skill.

## Re-pull procedure

When a new upstream release lands:

1. `gh api repos/obra/superpowers/git/ref/tags/<new-tag>` → record the new tag sha + commit sha.
2. `gh api repos/obra/superpowers/contents/skills/<skill>/SKILL.md?ref=<new-tag>` for each vendored skill → fetch raw bytes.
3. Diff against the current vendored files and the deviation list above. Any upstream change that overlaps a deviation needs a decision: preserve the deviation, or adopt upstream.
4. Update every vendored skill's `source:` frontmatter block (tag, commit, sha).
5. Update the **Upstream** section at the top of this file.
6. Append a new "Deviations from upstream (re-pull YYYY-MM-DD)" section if any new adaptations were made.
7. Commit with a message referencing the upstream tag.

## Deviations from upstream (re-pull 2026-07-24)

Changes applied from v5.0.7 → v6.2.0 and any new adaptations required.

### Global changes adopted
- **Prose removal:** upstream v6.2.0 deleted "Bottom Line / Key Principles / Real-World Impact" sections across the library. Applied: removed `## Key Principles` from `brainstorming`. Not applicable to other vendored skills (those sections were absent or not tracked).
- **Frontmatter updated** in all 7 vendored SKILL.md files: `tag` → `v6.2.0`, `commit` → `3dcbd5c4b48e02263fbf4a3c01e3fe4f81d584d9`.
- **NovaCore adaptations callout** version reference updated in all 7 skills.

### `using-git-worktrees` — major restructure adopted
- **Step 0 (Detect Existing Isolation):** new upstream section adopted verbatim, including the submodule guard (`git rev-parse --show-superproject-working-tree`). This prevents false-positive isolation detection inside submodules.
- **Step 1a / 1b split:** upstream now distinguishes native worktree tools (Step 1a, preferred) from git worktree fallback (Step 1b). Adopted. `EnterWorktree` / `/worktree` named explicitly as examples of native tools.
- **"Common Mistakes" → "Common Rationalizations":** section renamed per upstream; content updated.
- **NovaCore directory deviation preserved:** global directory remains `~/.config/nova-core/worktrees/<project-name>/` (not `superpowers`).
- **Python-first detection and `pytest` default preserved** in Step 2 / Step 3.
- **Operator ask preserved** in Step 1b directory selection (upstream v6.2.0 removed the explicit ask; NovaCore retains it as a UX preference).

### `finishing-a-development-branch` — structural changes adopted + deviation overlap flagged
- **"Discard" removed from main menu (adopted):** upstream v6.2.0 removed "Discard work" as Option 4. Now lives in a separate "If the operator asks to discard the work" section. The `Type 'discard' to confirm` ritual is preserved.
- **Step 2 (Detect Environment) adopted:** new upstream step using `GIT_DIR == GIT_COMMON` check + `WORKTREE_PATH` capture before directory changes. Enables correct detached-HEAD menu and provenance-based cleanup.
- **Step 6 (provenance-based Cleanup Workspace) adopted:** cleanup now keys on whether `WORKTREE_PATH` is under `.worktrees/` or `worktrees/` (Superpowers/NovaCore-owned) vs. host-managed. Cleaner than the previous `git worktree list | grep` approach.
- **Detached HEAD 2-option menu adopted** from upstream v6.2.0.
- **⚠ DEVIATION OVERLAP — forge-agnostic PR (needs operator review):** upstream v6.2.0 changed Option 2 to be forge-agnostic ("forge's tooling — its CLI if one is available, or the creation URL most forges print when you push"). NovaCore's Option 2 explicitly uses `gh pr create` as part of the `/ship` contract. These two approaches conflict. **Resolution deferred to operator.** NovaCore's `/ship`-contract Option 2 is preserved unchanged pending review.

### `test-driven-development` — content changes adopted
- **"Why Order Matters" section removed** (upstream v6.2.0 migrated these arguments into the Common Rationalizations table; NovaCore's table already contained equivalent entries).
- **`writing-good-tests.md` reference added** to "Good Tests" section with a note that this skill is not vendored in NovaCore. The upstream renamed `testing-anti-patterns.md` → `writing-good-tests.md` in v6.2.0.
- **"Example: Bug Fix" section added** (new in upstream v6.2.0; converted from TypeScript to Python per NovaCore's standard).

### `dispatching-parallel-agents` — minor additions adopted
- **"Real Example from Session" section added** (present in upstream v6.2.0, absent from NovaCore's v5.0.7 vendoring). File names converted from TypeScript (`.test.ts`) to Python (`.py`).

### Skills with frontmatter-only updates (no confirmed content changes)
- **`writing-plans`**: WebFetch retrieved a summary only; no structural changes detected. Frontmatter bumped. NovaCore deviations (plan-tracker / implementation-team) still apply against upstream v6.2.0.
- **`systematic-debugging`**: Same. The 4-phase + 3-attempt escalation and Critic-agent routing are NovaCore additions not present in upstream; they are preserved.
- **Manual review recommended** for both skills: fetch full raw content from `obra/superpowers/skills/*/SKILL.md?ref=v6.2.0` and diff against the vendored files to catch any prose-removal changes missed in this automated re-pull.

## Audit trail

| Date | Event | Tag | Commit SHA |
|---|---|---|---|
| 2026-04-24 | Initial Phase 1 vendoring | v5.0.7 | `1f20bef3f59b85ad7b52718f822e37c4478a3ff5` |
| 2026-04-24 | Phase 2 wiring — finishing-branch Option 2 auto-delegation | v5.0.7 | `1f20bef3f59b85ad7b52718f822e37c4478a3ff5` |
| 2026-07-24 | Quarterly re-pull from upstream | v6.2.0 | `3dcbd5c4b48e02263fbf4a3c01e3fe4f81d584d9` |
