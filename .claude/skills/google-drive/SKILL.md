---
name: google-drive
description: "Search, list, upload, and download Google Drive files using the NovaCore Google Workspace CLI. Auto-invoked for file management in Drive."
activation:
  keywords:
    - drive
    - google drive
    - upload
    - download
    - shared drive
    - file share
  when:
    - User asks to find, upload, or download files from Google Drive
    - Task output needs to be shared via Drive
    - Files need to be retrieved from Drive for processing
tool_doctrine:
  drive:
    workflow:
      - authenticate_first
      - search_before_download
      - verify_after_upload
      - respect_file_size_limits
output_contract:
  required:
    - summary
    - action_taken
    - verification
    - confidence
---

# Drive Skill

## When to use

- Searching for files in Google Drive by name or type
- Listing files in a specific folder
- Downloading files from Drive to local disk for processing
- Uploading NovaCore outputs or reports to Drive

## When NOT to use

- Local file operations — use file-ops skill
- Obsidian vault files — use reading-obsidian-memory skill
- Git-tracked files — use git-ops skill

## CLI Reference

All commands output JSON. The CLI is at `tools/google_workspace.py`.

### Search files
```bash
python3 tools/google_workspace.py drive search "quarterly report"
python3 tools/google_workspace.py drive search "budget" --mime-type "application/vnd.google-apps.spreadsheet"
```

### List files
```bash
python3 tools/google_workspace.py drive list
python3 tools/google_workspace.py drive list --folder-id <folder_id> --max-results 30
```

### Download a file
```bash
python3 tools/google_workspace.py drive download <file_id> /home/nova/nova-core/OUTPUT/downloaded_file.pdf
```

### Upload a file
```bash
python3 tools/google_workspace.py drive upload /home/nova/nova-core/OUTPUT/report.pdf --name "Q1 Report" --folder-id <folder_id>
```

## Workflow

1. **Check auth** — run `python3 tools/google_workspace.py auth status`.
2. **Search/list** — find the file by name or browse folder contents.
3. **Download** — save to `OUTPUT/` or a temp directory within `~/nova-core`.
4. **Process** — read/parse the downloaded file as needed.
5. **Upload** (if needed) — push results back to Drive.

## Tool Usage Rules

- Downloads go to `~/nova-core/OUTPUT/` or `~/nova-core/WORK/` — never outside the sandbox.
- Google Docs/Sheets/Slides are exported automatically: Docs→PDF, Sheets→CSV, Slides→PDF.
- Upload file size is limited by the Google Drive API (5 TB max, but practically capped by VPS disk).
- When searching, use specific terms — Drive search is name-based, not full-text.
- The `--mime-type` filter uses Google MIME types:
  - Docs: `application/vnd.google-apps.document`
  - Sheets: `application/vnd.google-apps.spreadsheet`
  - Slides: `application/vnd.google-apps.presentation`
  - Folders: `application/vnd.google-apps.folder`
  - PDF: `application/pdf`

## Failure Handling

| Error | Action |
|-------|--------|
| "Not authenticated" | Direct user to run `python3 scripts/gw-auth.py` |
| "File not found" | Verify file ID; check if file was moved or deleted |
| "Insufficient permissions" | File may not be shared with the authenticated account |
| "Disk full" | Check VPS disk space before downloading large files |
