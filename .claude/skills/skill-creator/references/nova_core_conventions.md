# Nova-Core Skill Conventions

Quick reference for patterns and conventions specific to Nova-Core skills.

## Dual Skill System

| Aspect | Prompt Skills | Execution Skills |
|--------|--------------|-----------------|
| **Location** | `.claude/skills/<name>/SKILL.md` | `SKILLS/<name>/SKILL.md` |
| **Purpose** | Enhance Claude during interactive sessions | Autonomous work via orchestrator/watcher |
| **Count** | 42 skills | 4 skills |
| **Activation** | Python engine via keyword matching | Hardcoded by orchestrator/supervisor |
| **Frontmatter** | name, description, activation.keywords, tool_doctrine, output_contract | name, version, description |
| **Output** | Flexible with contract headings | Strict machine-checkable CONTRACT block |

## Skill Selection Engine (tools/skills.py)

The engine at `tools/skills.py` selects prompt skills based on task text:

1. **Always-on**: `task-execution`, `self-verification` — included regardless
2. **Built-in keyword rules**: git-ops (git, commit, branch), file-ops (.py, .md, read, write), shell-ops (bash, sudo, systemctl)
3. **Activation keywords**: case-insensitive substring match from frontmatter

**Testing activation**: `python tools/dev_check_skills.py "your test prompt"`

## Frontmatter Fields

### Required (prompt skills)
- `name`: kebab-case, max 64 chars, must match directory name
- `description`: max 1024 chars — PRIMARY triggering mechanism
- `activation.keywords`: list of trigger strings — without these, the skill WILL NOT be selected by `tools/skills.py`

### Optional (informational, read by Claude in body)
- `allowed-tools`: explicit MCP tool whitelist
- `disable-model-invocation`: boolean flag
- `argument-hint`: usage hint shown in skill list
- `tool_doctrine`: execution discipline — informational, NOT parsed by selection engine
- `output_contract`: required output fields — informational, NOT parsed by selection engine

### Required (execution skills)
- `name`: kebab-case
- `version`: semver (e.g., 1.0.0)
- `description`: what it does

## Output Contract Patterns

### Prompt skill contract (headings-based)
```markdown
## Findings
<content>

## Confidence
<high | medium | low> — <justification>
```

### Execution skill contract (machine-checkable)
```
## CONTRACT
summary: <one-liner>
status: <done | failed>
files_changed: <paths>
verification: <how confirmed>
confidence: <low | medium | high>
```

## Existing Skill Categories

Know what exists to avoid duplication:

- **Memory lifecycle** (8): memory-store, memory-recall, memory-checkpoint, etc.
- **Vault/Note** (3): reading-obsidian-memory, writing-agent-patterns, capturing-workflow-learnings
- **Research** (5): web-research, http-fetch, browser-automation, firecrawl-*, research-to-action
- **Sandbox ops** (3): file-ops, git-ops, shell-ops
- **Execution** (2): task-execution, self-verification
- **Google** (4): google-calendar, google-docs, google-drive, google-gmail
- **External** (8): github-ops, gmail-triage, n8n-workflows, etc.
- **Analysis** (3): semgrep-security, context7-docs, sequential-thinking

## Safety Boundaries

- All file operations stay within `~/nova-core`
- Never store secrets, API keys, or passwords in skill files
- Execution skills must handle crashes gracefully (no human in the loop)
- Prompt skills should degrade gracefully when MCP tools are unavailable
- Always log major actions to `LOGS/`
