# Differences from Anthropic Official Version

This document explains exactly what was changed from Anthropic's official `skill-creator` (in `anthropics/skills` repository) and why.

## Source Material

- **Official repo**: `anthropics/skills` on GitHub
- **Official skill-creator**: `skills/skill-creator/` (33KB SKILL.md + 8 Python scripts + 3 agent files + 1 schema reference + 2 viewer files)
- **Date of analysis**: 2026-03-14

---

## Structural Changes

### Added: Dual Skill System Support
- **Anthropic**: Single skill type (SKILL.md with name + description frontmatter)
- **Nova-Core**: Two skill types — prompt skills (`.claude/skills/`) and execution skills (`SKILLS/`)
- **Why**: Nova-Core's architecture has a Python-based skill selection engine for prompt skills AND an orchestrator/watcher pipeline for execution skills. A skill-creator that only handles one type would be incomplete.
- **Impact**: Added separate templates, frontmatter specs, and body structures for each type.

### Added: Nova-Core Frontmatter Fields
- **Anthropic**: `name`, `description`, optional `compatibility`
- **Nova-Core**: `name`, `description`, `activation.keywords`, `tool_doctrine`, `output_contract`, optional `allowed-tools`, `disable-model-invocation`
- **Why**: Nova-Core's `tools/skills.py` selects skills via keyword matching. Without `activation.keywords`, a skill won't trigger through the selection engine. `tool_doctrine` and `output_contract` are Nova-Core conventions that formalize execution discipline and output validation.

### Added: Activation Testing via dev_check_skills.py
- **Anthropic**: Uses `claude -p` subprocess calls for description optimization and triggering tests
- **Nova-Core**: Uses `python tools/dev_check_skills.py "test prompt"` to verify skill selection
- **Why**: Nova-Core has a dedicated Python tool for testing skill activation. This is more deterministic than Anthropic's approach of running Claude and checking if it invokes the skill.

### Added: Evaluation Rubric
- **Anthropic**: No formal rubric — relies on qualitative human judgment and assertion-based grading
- **Nova-Core**: 6-dimension rubric (Clarity, Triggering, Completeness, Output Quality, Safety, Efficiency) with 1-5 scoring and overall verdict mapping
- **Why**: Nova-Core needs a consistent, repeatable way to assess skill quality across its 45+ skills. The rubric provides a shared language for quality assessment.

### Added: Nova-Core Conventions Reference
- **Anthropic**: N/A (the skill-creator assumes Anthropic's own environment)
- **Nova-Core**: `references/nova_core_conventions.md` documenting the dual skill system, selection engine, frontmatter fields, output contract patterns, existing skill categories, and safety boundaries
- **Why**: Anyone creating a Nova-Core skill needs to understand the ecosystem they're building for.

### Added: Contract Compliance in Grader
- **Anthropic grader**: Checks assertions and extracts claims
- **Nova-Core grader**: Also checks Nova-Core output contract compliance (required fields present, confidence justified, CONTRACT block for execution skills)
- **Why**: Nova-Core's output contracts are a core architectural pattern that needs to be validated during skill evaluation.

---

## Removed / Simplified

### Removed: eval-viewer/ (HTML Browser Viewer)
- **Anthropic**: 45KB `viewer.html` + 16KB `generate_review.py` + 7KB `eval_review.html` — a full browser-based review system with qualitative output browsing and benchmark display
- **Nova-Core**: Results presented directly in conversation or written to files
- **Why**: Nova-Core runs on a headless VPS. The browser-based viewer is designed for developers with a local display. Instead, we present results inline and use file-based feedback. If a browser viewer is needed in the future, the Anthropic originals can be adopted directly since they're self-contained HTML/Python.

### Backfilled (2026-03-14): Eval Scripts
- **Anthropic**: `run_eval.py`, `improve_description.py`, `run_loop.py`, `generate_report.py`, `aggregate_benchmark.py`, `quick_validate.py`, `utils.py` — automated eval + optimization loop
- **Nova-Core**: All 7 scripts ported to `scripts/` with minimal adaptation:
  - `CLAUDE_BIN` constant points to Nova-Core's Claude binary path
  - `run_eval.py` adds `--dangerously-skip-permissions` flag
  - `run_loop.py` replaces `webbrowser.open()` with file-save-only
  - `utils.py` uses Nova-Core's frontmatter parser + path constants
- **Why backfilled**: The eval infrastructure is the most valuable part of Anthropic's skill-creator. Keyword-based activation still benefits from automated testing (run_eval confirms the skill actually improves output quality, not just triggering).

### Removed: package_skill.py
- **Anthropic**: Creates distributable `.skill` zip files for sharing
- **Nova-Core**: Skills are managed via git in the nova-core repository
- **Why**: Nova-Core is a single-operator system where skills are committed directly to the repo. There's no marketplace or distribution mechanism — packaging adds complexity without benefit. If skill sharing becomes a need, this script can be adopted verbatim.

### Simplified: Claude.ai and Cowork Sections
- **Anthropic**: Extensive sections for Claude.ai-specific behavior (no subagents, no browser, manual iteration) and Cowork-specific behavior (subagents available, no display)
- **Nova-Core**: Removed these sections entirely
- **Why**: Nova-Core always runs in Claude Code on a VPS. There's no need for environment-specific behavioral switches.

---

## Preserved (Spirit and Substance)

These elements were preserved from the official Anthropic implementation, sometimes with light adaptation:

| Element | Status |
|---------|--------|
| **Core workflow**: intent → draft → test → evaluate → iterate | Preserved exactly |
| **Writing principles**: explain the why, keep it lean, generalize | Preserved exactly |
| **Progressive disclosure**: metadata → body → references | Preserved exactly |
| **Agent system**: grader, comparator, analyzer with structured JSON output | Preserved with Nova-Core contract additions |
| **Test case format**: evals.json with id, prompt, expected_output, assertions | Preserved with `skill_type` field added |
| **Grading format**: grading.json with expectations, summary, claims | Preserved with `contract_compliance` added |
| **Benchmark format**: benchmark.json with runs, run_summary, delta, notes | Preserved exactly |
| **Comparison format**: comparison.json with rubric scoring | Preserved exactly |
| **Iteration philosophy**: generalize from feedback, don't overfit | Preserved exactly |
| **"Principle of Lack of Surprise"**: no malware, no misleading skills | Preserved exactly |
| **Communication guidance**: adapt to user's technical level | Preserved exactly |

---

## Summary

The Nova-Core adaptation is a **structural fork**, not a copy. It preserves the Anthropic skill-creator's workflow, philosophy, evaluation methodology, and agent system, while adapting the skill format, frontmatter, activation mechanism, and tooling for Nova-Core's specific architecture. The browser-based viewer was removed because it doesn't fit the headless VPS environment. The eval scripts (run_eval, run_loop, improve_description, aggregate_benchmark, generate_report) have been backfilled with minimal adaptation — they use Nova-Core's binary path and skip the browser viewer, but otherwise preserve Anthropic's evaluation pipeline.
