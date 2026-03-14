---
name: github-ops
description: >-
  Manage GitHub repositories, issues, pull requests, and CI/CD via GitHub MCP.
  Auto-invoked when tasks involve repo management, issue triage, PR review,
  checking CI status, or code search across GitHub. Also use when the user
  references a GitHub URL, issue number, or repository name.
argument-hint: "[owner/repo] [action: issues|prs|ci|search|create-issue]"
allowed-tools:
  - mcp__github__search_repositories
  - mcp__github__get_file_contents
  - mcp__github__create_or_update_file
  - mcp__github__push_files
  - mcp__github__create_branch
  - mcp__github__list_commits
  - mcp__github__list_issues
  - mcp__github__search_issues
  - mcp__github__create_issue
  - mcp__github__add_issue_comment
  - mcp__github__create_pull_request
  - mcp__github__list_pull_requests
  - mcp__github__merge_pull_request
  - mcp__github__search_code
activation:
  keywords:
    - github
    - repo
    - issue
    - pull request
    - pr
    - ci
---

# GitHub Operations

Manage repositories, issues, PRs, and CI/CD through the official GitHub MCP Server.

## Workflow

1. **Identify scope** — which repo? (`owner/repo` format)
2. **Execute action** — use the appropriate tool:
   - **Issues**: `list_issues`, `search_issues`, `create_issue`, `add_issue_comment`
   - **PRs**: `list_pull_requests`, `create_pull_request`, `merge_pull_request`
   - **Code**: `search_code`, `get_file_contents`, `push_files`
   - **Repo**: `search_repositories`, `list_commits`, `create_branch`
3. **Report result** — summarize what was done with links

## Conventions

- Default repo: `nova-core-ai/nova-core` (override if user specifies)
- When creating issues, always include reproduction steps or context
- When creating PRs, include a `## Summary` and `## Test plan` in the body
- For CI checks, report status as pass/fail/pending with links to logs
- Never force-merge; always check CI status first

## Safety

- Read-only operations need no confirmation
- Write operations (create issue, merge PR, push files) should confirm with user unless CLAUDE.md grants autonomy
- Never delete branches without explicit request
