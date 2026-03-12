---
name: meeting-prep
description: "Prepare for your next meeting: pull agenda, attendees, linked docs, and recent email threads from the attendees. Use when the user says 'prep for my meeting', 'what's my next meeting', 'get ready for the call', or needs context before a scheduled event."
argument-hint: "[event-id-or-keyword]"
disable-model-invocation: false
activation:
  keywords: [meeting prep, prepare for meeting, next meeting, get ready for call, meeting agenda, meeting context, before the meeting]
  when:
    - User is about to join a meeting and needs context
    - User asks what their next meeting is about
    - User wants attendee info and linked documents
    - Pre-meeting preparation routine
tool_doctrine:
  meeting_prep:
    workflow:
      - authenticate_first
      - read_only_never_modify
      - fetch_calendar_then_enrich
      - cross_reference_email_threads
      - present_actionable_briefing
output_contract:
  required:
    - summary
    - meeting_title
    - attendees
    - action_taken
    - verification
    - confidence
---

# Meeting Prep

Inspired by [Google Workspace CLI workflow+meeting-prep](https://github.com/googleworkspace/cli).

## When to use

- Before any scheduled meeting: "prep me for my 2pm call"
- Morning scan: "what meetings do I have today?"
- Context gathering: "who's in the sprint review?"

## When NOT to use

- Creating or modifying calendar events (use google-calendar)
- Sending meeting invites (use google-calendar with attendees)

## Workflow

### Step 1 — Get upcoming events

```bash
python3 tools/google_workspace.py calendar list --days 1
```

For a specific meeting, search by keyword:
```bash
python3 tools/google_workspace.py calendar list --days 7
```
Then filter results by title match.

### Step 2 — Extract meeting details

From the event JSON, pull:
- **Title** and **time**
- **Attendees** (names and emails)
- **Description** (often contains agenda, Zoom links, doc links)
- **Location** (physical or virtual meeting link)

### Step 3 — Find related emails (optional enrichment)

Search for recent emails from/to the meeting attendees:

```bash
python3 tools/google_workspace.py gmail search "from:attendee@example.com newer_than:7d" --max-results 5
```

This surfaces last-minute agenda changes, pre-reads, or context.

### Step 4 — Check for linked documents (optional enrichment)

If the event description contains Google Doc/Sheet/Slide links, extract the document IDs and fetch summaries:

```bash
python3 tools/google_workspace.py docs read <doc_id>
```

### Step 5 — Present the briefing

Structure the output as an actionable brief:

```
## Meeting Brief: [Title]
**When**: [Date/Time]
**Where**: [Location/Link]

### Attendees
- Name (email) — role if known

### Agenda
- [from event description or "No agenda provided"]

### Recent Context
- [Email thread summaries from attendees]

### Linked Documents
- [Doc title — key highlights]

### Suggested Prep
- [Review X document]
- [Prepare answer for Y topic]
```

## Tool Usage Rules

- **Read-only.** Never modify events, docs, or emails.
- **Default to next upcoming event.** If user doesn't specify, prep for the soonest event.
- **Email enrichment is best-effort.** If no recent emails from attendees, skip gracefully.
- **Don't overwhelm.** Cap email context to 3 most recent per attendee.
- **Extract meeting links.** Look for Zoom, Google Meet, Teams URLs in description.

## Failure Handling

| Error | Action |
|-------|--------|
| No upcoming events | Report "no meetings scheduled" |
| Calendar not authenticated | Direct to `python3 scripts/gw-auth.py` |
| No emails from attendees | Skip enrichment, note in contract |
| Doc link inaccessible | Note "linked doc not accessible" |

## Outputs / Contract

```
## Meeting Prep Contract
summary: <briefing for [meeting title] at [time]>
meeting_title: <title>
meeting_time: <ISO datetime>
attendees: <count and key names>
agenda_found: <yes | no — from event description>
email_context: <enriched | skipped | not authenticated>
linked_docs: <count or "none">
action_taken: read-only calendar + email + docs scan
verification: <confirmed via calendar list output>
confidence: <high | medium | low>
```
