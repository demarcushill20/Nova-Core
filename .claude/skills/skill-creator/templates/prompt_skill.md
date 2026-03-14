# Prompt Skill Template

Use this template when creating a new `.claude/skills/<name>/SKILL.md` prompt-based skill.

---

```markdown
---
name: {{skill-name}}
description: "{{What this skill does. Be specific and pushy — include trigger phrases and contexts so Claude actually invokes it. Example: 'Fast web research using Brave and Tavily. Auto-invoked when tasks require finding current information, comparing sources, or answering factual questions.'}}"
activation:
  keywords:
    - {{keyword1}}
    - {{keyword2}}
    - {{relevant phrase}}
    - {{action verb}}
allowed-tools:              # optional: list MCP tools this skill needs
  - {{mcp__server__tool_name}}
---

# {{Skill Title}}

## When to use
- {{Trigger condition 1 — describe the situation, not the keyword}}
- {{Trigger condition 2}}
- {{Trigger condition 3}}

## Inputs
- **{{param1}}**: {{description}} (required)
- **{{param2}}**: {{description}} (optional, default: {{value}})

## Workflow

1. **{{Step name}}** — {{what to do and why it matters}}
2. **{{Step name}}** — {{what to do and why it matters}}
3. **{{Step name}}** — {{what to do and why it matters}}
4. **{{Step name}}** — {{what to do and why it matters}}

## Tool usage rules
- {{Constraint 1 — explain why this constraint exists}}
- {{Constraint 2 — explain the reasoning}}
- All operations must stay within `~/nova-core`.

## Outputs / contract

Every response MUST contain these headings:

\```
## {{Section 1}}
<{{what goes here}}>

## {{Section 2}}
<{{what goes here}}>

## Confidence
<high | medium | low> — <1-sentence justification>
\```

## Examples

### Example 1: {{Scenario name}}
**User**: "{{realistic user prompt}}"

**{{Output section}}**:
{{show what the skill produces}}

**Confidence**: {{level}} — {{justification}}

### Example 2: {{Scenario name}}
**User**: "{{realistic user prompt}}"

**{{Output section}}**:
{{show what the skill produces}}
```

---

## Template Notes

- **name**: kebab-case, max 64 characters, must match directory name
- **description**: max 1024 characters. This is the PRIMARY triggering mechanism. Be pushy.
- **activation.keywords**: REQUIRED — case-insensitive substring match by `tools/skills.py`. Without these the skill won't be auto-selected. Include obvious terms plus natural variations.
- **allowed-tools**: optional MCP tool whitelist
- **tool_doctrine**: optional, informational — snake_case workflow steps (not parsed by engine)
- **output_contract**: optional, informational — required output fields (not parsed by engine)
- **Workflow**: 4-8 steps is ideal. Use imperative form ("Read the file", not "The file should be read").
- **Examples**: Include 2-3 realistic examples showing realistic user prompts with full expected outputs.
- **Target length**: Under 500 lines. Use `references/` for overflow.
