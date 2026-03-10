# Heartbeat Checklist

Run through this checklist each heartbeat cycle. For each item, use your judgment
to decide if action is needed RIGHT NOW. If nothing needs attention, respond
with exactly: HEARTBEAT_OK

## Task Queue
- Any TASKS/*.md files older than 30 minutes that are still pending (not picked up)?
- Any .inprogress files older than 20 minutes (stuck worker)?
- Any .failed files from the last hour that haven't been retried?

## Follow-ups
- Any open_threads from the last Fusion Memory checkpoint that need attention?
- Any recently completed tasks in OUTPUT/ that the operator should know about?

## Proactive
- If idle for 6+ hours with no tasks, note it (but don't spam the operator)
- If a background research task finished recently, prepare a brief summary
- Check if any goals in STATE/goals.json are stale or need progress

## System Health
- Are all services healthy? (pre-checked — see system state below)
- Any disk usage concerns? (pre-checked — see system state below)
- Any unusual failure rates in metrics?

## Response Format
If action is needed, respond with a JSON array of actions:
```json
[
  {"type": "notify", "message": "Brief message for the operator"},
  {"type": "task", "title": "Task title", "body": "Task description"}
]
```

If nothing needs attention: respond with exactly HEARTBEAT_OK
