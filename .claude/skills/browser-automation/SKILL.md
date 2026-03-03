---
name: browser-automation
description: "Playwright browser automation for multi-step web tasks — scraping dynamic pages, filling forms, navigating SPAs. Headless, login-free by default. Auto-invoked when HTTP fetch is insufficient."
disable-model-invocation: false
allowed-tools:
  - mcp__playwright__browser_navigate
  - mcp__playwright__browser_snapshot
  - mcp__playwright__browser_click
  - mcp__playwright__browser_type
  - mcp__playwright__browser_fill_form
  - mcp__playwright__browser_select_option
  - mcp__playwright__browser_press_key
  - mcp__playwright__browser_hover
  - mcp__playwright__browser_evaluate
  - mcp__playwright__browser_wait_for
  - mcp__playwright__browser_take_screenshot
  - mcp__playwright__browser_navigate_back
  - mcp__playwright__browser_tabs
  - mcp__playwright__browser_console_messages
  - mcp__playwright__browser_network_requests
  - mcp__playwright__browser_handle_dialog
  - mcp__playwright__browser_close
---

# Browser Automation

## When to use
- Page content is rendered by JavaScript (SPAs, React/Vue/Angular sites)
- Multi-step interaction required (click through tabs, expand sections, paginate)
- Need to fill and submit forms (without login credentials)
- Extracting data from dynamic tables, infinite scroll, or lazy-loaded content
- Taking screenshots of rendered pages
- HTTP fetch returned empty or incomplete content

## When NOT to use
- Static content fetchable via `http-fetch` — use that instead (faster, cheaper)
- Page requires login credentials — stop and report the constraint
- Simple URL retrieval — use `http-fetch`

## Inputs
- **url**: Starting URL (required)
- **task**: What to do on the page (required) — e.g., "extract the pricing table", "screenshot the hero section"
- **max_steps**: Maximum interaction steps before stopping. Default: 10

## Workflow

1. **Navigate** — `browser_navigate` to the starting URL.
2. **Snapshot** — `browser_snapshot` to get the accessibility tree. Prefer this over screenshots for action planning.
3. **Plan** — identify the minimal steps needed. Use `ref` attributes from the snapshot for deterministic selectors.
4. **Execute** — perform clicks, typing, form fills using exact `ref` values from snapshots.
5. **Re-snapshot** — after each significant action, take a new snapshot to verify state.
6. **Extract** — pull the needed data from the final page state.
7. **Close** — `browser_close` when done.

## Tool usage rules

### Selectors
- Always use `ref` attributes from `browser_snapshot`. Never guess CSS selectors.
- Re-snapshot after navigation or interaction — refs may change.

### Pacing
- Use `browser_wait_for` before extracting if content loads asynchronously.
- Do NOT add arbitrary sleeps. Wait for specific text or element presence.

### Failure handling
- **Login wall**: Stop immediately. Report the constraint in Errors. Do not attempt to bypass.
- **Captcha**: Stop immediately. Report in Errors.
- **Paywall**: Stop. Report what's visible and the constraint.
- **Infinite loop**: If the same snapshot appears 3 times, stop and report.
- **Console errors**: Check `browser_console_messages` if the page seems broken.

### Safety
- No credential entry. No payment form interaction. No file downloads to unknown paths.
- Close the browser when done to free resources.

## Outputs / contract

Every response MUST contain these headings:

```
## Run log
| Step | Action | Target | Result |
|------|--------|--------|--------|
| 1    | navigate | <url> | loaded |
| 2    | click | <element description> | success |
| ...  | ...    | ...    | ...    |

## Extracted data
<the data pulled from the page>

## Errors / blocks
<"None" or: login required, captcha detected, timeout, etc.>

## Next attempt suggestions
<"None" or suggestions for alternative approaches>
```

## Examples

### Example 1: Extract data from a dynamic page
**User**: "Get the current top 5 trending repos on GitHub"

```
## Run log
| Step | Action | Target | Result |
|------|--------|--------|--------|
| 1 | navigate | https://github.com/trending | loaded |
| 2 | snapshot | page | 25 repo entries found |
| 3 | extract | first 5 repo names + descriptions | success |

## Extracted data
1. user/repo-a — Description A (1,234 stars today)
2. user/repo-b — Description B (987 stars today)
...

## Errors / blocks
None

## Next attempt suggestions
None
```

### Example 2: Page blocked by login
**User**: "Scrape my GitHub notifications"

```
## Run log
| Step | Action | Target | Result |
|------|--------|--------|--------|
| 1 | navigate | https://github.com/notifications | redirected to login |
| 2 | snapshot | page | login form detected |

## Extracted data
None — page requires authentication.

## Errors / blocks
Login required. GitHub redirected to sign-in page. This skill does not handle credentials.

## Next attempt suggestions
- Use GitHub API via `gh` CLI (already authenticated on this system)
- Use `gh api /notifications` for programmatic access
```
