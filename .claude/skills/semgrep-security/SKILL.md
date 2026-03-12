---
name: semgrep-security
description: "Static security scanning on code using Semgrep MCP. Detects vulnerabilities, code smells, and anti-patterns in generated or existing code. Auto-invoked after writing security-sensitive code or when reviewing code for vulnerabilities."
disable-model-invocation: false
allowed-tools:
  - mcp__semgrep__security_check
  - mcp__semgrep__semgrep_scan
  - mcp__semgrep__semgrep_scan_with_custom_rule
  - mcp__semgrep__get_abstract_syntax_tree
  - mcp__semgrep__supported_languages
  - mcp__semgrep__semgrep_rule_schema
activation:
  keywords: [security scan, vulnerability, semgrep, static analysis, code review, audit code, check for bugs, OWASP]
  when:
    - After writing security-sensitive code (auth, input handling, crypto, network)
    - User asks to review code for vulnerabilities
    - User asks to scan a file or directory for security issues
    - Investigating a potential vulnerability or CVE
    - Writing custom detection rules
tool_doctrine:
  security_scanning:
    workflow:
      - scan_before_shipping
      - triage_by_severity
      - fix_critical_immediately
      - explain_findings_clearly
      - never_ignore_without_justification
output_contract:
  required:
    - summary
    - files_scanned
    - findings_count
    - critical_findings
    - verification
    - confidence
---

# Semgrep Security Scanning

## When to use

- After writing code that handles authentication, authorization, or sessions
- After writing code that processes user input or external data
- After writing code that uses cryptography, hashing, or secrets
- After writing network-facing code (API endpoints, WebSocket handlers)
- When the user asks to "scan", "audit", or "review" code for security issues
- Before committing security-sensitive changes
- When investigating whether a codebase is affected by a known vulnerability pattern

## When NOT to use

- For code formatting or style issues (use ruff instead)
- For type checking (use mypy instead)
- For non-security code review (general logic, performance)
- On files that aren't source code (configs, markdown, data files)

## Inputs

- **target**: File path, directory, or code snippet to scan (required)
- **config**: Semgrep config string. Default: `auto` (uses recommended rules). Other options: `p/security-audit`, `p/owasp-top-ten`, `p/python`
- **scope**: `quick` (security_check only), `standard` (security_check + targeted scan), `deep` (all rulesets + custom rules). Default: `standard`

## Workflow

### Step 1 -- Quick Security Check

Start with the built-in security check for a fast overview:

```
Tool: mcp__semgrep__security_check
Args: {
  "code": "<code content or file path>",
  "language": "python"
}
```

This runs Semgrep's curated security rules and returns findings with severity levels.

### Step 2 -- Targeted Scan (if scope >= standard)

For deeper analysis, run a targeted scan with specific rulesets:

```
Tool: mcp__semgrep__semgrep_scan
Args: {
  "paths": ["/home/nova/nova-core/telegram/"],
  "config": "p/owasp-top-ten"
}
```

Useful configs:
| Config | Use for |
|--------|---------|
| `auto` | General best-effort (default) |
| `p/security-audit` | Comprehensive security review |
| `p/owasp-top-ten` | OWASP Top 10 compliance |
| `p/python` | Python-specific patterns |
| `p/secrets` | Hardcoded secrets detection |
| `p/command-injection` | Command injection patterns |

### Step 3 -- Triage Findings

Classify each finding by severity and actionability:

| Severity | Action |
|----------|--------|
| **ERROR / Critical** | Fix immediately before proceeding |
| **WARNING / High** | Fix before committing |
| **INFO / Medium** | Fix if straightforward, otherwise note |
| **NOTE / Low** | Document if relevant, skip if noise |

### Step 4 -- Fix Critical Issues

For each critical/high finding:
1. Read the affected code
2. Understand the vulnerability
3. Apply the fix
4. Re-scan to confirm the fix resolved the issue

### Step 5 -- Custom Rules (if scope = deep)

For project-specific patterns, write custom Semgrep rules:

```
Tool: mcp__semgrep__semgrep_scan_with_custom_rule
Args: {
  "paths": ["/home/nova/nova-core/"],
  "rule": "rules:\n  - id: nova-no-raw-exec\n    pattern: os.system($X)\n    message: Use subprocess.run instead of os.system\n    severity: WARNING\n    languages: [python]"
}
```

### Optional -- AST Inspection

For understanding complex code structures:

```
Tool: mcp__semgrep__get_abstract_syntax_tree
Args: {
  "code": "def handler(request): ...",
  "language": "python"
}
```

## Tool Usage Rules

- **Always start with security_check.** It's fast and catches the most common issues.
- **Scan changed files, not the whole repo.** Target specific paths to keep results actionable.
- **Never suppress findings without explanation.** If a finding is a false positive, document why.
- **Re-scan after fixes.** Confirm that fixes actually resolve the findings.
- **Use the right config for the context.** `p/owasp-top-ten` for web code, `p/secrets` for config files, `auto` for general scans.

## Failure Handling

- If Semgrep MCP is unreachable: note it in the contract, proceed without scanning, flag as `confidence: low`.
- If a scan returns no findings: this is valid -- report "0 findings" with confidence level based on config coverage.
- If a scan times out on large directories: narrow the target to specific files or subdirectories.
- If a custom rule has syntax errors: use `semgrep_rule_schema` to validate the rule YAML.

## Outputs / Contract

```
## Semgrep Security Contract
summary: <what was scanned and key findings>
files_scanned: <list of paths or count>
config_used: <semgrep config string>
findings_count: <total findings>
critical_findings:
  - <finding 1: severity, rule, file:line, description>
  - <finding 2: ...>
  - "none" if clean
fixes_applied: <count or "none">
verification: <re-scan confirmed fixes | clean scan | findings documented>
confidence: <high | medium | low>
```

## Examples

### Example 1: Quick scan after writing auth code

**Situation**: Just wrote a login handler

**Step 1**: security_check on the new code
**Result**: 1 WARNING -- SQL injection via string formatting

**Step 2**: Fix the code (use parameterized query)
**Step 3**: Re-scan -- 0 findings

**Contract**:
```
summary: Scanned login handler, found and fixed 1 SQL injection
files_scanned: [telegram/auth.py]
config_used: security_check (built-in)
findings_count: 1 (fixed)
critical_findings: none (after fix)
fixes_applied: 1
verification: re-scan confirmed fix
confidence: high
```

### Example 2: OWASP audit of Telegram module

**Situation**: Pre-deployment security audit

**Step 1**: security_check on telegram/ -- 0 critical
**Step 2**: semgrep_scan with p/owasp-top-ten on telegram/ -- 2 INFO findings (logging sensitive data)

**Contract**:
```
summary: OWASP Top 10 scan of telegram/ module, 2 low-severity findings
files_scanned: [telegram/*.py] (6 files)
config_used: p/owasp-top-ten
findings_count: 2
critical_findings: none
fixes_applied: none (INFO-level, documented)
verification: clean scan at WARNING+ level
confidence: high
```
