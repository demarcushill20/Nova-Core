---
name: google-docs
description: "Read, create, and edit Google Docs and Sheets using the NovaCore Google Workspace CLI. Auto-invoked for document and spreadsheet operations."
activation:
  keywords:
    - google docs
    - google doc
    - google sheets
    - spreadsheet
    - document
    - gdoc
  when:
    - User asks to read or create a Google Doc
    - Task requires writing content to a Google Doc
    - Spreadsheet data needs to be read or written
tool_doctrine:
  docs:
    workflow:
      - authenticate_first
      - read_before_edit
      - verify_after_write
      - cap_content_length
output_contract:
  required:
    - summary
    - action_taken
    - verification
    - confidence
---

# Docs & Sheets Skill

## When to use

- Reading content from a Google Doc
- Creating new Google Docs with content
- Appending text to existing Google Docs
- Reading spreadsheet data from Google Sheets
- Writing data to Google Sheets
- Creating new spreadsheets

## When NOT to use

- Local markdown files — use file-ops skill
- Obsidian notes — use reading-obsidian-memory skill
- PDF generation — use generate_pdf_report skill

## CLI Reference — Docs

All commands output JSON. The CLI is at `tools/google_workspace.py`.

### Read a document
```bash
python3 tools/google_workspace.py docs read <document_id>
```

### Create a document
```bash
python3 tools/google_workspace.py docs create --title "Meeting Notes" --body "# Sprint Review\n\nAttendees: ..."
```

### Append to a document
```bash
python3 tools/google_workspace.py docs append <document_id> --text "\n\n## New Section\nAdditional content..."
```

## CLI Reference — Sheets

### Read spreadsheet data
```bash
python3 tools/google_workspace.py sheets read <spreadsheet_id>
python3 tools/google_workspace.py sheets read <spreadsheet_id> --range "Sheet1!A1:D10"
```

### Write data to spreadsheet
```bash
python3 tools/google_workspace.py sheets write <spreadsheet_id> \
  --range "Sheet1!A1" \
  --values '[["Name","Score"],["Alice","95"],["Bob","87"]]'
```

### Create a spreadsheet
```bash
python3 tools/google_workspace.py sheets create --title "Project Tracker"
```

## Workflow

1. **Check auth** — run `python3 tools/google_workspace.py auth status`.
2. **Identify document** — get the document or spreadsheet ID (from URL or Drive search).
3. **Read first** — always read current content before editing.
4. **Execute operation** — create, append, or write data.
5. **Verify** — re-read to confirm changes applied.

## Tool Usage Rules

- Document IDs are the long alphanumeric string in the Google Docs/Sheets URL.
  - Docs URL: `https://docs.google.com/document/d/<DOCUMENT_ID>/edit`
  - Sheets URL: `https://docs.google.com/spreadsheets/d/<SPREADSHEET_ID>/edit`
- Document text is capped at 20,000 characters on read. Note truncation if longer.
- Sheets data is capped at 500 rows on read. Use `--range` for specific sections.
- For Sheets write, `--values` must be a JSON array of arrays (rows of cells).
- Always read a document before appending to understand current content.
- Text appended to Docs goes at the end of the document.

## Failure Handling

| Error | Action |
|-------|--------|
| "Not authenticated" | Direct user to run `python3 scripts/gw-auth.py` |
| "Document not found" | Verify document ID from the URL |
| "Permission denied" | Document may not be shared with authenticated account |
| "Invalid range" | Check sheet name and cell range format (e.g., `Sheet1!A1:B2`) |
| "Invalid JSON" | Ensure `--values` is valid JSON array of arrays |
