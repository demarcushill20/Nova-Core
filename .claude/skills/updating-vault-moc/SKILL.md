---
name: updating-vault-moc
description: >
  Append new notes to an existing Map of Content (MOC) in the vault.
  Uses vault_update to add a dated section with annotated wikilinks
  for recently created notes not yet in the MOC. Invoke when new notes
  have been added to a domain since the MOC was last updated.
activation:
  keywords:
    - update moc
    - refresh moc
    - add to moc
    - moc update
allowed-tools:
  - mcp__nova-vault__vault_read
  - mcp__nova-vault__vault_search
  - mcp__nova-vault__vault_update

tool_doctrine:
  moc_must_exist: >
    The MOC must already exist. If not found, report "no MOC found —
    use generating-vault-moc to create one" and stop.
  no_duplicate_links: >
    Read the existing MOC content and do not add notes that are already
    linked. Only append genuinely new notes.
  append_only: >
    Use vault_update (append-only). Never rewrite or restructure the
    existing MOC content.
  bounded_execution: >
    Max 5 tool calls per invocation.

output_contract:
  required_sections:
    - update_result: >
        - moc_path: <path>
          status: updated | skipped
          notes_added: count
          reason: (if skipped)
  format: >
    Brief summary of what was added.
---

# Updating a Vault MOC

Append recently created notes to an existing MOC.

## When to Use

- New notes have been added to a domain since the MOC was last updated
- The graph hygiene skill detected a stale MOC
- After a batch of workflow learnings or patterns have been captured

## When NOT to Use

- No MOC exists yet — use `generating-vault-moc` first
- You want to restructure the MOC — manual operator task
- The MOC is in a read-only folder — cannot update

## Inputs

- **moc_path**: Path to the existing MOC (e.g., `00-inbox/moc-novatrade.md`). Required.
- **new_notes** (optional): List of specific note paths to add. If omitted,
  auto-discovers recent notes in the domain.

## Workflow

```
1. READ EXISTING MOC
   - vault_read(moc_path) to get current content
   - If not found, report "MOC not found" and stop
   - Extract domain from frontmatter
   - Parse existing wikilinks to build an "already linked" set

2. DISCOVER NEW NOTES
   - If new_notes provided, use those
   - Otherwise: vault_search with domain keywords
   - Filter out notes already linked in the MOC

3. COMPOSE UPDATE SECTION
   - Build a dated section:
     ## Recent Additions (YYYY-MM-DD)
     - [[new-note-1]] — <annotation>
     - [[new-note-2]] — <annotation>
   - If no new notes to add, report "MOC is up to date" and stop

4. APPEND TO MOC
   - vault_update(moc_path, section_content)
   - Report success with count of notes added
```

## Rules

1. **MOC must exist.** Do not create — only update.
2. **No duplicate links.** Parse existing content before appending.
3. **Append-only.** Use vault_update, never vault_write.
4. **Annotated links.** Every new `[[wikilink]]` must have a `—` annotation.
5. **Max 5 tool calls.** Keep updates lightweight.
6. **Date the section.** Always include the date in the section heading.

## Failure Handling

| Situation | Action |
|-----------|--------|
| MOC not found | Report "use generating-vault-moc" |
| No new notes to add | Report "MOC is up to date" |
| vault_update fails | Report error, do not retry |
| All discovered notes already linked | Report "MOC is up to date" |
