# Phase 0 Test Plan

Generated: 2026-03-13

---

## Test Infrastructure

- **Framework**: pytest 9.0.2
- **Config**: pyproject.toml (addopts: `-x -q --tb=short`, timeout: 120s)
- **CI**: `.github/workflows/guardrails.yml` runs `scripts/check-guardrails.sh --all` then `pytest tests/ -q`
- **Total tests**: 3275 across 76 test files

## Commands

```bash
# Run all Phase 0-relevant tests
python3 -m pytest tests/test_mcp_vault_server.py tests/test_vault_schema_standalone.py tests/test_repair_vault_types.py -v

# Run broader memory tests
python3 -m pytest tests/test_memory_engine.py tests/test_vault_context.py tests/test_vault_sync.py -v

# Full regression suite
python3 -m pytest tests/ -q

# Vault repair audit (dry-run)
python3 scripts/repair_vault_types.py --verbose
```

## Phase 0 Coverage Map

| Area | Test File | Count | Covers |
|------|-----------|-------|--------|
| MCP vault server | test_mcp_vault_server.py | 196 | Path safety, frontmatter parsing, read/search/list, write/update with schema validation, sensitive content, feature flags, ownership |
| Standalone schema | test_vault_schema_standalone.py | 73 | All 7 canonical types, rejection paths, ADR validation, plan optional fields, enum completeness, MCP server sync |
| Repair script | test_repair_vault_types.py | 33 | YAML parsing, status mapping, fixability detection, scan_vault, apply_fix safety |
| Memory engine | test_memory_engine.py | 36 | Artifact validation, writes, retrieval, capture |
| Vault context | test_vault_context.py | 48 | Eligibility, keyword extraction, injection, source-truth boundaries |
| Vault sync | test_vault_sync.py | 24 | Config, daemon/oneshot modes, fail-open, audit contracts |

**Total Phase 0-relevant: 410 tests**

## Coverage Gaps

| Gap | Reason | Risk |
|-----|--------|------|
| Fusion Memory MCP writes | Prompt-delegated to Claude subprocess; cannot unit test without mocking entire LLM | LOW — Fusion Memory MCP server validates server-side |
| OUTPUT/ writes | Prompt-delegated to Claude subprocess | LOW — contract validation catches malformed output |
| Live vault repair (--repair) | Would require real vault; dry-run tested via scan_vault | LOW — atomic writes, operator-invoked |
| Vault audit log format | Advisory only, non-binding | NEGLIGIBLE |
