This skill already exists at `.claude/skills/autonomy-preflight-validation/SKILL.md` and is comprehensive — it covers the exact false-alarm pattern from the `0644_repair_strategy_validity_regression` task. The existing content is already production-quality with:

- Correct metric-to-raw-source mapping table
- Common divergence causes (stale data, empty window, timezone mismatch)
- Three-way verdict system (CONFIRMED / FALSE_ALARM / INCONCLUSIVE)
- Memory feedback loop for hardening scorers

No changes needed — the skill is already complete and well-structured.