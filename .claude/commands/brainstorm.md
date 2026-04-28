# Brainstorm

Invoke the `brainstorming` skill to turn an idea into a fully-formed design through autonomous engineering judgment with a single approval gate at the end.

## Usage

```
/brainstorm <short description of what you want to build>
```

If no argument is given, ask the operator what they want to brainstorm.

## What it does

- Explores project context (files, docs, recent commits, prior memory) before deciding anything
- Asks the operator only for operator-only judgment (intent, risk tolerance, business priority) — default is to skip and proceed
- Resolves tactical engineering decisions autonomously with ultrathink-level reasoning; surfaces each choice + reasoning + trade-offs in the design draft
- Presents the full design in one pass with a single approval gate at the end
- Saves the validated spec and hands off to `/write-plan`

## HARD-GATE

No implementation actions — no code, no scaffolding, no other skill invocation — until the operator has approved the design.

## See also

- `.claude/skills/brainstorming/SKILL.md` — full skill contract
- `/write-plan` — the next step after the design is approved
