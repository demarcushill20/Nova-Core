# file-ops Examples

## Example 1: Edit an existing README

**User request:** "Add a Contributing section to README.md"

**Workflow:**

1. Read `README.md` to see current contents.
2. Confirm no Contributing section exists yet.
3. Draft the new section, matching existing heading style.
4. Apply a minimal edit appending the section at the end.
5. Re-read `README.md` to verify the section was added.

**Output Contract:**

```
## CONTRACT
summary: Added Contributing section to README.md
files_changed:
  - README.md (edited)
verification: Re-read confirmed Contributing section present at end of file
```

---

## Example 2: Create a new config file

**User request:** "Create app/config.yml with database host, port, and name"

**Workflow:**

1. Check if `app/config.yml` already exists — it does not.
2. Confirm `app/` directory exists; create it if not.
3. Write `app/config.yml` with the requested keys.
4. Re-read the file to verify contents match intent.

**Output Contract:**

```
## CONTRACT
summary: Created app/config.yml with database configuration
files_changed:
  - app/config.yml (created)
verification: Re-read confirmed file contains host, port, and name keys
```

---

## Example 3: Move a log file to backups

**User request:** "Archive LOGS/worker_old.log to backups"

**Workflow:**

1. Confirm `LOGS/worker_old.log` exists.
2. Confirm destination `LOGS/backups/` exists; create it if not.
3. Check that `LOGS/backups/worker_old.log` does not already exist (no collision).
4. Move the file.
5. Verify source is gone and destination exists.

**Output Contract:**

```
## CONTRACT
summary: Moved worker_old.log to LOGS/backups/
files_changed:
  - LOGS/worker_old.log (moved to LOGS/backups/worker_old.log)
verification: Source removed, destination exists with correct size
```

---

## Example 4: Edit a Python function

**User request:** "Change the timeout in tools/runner.py from 120 to 300"

**Workflow:**

1. Read `tools/runner.py` to find the current timeout value.
2. Locate `timeout = int(args.get("timeout", 120))` on line 289.
3. Apply a minimal edit: change `120` to `300`.
4. Re-read the file to confirm only the timeout value changed.

**Output Contract:**

```
## CONTRACT
summary: Changed default shell timeout from 120s to 300s in tools/runner.py
files_changed:
  - tools/runner.py (edited)
verification: Re-read confirmed timeout default is 300; diff shows single-line change
```

---

## Example 5: Refuse a deletion outside backups

**User request:** "Delete OUTPUT/0005_report.md"

**Workflow:**

1. Check path — `OUTPUT/0005_report.md` is outside `LOGS/backups/`.
2. No explicit user deletion approval was given for this path.
3. **Refuse** the operation.
4. Suggest moving to `LOGS/backups/` instead, or ask the user to explicitly approve deletion.

**Output Contract:**

```
## CONTRACT
summary: Refused deletion of OUTPUT/0005_report.md (outside LOGS/backups/, no explicit approval)
files_changed: []
verification: File confirmed still present at original path
```
