---
name: brainstorming
description: "Use before any creative engineering work — designing a feature, adding a component, or modifying behavior. Explores intent, constraints, and design options before implementation. Also use when the user says 'brainstorm', 'design', 'let's think through', or describes a new capability without an implementation plan."
source:
  upstream: obra/superpowers
  tag: v6.2.0
  commit: 3dcbd5c4b48e02263fbf4a3c01e3fe4f81d584d9
  path: skills/brainstorming/SKILL.md
  license: MIT
---

# Brainstorming Ideas Into Designs

Turn ideas into fully formed designs and specs through autonomous engineering judgment. Understand project context first, then ultrathink each tactical decision and pick the best path — asking the operator only for genuinely operator-only judgment (intent, risk tolerance, business priority, information only they have). Present the resulting design and get a single explicit approval before any implementation begins.

> **NovaCore adaptations (vendored from Superpowers v6.2.0):**
> - The upstream **Visual Companion** (browser-served HTML mockups) is **out of scope** for this vendoring. Text-only brainstorming only.
> - Spec output currently lands in `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md`. Retargeting spec output through `plan-tracker` into the Obsidian vault is **deferred** (tracked in `.claude/skills/_vendored/SUPERPOWERS.md`). `writing-plans` *is* already retargeted to the vault.
> - Upstream's "MUST" language is kept where it protects the HARD-GATE; elsewhere this skill is recommended, not mandatory, consistent with NovaCore's path-choice autonomy policy.
> - **NovaCore behavioral override (2026-04-28):** Upstream's "ask one question at a time" interaction pattern is replaced with autonomous tactical decision-making per `feedback_decision_autonomy.md`. The HARD-GATE on final design approval is preserved.

<HARD-GATE>
Do NOT invoke any implementation skill, write any code, scaffold any project, or take any implementation action until you have presented a design and the operator has approved it. This applies to every project regardless of perceived simplicity.
</HARD-GATE>

## Anti-Pattern: "This Is Too Simple To Need A Design"

Every project goes through this process. A todo list, a one-function utility, a config change — all of them. "Simple" projects are where unexamined assumptions cause the most wasted work. The design can be short (a few sentences for truly simple projects), but you must present it and get approval.

## Checklist

Create a task for each of these and complete them in order:

1. **Explore project context** — check files, docs, recent commits
2. **Ask operator-only questions, if any** — only when the operator has unique knowledge you can't infer (intent/purpose if absent from args, risk tolerance, business priority, deadline). Default is to skip this step entirely. One question at a time when asking is genuinely needed.
3. **Resolve tactical decisions autonomously** — for each design dimension (method, format, schema, scope, tooling, etc.), ultrathink and pick the best path; record the choice + reasoning + trade-offs as part of the design draft.
4. **Present design** — full design in one pass, sections scaled to their complexity. Lead with the resolved decisions and trade-offs. Get a single operator approval at the end.
5. **Write design doc** — save to `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md` and commit
6. **Spec self-review** — inline check for placeholders, contradictions, ambiguity, scope
7. **Operator reviews written spec** — ask for review before proceeding
8. **Transition to implementation** — invoke `writing-plans` to create the implementation plan

## Process Flow

```dot
digraph brainstorming {
    "Explore project context" [shape=box];
    "Operator-only questions needed?" [shape=diamond];
    "Ask operator-only questions" [shape=box];
    "Resolve tactical decisions (ultrathink)" [shape=box];
    "Present full design" [shape=box];
    "Operator approves design?" [shape=diamond];
    "Write design doc" [shape=box];
    "Spec self-review (fix inline)" [shape=box];
    "Operator reviews spec?" [shape=diamond];
    "Invoke writing-plans" [shape=doublecircle];

    "Explore project context" -> "Operator-only questions needed?";
    "Operator-only questions needed?" -> "Ask operator-only questions" [label="yes"];
    "Operator-only questions needed?" -> "Resolve tactical decisions (ultrathink)" [label="no (default)"];
    "Ask operator-only questions" -> "Resolve tactical decisions (ultrathink)";
    "Resolve tactical decisions (ultrathink)" -> "Present full design";
    "Present full design" -> "Operator approves design?";
    "Operator approves design?" -> "Resolve tactical decisions (ultrathink)" [label="no, revise"];
    "Operator approves design?" -> "Write design doc" [label="yes"];
    "Write design doc" -> "Spec self-review (fix inline)";
    "Spec self-review (fix inline)" -> "Operator reviews spec?";
    "Operator reviews spec?" -> "Write design doc" [label="changes requested"];
    "Operator reviews spec?" -> "Invoke writing-plans" [label="approved"];
}
```

