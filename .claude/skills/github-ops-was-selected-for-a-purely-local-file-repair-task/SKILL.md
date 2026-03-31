The sandbox is blocking directory creation under `.claude/skills/`. Here's the complete SKILL.md content:

---
name: output-repair
description: "Repair broken, truncated, or malformed output files, contract blocks, and local artifacts — purely local file surgery with zero remote/GitHub context."
activation:
  keywords:
    - repair
    - fix
    - broken
    - truncated
    - malformed
    - corrupt
    - contract block
    - output file
    - missing block
    - incomplete output
    - restore section
    - patch file
    - local repair
    - file integrity
  when:
    - Task involves repairing or fixing a local file (OUTPUT/, LOGS/, MEMORY/, TASKS/)
    - A contract block is missing, truncated, or malformed in an output file
    - A JSON, YAML, or Markdown artifact has structural damage
    - The task mentions no GitHub repo, PR, issue, branch, or remote — purely local
  negative_signals:
    - github
    - repo
    - pull request
    - pr
    - issue (in GitHub context)
    - ci
    - remote
    - push
    - branch
    - merge
tool_doctrine:
  files:
    workflow:
      - read_damaged_file
      - diagnose_damage
      - compute_repair_diff
      - apply_repair
      - verify_integrity
output_contract:
  required:
    - damage_diagnosis
    - repair_applied
    - verification
    - files_changed
    - confidence
---

# Output Repair

Repair broken, truncated, or malformed local files. This skill handles structural damage to output files, contract blocks, JSON/YAML/Markdown artifacts, and other local file integrity issues.

**Key distinction**: This skill is for purely local file operations. If the task involves GitHub repos, PRs, issues, or remote operations, use `github-ops` instead. If the task is general file CRUD (create, move, rename), use `file-ops` instead.

## When To Use

- An OUTPUT/ file has a missing or truncated `## CONTRACT` block
- A JSON file has broken syntax (unclosed braces, trailing commas, encoding issues)
- A YAML file has indentation damage or missing required fields
- A Markdown artifact has structural corruption (broken frontmatter, missing sections)
- A MEMORY/ or TASKS/ file needs section repair
- Any local file needs surgical fixes to restore structural integrity
- **No GitHub, remote, or CI context is present in the task**

## Workflow

1. **Read the damaged file** — use the Read tool to inspect full contents. Never guess at damage without reading first.

2. **Diagnose the damage** — identify exactly what is broken:
   - Missing section (e.g., `## CONTRACT` block absent)
   - Truncation (file cuts off mid-content)
   - Syntax error (broken JSON/YAML/TOML structure)
   - Encoding corruption (mojibake, null bytes)
   - Malformed frontmatter (missing `---` delimiters, bad YAML)

3. **Determine repair strategy**:
   - **Missing section**: reconstruct from context (other fields, filename conventions, task metadata)
   - **Truncation**: if recoverable content exists (e.g., in logs or memory), restore it; otherwise, add a `[TRUNCATED — unrecoverable]` marker
   - **Syntax error**: apply minimal fix (close braces, fix commas, correct indentation)
   - **Encoding**: strip bad bytes, normalize to UTF-8
   - **Frontmatter**: reconstruct valid YAML between `---` delimiters

4. **Compute minimal diff** — change only the damaged portion. Do not reformat or restructure undamaged content.

5. **Apply the repair** — use the Edit tool for surgical fixes. Use Write only for complete rewrites when damage is pervasive.

6. **Verify integrity** — re-read the file and confirm:
   - For JSON: valid parse
   - For YAML: valid parse, required fields present
   - For Markdown with contract: `## CONTRACT` block present with all required fields
   - For frontmatter: valid `---` delimiters, parseable YAML

## Expected Inputs

- A file path (absolute or relative to `~/nova-core`)
- Description of the problem (optional — skill will diagnose if not provided)
- Expected structure or schema (optional — skill infers from file type and conventions)

## Expected Outputs

```
## CONTRACT
damage_diagnosis: <what was broken and why>
repair_applied: <what was changed, as a minimal description>
verification: <how integrity was confirmed post-repair>
files_changed:
  - <path> (<action: repaired|restored|reconstructed>)
confidence: <high | medium | low>
```

## Error Handling

- **File not found**: report clearly, do not create a placeholder file
- **Ambiguous damage**: if multiple valid repairs exist, pick the most conservative one and note alternatives
- **Unrecoverable content**: mark with `[UNRECOVERABLE]` rather than fabricating content
- **Binary file**: refuse to repair in-place; report file type and suggest alternatives
- **Path outside sandbox**: refuse the operation

## Skill Selection Guidance

This skill should be selected when ALL of these are true:
1. The task involves fixing/repairing/restoring a local file
2. No GitHub repo, PR, issue, or remote context is mentioned
3. The damage is structural (not a logic or code bug)

Do NOT select this skill when:
- The task is about creating a GitHub issue about a broken file (use `github-ops`)
- The task is routine file creation/editing with no damage (use `file-ops`)
- The task requires running code to fix itself (use `shell-ops` or `task-execution`)
