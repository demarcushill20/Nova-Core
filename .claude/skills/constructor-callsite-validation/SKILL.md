---
name: constructor-callsite-validation
description: "Validate that diagnostic/test code instantiates domain objects with correct constructor kwargs matching the current dataclass/model signature."
activation:
  keywords:
    - constructor mismatch
    - wrong kwarg
    - unexpected keyword argument
    - callsite validation
    - action vs side
    - field name mismatch
    - dataclass signature
    - model constructor
    - TypeError constructor
    - diagnostic instantiation
  when:
    - Test or diagnostic code fails with TypeError about unexpected keyword arguments
    - A domain model's constructor signature has changed but call sites were not updated
    - Diagnostic utilities instantiate domain objects with legacy or assumed field names
    - Code review reveals kwarg names that don't match the target dataclass/Pydantic model
tool_doctrine:
  discovery:
    workflow:
      - read_model_definition
      - grep_all_constructor_callsites
      - diff_kwargs_vs_signature
  repair:
    workflow:
      - targeted_kwarg_rename
      - add_enum_import_if_needed
      - verify_instantiation_succeeds
output_contract:
  required:
    - model_class
    - mismatched_callsites
    - applied_fixes
    - verification_result
---

# Constructor Call-Site Validation

Detect and fix constructor-call-site mismatches where diagnostic, test, or glue code instantiates domain objects (dataclasses, Pydantic models, NamedTuples) using kwargs that don't match the current class signature.

## Why This Exists

Domain models evolve — fields get renamed (`action` → `side`), types change (`str` → `OrderSide.BUY`), fields are added or removed. Test fixtures, diagnostic scripts, and helper factories written against an older API silently break or pass wrong data. Python dataclasses raise `TypeError` at runtime, but Pydantic models may silently drop or alias fields, hiding the bug until production.

This pattern was first observed when `utils/broker_diagnostic.py` passed `action='buy'` to `OrderRequest(...)` which expects `side=OrderSide.BUY` — a two-axis mismatch (wrong kwarg name AND wrong value type).

## Step-by-Step

### 1. Identify the Canonical Model Signature

- Read the model definition file (e.g., `novatrade/models.py`).
- Extract the full list of constructor kwargs, their types, and defaults.
- Note required vs optional fields.
- Note enum-typed fields that require specific enum values (not raw strings).

### 2. Find All Call Sites

- Grep the codebase for all instantiations of the model class:
  ```
  grep -rn "ModelName(" --include="*.py"
  ```
- Include test files, diagnostic scripts, CLI commands, fixtures, and factory functions.
- Pay special attention to files outside the core module (tests/, utils/, scripts/, cli/) — these are the most likely to drift.

### 3. Validate Each Call Site

For every call site, check:

| Check | Example Failure | Fix |
|---|---|---|
| **Kwarg name exists** in model signature | `action='buy'` → model has `side`, not `action` | Rename kwarg to `side` |
| **Value type matches** field type | `side='buy'` → field is `OrderSide`, not `str` | Change to `side=OrderSide.BUY` |
| **Required fields present** | Missing `order_type=` | Add the required kwarg |
| **No extra kwargs** | `timeout=30` but model has no `timeout` | Remove the kwarg or update the model |
| **Enum member valid** | `OrderSide.LONG` → enum only has `BUY`/`SELL` | Use valid enum member |

### 4. Apply Fixes

- Use `Edit` with precise `old_string` → `new_string` for each call site.
- Add missing imports (e.g., `from novatrade.models import OrderSide`) if an enum wasn't previously imported.
- Prefer one fix per call site for clean diffs.

### 5. Verify

- Run the affected test file(s) to confirm `TypeError` is resolved.
- If no test covers the call site, add a minimal smoke test that instantiates the object.
- Confirm `pytest` passes for all modified files.

## Expected Inputs

- A `TypeError` traceback or a code review finding showing a constructor kwarg mismatch.
- OR a model class name whose call sites should be audited proactively.

## Expected Outputs

- `model_class`: The fully-qualified class name audited (e.g., `novatrade.models.OrderRequest`).
- `mismatched_callsites`: List of `{file, line, old_kwargs, new_kwargs, mismatch_type}` describing each bad call site found.
- `applied_fixes`: List of `{file, line, old_code, new_code}` for each repair applied.
- `verification_result`: `pass` or `fail` with test command and output summary.

## Error Handling

- If a call site uses `**kwargs` or dynamic unpacking, flag it for manual review — static analysis cannot validate these.
- If the model uses `__init__` overrides or custom `__post_init__` that accept extra args, read those before flagging call sites as wrong.
- If more than 10 call sites are broken, consider whether the model itself was incorrectly changed — check git blame on the model before bulk-fixing callers.
- If an enum import would create a circular dependency, use a string literal with a `# type: ignore` and flag for architectural review.

## Anti-Patterns (Do NOT)

- **Do NOT** change the model signature to match broken callers — callers conform to the model, not the reverse.
- **Do NOT** add compatibility shims (`action` aliased to `side`) — fix call sites directly.
- **Do NOT** use raw strings for enum fields (e.g., `side="BUY"`) — always use the enum member (`side=OrderSide.BUY`).
- **Do NOT** skip test/diagnostic files assuming they're "not production" — broken tests mask real bugs.
- **Do NOT** bulk find-and-replace without reading each call site — context matters (e.g., some files may use a different `OrderRequest` class).

## Proactive Use

When any domain model signature changes (field added, renamed, or retyped), proactively run this validation on all call sites before committing. A 2-minute grep now prevents a 20-minute debugging session later.

## Origin

Captured from `shift_20260401_2_novatrade_progress`: `utils/broker_diagnostic.py` passed `action='buy'` to `OrderRequest(...)` which requires `side=OrderSide.BUY` — a two-axis mismatch (wrong kwarg name + wrong value type). The fix was a targeted kwarg rename plus enum import.

---

I was blocked by permissions writing to `.claude/skills/`. The complete SKILL.md content is above — improved the name from the overly-specific suggested name to **`constructor-callsite-validation`** which is reusable across any domain model, not just the `OrderRequest`/`action→side` case. Key design choices:

- **Two-axis validation**: checks both kwarg names AND value types (the original bug had both wrong)
- **Proactive trigger**: can be invoked after any model signature change, not just on failure
- **Anti-pattern guardrails**: explicitly blocks the temptation to add backwards-compat shims or change models to match broken callers
- **Graduated escalation**: >10 broken sites → check if model was wrong; `**kwargs` → manual review
