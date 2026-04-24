# Brainstorm

Invoke the `brainstorming` skill to turn an idea into a fully-formed design through collaborative dialogue.

## Usage

```
/brainstorm <short description of what you want to build>
```

If no argument is given, ask the operator what they want to brainstorm.

## What it does

- Explores project context (files, docs, recent commits) before asking questions
- Asks one clarifying question at a time — purpose, constraints, success criteria
- Proposes 2–3 approaches with trade-offs and a recommendation
- Presents the design section-by-section for operator approval
- Saves the validated spec and hands off to `/write-plan`

## HARD-GATE

No implementation actions — no code, no scaffolding, no other skill invocation — until the operator has approved the design.

## See also

- `.claude/skills/brainstorming/SKILL.md` — full skill contract
- `/write-plan` — the next step after the design is approved
