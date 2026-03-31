# Ship: Checkpoint → Commit → Push

Save a Fusion Memory checkpoint, commit all changes, and push to remote. Run this at the end of a work session or after completing a significant milestone.

## Steps

### 1. Fusion Memory Checkpoint

Create a session checkpoint using the `memory-checkpoint` skill:

- Call `mcp__nova-memory__get_last_checkpoint` with `{"project": "nova-core"}` to get the previous session boundary
- Call `mcp__nova-memory__get_recent_events` with `{"n": 20, "project": "nova-core"}` to gather session context
- Compose a factual 2-5 sentence summary of what was accomplished this session
- Identify open threads (unfinished work) and next actions
- Call `mcp__nova-memory__create_checkpoint` with session_id `session-YYYY-MM-DD` (or `session-YYYY-MM-DD-N` if multiple today), the summary, open_threads, and next_actions
- Verify the checkpoint was stored via `mcp__nova-memory__get_last_checkpoint`

### 2. Git Commit

Stage and commit all changes:

- Run `git status` and `git diff` to review what's changed
- Run `git log --oneline -5` to match commit message style
- Stage relevant files with `git add <specific-paths>` (never `git add -A` blindly — exclude secrets, .env, credentials)
- Write a commit message following `<type>: <summary>` convention (feat, fix, docs, test, refactor, chore)
- Commit using a HEREDOC for proper formatting, include `Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>`
- Verify with `git status` after commit

### 3. Git Push

Push to remote:

- Run `git push`
- Verify with `git log --oneline -1` that the commit is on remote

### 4. Report

Output a brief summary:

```
## Shipped
- Checkpoint: <session_id> (seq: <last_event_seq>)
- Commit: <hash> <message>
- Push: <branch> → origin
```

## Important

- If there are no changes to commit, skip steps 2-3 and only create the checkpoint
- If the checkpoint fails, still proceed with commit and push — report the checkpoint failure
- Never force-push. Never skip pre-commit hooks
- Do not ask for confirmation — full YOLO mode