**Terminal state is invoking `writing-plans`.** Do not jump to any other implementation skill from here.

## The Process

**Understanding the idea:**

- Check the current project state first (files, docs, recent commits, prior memory entries).
- Assess scope before deciding anything else. If the request describes multiple independent subsystems (e.g., "build a platform with chat, file storage, billing, and analytics"), flag that immediately — don't refine a project that needs decomposition first.
- If the project is too large for a single spec, help decompose into sub-projects: what are the independent pieces, how do they relate, what order? Then brainstorm the first sub-project through this flow. Each sub-project gets its own spec → plan → implementation cycle.

**Operator-only questions (default: skip):**

- Ask the operator only when they have unique knowledge you cannot infer from project context, args, or memory. Examples of operator-only items: purpose/intent (when not stated in args), risk tolerance for breaking changes, business priority or deadline, choice between paths driven by product judgment rather than engineering trade-offs, info that lives in the operator's head.
- Default action is to skip this step. Most well-scoped requests carry enough context to proceed.
- When asking is genuinely needed: one question at a time, multiple-choice when possible.

**Tactical decisions (autonomous):**

- For every engineering-judgment decision in the design (method, format, schema, scope cuts, threshold values, tooling, data location, etc.), apply maximum reasoning effort (ultrathink-level) and pick the best option for the situation.
- Surface each decision in the design draft as a short block: **Decision:**, **Reasoning:**, **Trade-offs considered:**. The operator sees the full reasoning and can redirect any individual choice when reviewing the design.
- When propose-2-3-approaches feels load-bearing for a particularly consequential decision, include the alternatives inline in the design under "Considered" — the operator gets to see the full option space without having to vote on each one.

**Presenting the design:**

- Present the full design in one pass, sections scaled to complexity (a few sentences when straightforward, up to 200–300 words when nuanced).
- Cover: architecture, components, data flow, error handling, testing — plus the resolved tactical decisions with their reasoning.
- Get a single explicit approval at the end. If the operator pushes back on a specific decision, revise that section and re-present.
- Go back and clarify if something doesn't make sense.

**Design for isolation and clarity:**

- Break the system into units with one clear purpose, well-defined interfaces, and independent testability.
- For each unit: what does it do, how do you use it, what does it depend on?
- Large files are a signal that a unit is doing too much.

**Working in existing codebases:**

- Explore the current structure before proposing changes. Follow existing patterns.
- If existing code has problems that affect the work (oversized file, unclear boundaries), include targeted improvements in the design.
- Don't propose unrelated refactoring. Stay focused on the current goal.

## After the Design

**Documentation:**

- Write the validated design to `docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md` (operator preference overrides this default; Phase 2 wiring retargets to vault via `plan-tracker`).
- Commit the design document to git.

**Spec Self-Review:**

After writing the spec, look at it with fresh eyes:

1. **Placeholder scan:** any "TBD", "TODO", incomplete sections, or vague requirements? Fix them.
2. **Internal consistency:** any sections contradict each other? Does the architecture match the feature descriptions?
3. **Scope check:** focused enough for one implementation plan, or does it need decomposition?
4. **Ambiguity check:** any requirement interpretable two different ways? Pick one and make it explicit.

Fix issues inline. No need to re-review — just fix and move on.

**Operator Review Gate:**

After the self-review passes, ask the operator to review the spec before proceeding:

> "Spec written and committed to `<path>`. Please review and let me know if you want changes before we write the implementation plan."

Wait for a response. If changes requested, make them and re-run self-review. Only proceed once approved.

**Implementation handoff:**

- Invoke the `writing-plans` skill to create a detailed implementation plan.
- Do not invoke any other skill from here. `writing-plans` is the next step.

