# Memory Autonomy Policy (Phase 10, Step 10.6)

## Purpose

This document defines what Nova-Core's memory system is allowed to do
autonomously, what requires operator approval, and what must never be
automated. It reflects the real implemented behavior after Phases 0–9.

---

## Fully Autonomous (No Approval Required)

These operations happen automatically during normal runtime:

| Operation | Phase | Trigger | Guardrails |
|-----------|-------|---------|------------|
| Ingest memory candidate | 1–4 | Any runtime event | Schema validation, evaluation scoring |
| Working memory write | 1–3 | Heartbeat, session events | 7-day auto-cleanup, layer enforcement |
| Episodic artifact write | 2–4 | Task completion, plan events | Importance/novelty/durability scoring |
| Candidate evaluation | 4 | Every store() call | Deterministic scoring, rejection thresholds |
| Intent classification | 5 | Every recall() call | Regex-based signals, routing table |
| Recall from file stores | 1–5 | Any recall query | Multi-factor ranking, deduplication |
| Working memory cleanup | 1, 8 | Store operations, governance sweep | 7-day retention, mtime-based |
| Open loop detection | 7 | task_failed, session_end events | Conservative detection, dedupe key |
| Stale loop marking | 7–8 | Governance sweep | 14-day threshold on updated_at |
| Notification pruning | 8 | Governance sweep | 14-day retention |
| Rotated log pruning | 8 | Governance sweep | 30-day retention |
| Exact duplicate dedup | 9 | Compaction sweep | Content hash, archive older copy |
| Supersession marking | 9 | Compaction sweep | Same workflow_id, metadata only |

## Allowed with Dry-Run First

These operations modify artifacts and should run in dry-run mode before execution:

| Operation | Phase | What Changes | Safeguard |
|-----------|-------|-------------|-----------|
| Governance sweep (execute) | 8 | Archives, prunes, marks stale | Dry-run default, protection checks |
| Compaction sweep (execute) | 9 | Archives duplicates, supersedes | Dry-run default, protection checks |
| Session archival | 8 | Moves old sessions to _archive/ | 60-day threshold |
| Operational state archival | 8 | Moves old STATE/ files to _archive/ | 30-day threshold, protection check |
| Thin artifact archival | 8 | Archives low-content episodic artifacts | 90-day + thin summary check |

## Requires Operator Triage (Not Auto-Executed)

These produce candidates or flags but do NOT auto-execute:

| Operation | Phase | What is Produced | Operator Action Needed |
|-----------|-------|-----------------|----------------------|
| Promotion eligibility flagging | 4 | `promotion_eligibility: "eligible"` | Operator decides whether to promote |
| Pattern candidate extraction | 6 | PatternCandidate metadata | Operator reviews for Obsidian promotion |
| ADR candidate detection | 6, 10 | Candidate count in metrics | Operator creates ADR in Obsidian |
| Near-duplicate ambiguous match | 9 | `rejected_ambiguous` result | Operator reviews whether to merge |
| Cross-store duplicate detection | 9 | Detection only | Manual resolution required |
| Obsidian vault writes | — | Via MCP vault_write | Schema validation, folder restrictions |
| Fusion Memory writes | — | Via prompt delegation | Claude subprocess handles |

## Never Automated

These must NEVER happen without explicit human instruction:

| Operation | Reason |
|-----------|--------|
| Delete procedural memory (ADRs, patterns) | Highest-value knowledge |
| Delete semantic memory (verified facts) | Consolidated truth |
| Merge active open loops | Each represents distinct lifecycle state |
| Modify operator-authored artifacts | Human judgment, not machine-generated |
| Override protection flags | Explicit operator-set safety markers |
| Rewrite historical records | Append-only audit trail integrity |
| Push to external systems | Requires human review |
| Cross-project memory transfer | Scope isolation |
| Auto-promote to procedural layer | Requires 2+ evidence contexts + operator approval |

## Protection Levels (Implemented)

| Level | Auto-Modify | Auto-Archive | Auto-Compact | Auto-Delete |
|-------|------------|-------------|-------------|------------|
| Protected | No | No | No | No |
| High | No | No | No | No |
| Medium | No | Possible (90d+ thin) | No | No |
| Low | Yes (per rules) | Yes | Yes | Yes (per rules) |
| None | Yes | Yes | Yes | Yes |

## Confidence Requirements

| Action | Min Confidence |
|--------|---------------|
| Store to working layer | Any |
| Store to episodic layer | Summary ≥ 20 chars, importance ≥ 0.3 |
| Flag as promotion-eligible | Importance ≥ 0.5, durability ≥ 0.5 |
| Mark stale | Any (rule-based) |
| Archive | Low |
| Compact / dedup | Medium |
| Prune | High (all criteria met) |
| Supersede | Medium (workflow_id match) |

## Summary

Nova-Core's memory system follows a "conservative autonomy" model:
1. **Create freely** — Writing to working/episodic layers is fully autonomous
2. **Evaluate always** — Every candidate is scored before storage
3. **Recall smartly** — Intent-aware routing with multi-factor ranking
4. **Clean cautiously** — Governance and compaction default to dry-run
5. **Protect strictly** — High-value memory is never auto-modified
6. **Promote never** — Layer promotion requires operator triage
7. **Delete rarely** — Only transient/expired artifacts with explicit rules
