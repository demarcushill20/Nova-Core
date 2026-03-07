# Note Type Guide

## Vault Folder Structure

The Nova-Core Open Memory vault uses numbered folders with semantic roles:

| Folder | Owner | Purpose |
|--------|-------|---------|
| `00-inbox` | Nova-Core | Unsorted incoming notes |
| `10-adrs` | Human | Architecture Decision Records |
| `20-agent-patterns` | Nova-Core | Agent behavior patterns and conventions |
| `30-workflow-learnings` | Nova-Core | Lessons learned from task execution |
| `40-research` | Nova-Core | Research summaries and analysis |
| `50-playbooks` | Human | Operator playbooks and runbooks |
| `60-project` | Human | Project-level planning and status |
| `70-debugging` | Nova-Core | Debugging insights and solutions |
| `80-references` | Human | Reference material and bookmarks |
| `90-diary` | Human | Session diary and chronological log |
| `_meta` | Shared | Vault metadata and configuration |

## Retrieval Implications

- **Human-managed folders** (`10-adrs`, `50-playbooks`, `60-project`, `80-references`, `90-diary`): treat content as human-authored. Do not "correct" or dismiss. These represent operator intent.
- **Nova-managed folders** (`00-inbox`, `20-agent-patterns`, `30-workflow-learnings`, `40-research`, `70-debugging`): machine-generated but may have been edited by the human. Still treat with respect.
- **Search broadly, read narrowly.** `vault_search` searches across all folders. Use folder context from results to interpret authority and freshness.
- **ADRs are decisions.** Notes in `10-adrs` represent finalized architectural decisions. Weight them highly.
- **Diary is chronological.** Notes in `90-diary` are session logs. They provide timeline context but may be superseded by newer entries.
