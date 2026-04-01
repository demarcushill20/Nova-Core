---
name: contract-field-repair
description: "Repair output contract violations with minimal field-level patches instead of full-block regeneration."
activation:
  keywords:
    - contract
    - field mismatch
    - missing field
    - field rename
    - output contract
    - contract repair
    - validation error
    - schema mismatch
  when:
    - Output contract validation fails due to field name mismatches
    - A required contract field is present under a wrong name
    - Contract block has correct data but incorrect field keys
    - Task retry triggered by contract validation failure
tool_doctrine:
  diagnosis:
    workflow:
      - read_contract_spec
      - read_actual_output
      - diff_field_names
      - identify_mismatch
  repair:
    workflow:
      - targeted_field_rename
      - verify_contract_passes
      - never_regenerate_full_block
output_contract:
  required:
    - mismatched_fields
    - applied_patches
    - validation_result
---

# Contract Field Repair

Minimal-diff repair for output contract violations. When a contract check fails, diagnose the exact field-level mismatch and apply a surgical rename — never regenerate the entire contract block.

## Why This Exists

Full-block regeneration is wasteful and risky: it discards correct data, introduces regressions, and burns tokens. Most contract failures are simple field-name mismatches (e.g., `files_modified` vs `files_changed`) where the data is already correct — only the key needs renaming.

## Step-by-Step

### 1. Diagnose the Mismatch
- Read the **expected** contract schema (from the task spec or skill's `output_contract.required` list).
- Read the **actual** output block that failed validation.
- Diff the field names. Identify which required fields are missing and which unexpected fields are present.
- Check for semantic equivalents (e.g., `files_modified` ↔ `files_changed`, `error_count` ↔ `errors`).

### 2. Classify the Failure
| Failure Type | Action |
|---|---|
| Field renamed (data correct, key wrong) | Rename the key in-place |
| Field missing (no equivalent exists) | Add the field with appropriate default or computed value |
| Field type mismatch (string vs list) | Cast/wrap the value to match the schema |
| Structural mismatch (nested vs flat) | Restructure minimally — do NOT regenerate |

### 3. Apply the Patch
- Use `Edit` tool with exact `old_string` → `new_string` targeting only the mismatched field name.
- **Never** replace the entire contract/output block.
- **Never** re-run the full task to regenerate output.
- If multiple fields are wrong, apply one patch per field.

### 4. Verify
- Re-read the patched output.
- Validate all `output_contract.required` fields are now present with correct names.
- Confirm no data was lost or corrupted by the patch.

## Expected Inputs
- A failed contract validation result (field names expected vs actual).
- The file path containing the output block.
- The contract schema (from SKILL.md or task spec).

## Expected Outputs
- `mismatched_fields`: List of `{expected, actual, action}` tuples describing each mismatch found.
- `applied_patches`: List of `{field, old_name, new_name, file, line}` for each rename applied.
- `validation_result`: `pass` or `fail` after patches applied.

## Error Handling
- If more than 3 fields are mismatched in a single block, flag for manual review — the output may have been generated against a wrong schema version.
- If the actual output contains no semantic equivalent for a required field, escalate rather than fabricating data.
- If the output file cannot be read or parsed, fall back to the standard task-execution retry flow.

## Anti-Patterns (Do NOT)
- **Do NOT** regenerate the full output block to fix a single field name.
- **Do NOT** re-execute the entire task when only the contract envelope is wrong.
- **Do NOT** silently drop fields that don't match — every field must be accounted for.
- **Do NOT** guess field mappings without checking semantic equivalence first.

## Origin
Captured from pattern observed in `shift_20260331_16_evening_wrap__retry1`: agent diagnosed a `files_modified` vs `files_changed` mismatch and applied a single targeted rename, avoiding full-block regeneration. This saved tokens, preserved correct data, and reduced retry latency.
