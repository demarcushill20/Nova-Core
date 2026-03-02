# Summary Fallback Logic — Test & Design Document

## Purpose

Ensure `parse_task_report()` in `telegram_notifier.py` ALWAYS returns a meaningful
summary string, even when the report lacks a `## Summary` section.

## Fallback Priority Order

| Priority | Source | Extraction |
|----------|--------|------------|
| 1 | `## Summary` | Full section body |
| 2 | `## Actions Taken` | First bullet or sentence only |
| 3 | `## Instruction` | Full section body |
| 4 | `# Task Report:` / `# Output Report:` header | First non-metadata paragraph |
| 5 | Any `##` section | First bullet or sentence from first section |
| 6 | Any non-header, non-metadata line | First content line in document |
| final | (none) | `"(no summary available)"` |

## Extraction Rules

- Stop at next markdown header (`##` or `#`)
- Trim whitespace
- Collapse multiple newlines into one
- Cap at 300 characters (append `...` if truncated)
- Never return empty string

## Helper Functions Added

- `_extract_section(md_text, heading)` — Pull body text under a `## <heading>`, stopping at next header
- `_first_bullet_or_sentence(text)` — Return first bullet content or first sentence
- `_clean_summary(text)` — Normalize whitespace and enforce 300-char limit
- `_extract_summary(md_text)` — Main fallback chain orchestrator

## Test Results (all 17 OUTPUT files)

```
[OK] 0001_bootstrap           → Priority 6 (first content line: "Goal:")
[OK] 0002_agent_bootstrap     → Priority 5 (first ## section: "Deliverable")
[OK] 0004_real_autonomy       → Priority 1 (## Summary)
[OK] 0005_service_test        → Priority 1 (## Summary)
[OK] tg_*_tg_ok_txt           → Priority 1 (## Summary)
[OK] tg_*_auth_ok_txt         → Priority 1 (## Summary)
[OK] tg_*_notifier_ok_txt     → Priority 1 (## Summary)
[OK] tg_*_verbose_ok_txt      → Priority 1 (## Summary)
[OK] tg_*_hello               → Priority 1 (## Summary)
[OK] tg_*_ping_notifier       → Priority 1 (## Summary)
[OK] tg_*_log_test            → Priority 1 (## Summary)
[OK] tg_*_format_test         → Priority 1 (## Summary)
[OK] tg_*_cancelled           → Priority 1 (## Summary)
[OK] tg_*_sleep_test          → Priority 1 (## Summary)
[OK] tg_*_cancel_last         → Priority 1 (## Summary)
[OK] tg_*_worker_ok_txt       → Priority 2 (## Actions Taken)
[OK] tg_*_env_fix_test        → Priority 1 (## Summary)
```

Result: **17/17 OK, 0 EMPTY**

## Backward Compatibility

- Existing files with `## Summary` are unaffected (Priority 1 hits first)
- No changes to `compute_metrics()`, command routing, watcher, or file paths
- All existing regex patterns for Task ID and Completed timestamp preserved

## Diagnostic Script

`WORK/summary_fallback_diag.py` — Run against OUTPUT/ to verify all reports produce summaries.
