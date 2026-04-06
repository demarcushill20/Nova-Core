---
name: self-verification
description: Compact verification rules
version: "1.0"
---

## Self-Verification (Compact)

After every action, verify:
1. Read-after-write: confirm files exist and contain expected content
2. Check exit codes (don't trust 0 alone -- verify actual output)
3. Validate CONTRACT block: summary, verification, confidence required
4. Cross-reference: output matches task requirements
5. Score confidence: high (all checks pass), medium (partial), low (issues found)

Never assume success. Always verify before marking tasks done.
