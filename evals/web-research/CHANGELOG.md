# web-research eval dataset changelog

## 2026-03-15 — test.jsonl: replace invalid negative

**Changed query**: "edit the SKILL.md file to improve the description" → "fix the typo in the README file"

### Why the original was invalid

The eval harness detects skill activation by monitoring `Read` tool calls
to `.claude/skills/{skill_name}/SKILL.md`. When the query explicitly asks
to "edit the SKILL.md file," Claude reads the web-research SKILL.md to
understand what to edit — not because it activated the skill, but because
the user asked it to interact with that file. The harness cannot
distinguish these two cases, producing a deterministic false positive
(3/3 triggers) on both baseline and candidate descriptions.

This was confirmed empirically: when asked directly ("would you use
web-research for this query?"), Claude answers NO. The trigger is purely
an artifact of the Read-based detection path.

### Why the replacement is valid

"fix the typo in the README file" preserves the original test intent
(wrong-domain file-editing task, easy difficulty) without referencing
any artifact in the eval harness's detection path. It does not make the
benchmark easier — it removes a query that was structurally impossible
to pass regardless of description quality.

### Evidence

| Description | "edit the SKILL.md..." | "fix the typo..." |
|-------------|------------------------|--------------------|
| Baseline    | 3/3 FP (artifact)      | 0/3 TN (correct)   |
| Candidate   | 3/3 FP (artifact)      | 0/3 TN (correct)   |
