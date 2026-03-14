# Skill Evaluation Rubric

Use this rubric to evaluate the quality of a Nova-Core skill before marking it production-ready.

## Scoring

Rate each dimension 1-5. A skill needs an average of 3.5+ to be "usable" and 4.0+ to be "production-ready".

---

## 1. Clarity (1-5)

Does the skill clearly communicate what Claude should do?

| Score | Description |
|-------|-------------|
| 1 | Vague, ambiguous instructions. Claude would guess at most steps. |
| 2 | Some steps are clear, but key decisions are left unspecified. |
| 3 | Main workflow is clear. A few edge cases are ambiguous. |
| 4 | All steps are clear and unambiguous. Edge cases are addressed. |
| 5 | Crystal clear. A new reader could understand the skill instantly. |

## 2. Triggering (1-5)

Does the skill activate when it should and stay silent when it shouldn't?

| Score | Description |
|-------|-------------|
| 1 | Description is generic. Triggers on many unrelated prompts. |
| 2 | Triggers sometimes. Misses many valid use cases. |
| 3 | Triggers for obvious cases. Misses edge cases or has some false positives. |
| 4 | Reliable triggering. Few false positives or negatives. |
| 5 | Precise triggering. Activates exactly when needed, never when not. |

## 3. Completeness (1-5)

Does the skill cover all the scenarios it should?

| Score | Description |
|-------|-------------|
| 1 | Handles only the happy path. Fails on any variation. |
| 2 | Covers main use case. Falls apart on edge cases. |
| 3 | Covers main use cases and common variations. Some gaps remain. |
| 4 | Comprehensive coverage. Handles most edge cases gracefully. |
| 5 | Exhaustive. Every reasonable scenario is addressed or has a fallback. |

## 4. Output Quality (1-5)

Does the skill produce consistent, high-quality outputs?

| Score | Description |
|-------|-------------|
| 1 | Outputs are unpredictable in format and quality. |
| 2 | Basic structure is consistent, but content quality varies wildly. |
| 3 | Consistent format. Content quality is acceptable. |
| 4 | Consistent, high-quality outputs. Contract is always satisfied. |
| 5 | Outputs are polished, complete, and exceed expectations consistently. |

## 5. Safety (1-5)

Does the skill respect Nova-Core safety boundaries?

| Score | Description |
|-------|-------------|
| 1 | No safety constraints. Could modify files outside sandbox. |
| 2 | Some constraints mentioned, but not enforced by the workflow. |
| 3 | Safety constraints exist. Tool usage rules are reasonable. |
| 4 | Strong safety model. Failure handling prevents data loss. |
| 5 | Defense in depth. Verification steps catch issues. Crash recovery works. |

## 6. Efficiency (1-5)

Does the skill use Claude's time and tokens well?

| Score | Description |
|-------|-------------|
| 1 | Excessive steps, redundant tool calls, bloated output. |
| 2 | Some waste but generally progresses toward the goal. |
| 3 | Reasonable efficiency. No major waste. |
| 4 | Lean workflow. Each step contributes directly to the outcome. |
| 5 | Optimally efficient. No unnecessary steps, reads, or tool calls. |

---

## Overall Verdict

| Average Score | Verdict |
|---------------|---------|
| < 2.5 | **Needs rewrite** — fundamental issues with the skill design |
| 2.5 - 3.4 | **Draft** — usable for testing but not ready for production |
| 3.5 - 3.9 | **Usable** — works for most cases, minor improvements needed |
| 4.0 - 4.4 | **Production-ready** — solid skill, can be relied upon |
| 4.5+ | **Excellent** — exemplary skill, use as reference for others |

## Quick Checklist

Before marking a skill as done:

- [ ] Frontmatter validates (run `scripts/validate_skill.py`)
- [ ] Description is specific and "pushy"
- [ ] Activation keywords tested with `dev_check_skills.py`
- [ ] No keyword conflicts with existing 42+ skills
- [ ] Workflow has 4-8 clear, imperative steps
- [ ] Tool usage rules are explicit
- [ ] Output contract is defined and complete
- [ ] At least 2 examples included
- [ ] Under 500 lines (or uses progressive disclosure)
- [ ] Safety boundaries enforced
- [ ] Failure handling covers likely error scenarios
