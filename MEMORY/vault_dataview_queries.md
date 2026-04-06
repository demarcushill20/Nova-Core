# Vault Dataview Queries

Reference queries for exploring the Nova-Core Obsidian vault using the
Dataview plugin. These queries use DQL (Dataview Query Language).

## Navigating the MOC Hierarchy

### All notes pointing up to a specific MOC
```dataview
LIST
FROM [[moc-novatrade]]
SORT file.name ASC
```

### MOC index — all MOCs in the vault
```dataview
TABLE domain, date_created, length(file.inlinks) AS "Linked Notes"
FROM #type/moc
SORT domain ASC
```

## Patterns & Learnings

### All patterns by confidence
```dataview
TABLE confidence, agent_role, date_created, up
FROM "20-agent-patterns"
SORT confidence DESC
```

### All learnings by date
```dataview
TABLE task_class, confidence, date
FROM "30-workflow-learnings"
SORT date DESC
```

### Learnings by task class
```dataview
TABLE confidence, date, title
FROM "30-workflow-learnings"
WHERE task_class = "code_impl"
SORT date DESC
```

## Research

### All research summaries by topic
```dataview
TABLE topic, sources_count, confidence, date_researched
FROM "40-research"
SORT date_researched DESC
```

## Graph Health

### Orphan finder — notes with no outgoing links
```dataview
LIST
FROM "40-research"
WHERE length(file.outlinks) = 0
```

### Orphan finder — notes with no incoming links
```dataview
LIST
FROM "20-agent-patterns"
WHERE length(file.inlinks) = 0
```

### Stale active notes — not modified in 30+ days
```dataview
TABLE file.mtime AS "Last Modified", type
FROM ""
WHERE status = "active" AND file.mtime < date(today) - dur(30d)
SORT file.mtime ASC
```

### Notes without domain tags
```dataview
LIST
FROM "30-workflow-learnings" OR "20-agent-patterns" OR "40-research"
WHERE !contains(tags, "#domain/")
```

## Domain-Specific Views

### Auto-list for any domain tag
```dataview
LIST
FROM #domain/novatrade
SORT file.name ASC
```

### Notes per domain (manual count)
```dataview
TABLE length(rows) AS "Count"
FROM #type/learning OR #type/pattern OR #type/research
GROUP BY choice(
  contains(tags, "#domain/novatrade"), "novatrade",
  contains(tags, "#domain/autonomy"), "autonomy",
  contains(tags, "#domain/memory"), "memory",
  contains(tags, "#domain/infrastructure"), "infrastructure",
  contains(tags, "#domain/agents"), "agents",
  contains(tags, "#domain/risk"), "risk",
  "other"
) AS Domain
SORT Domain ASC
```

## ADR Tracking

### ADR candidates pending review
```dataview
TABLE title, date
FROM "00-inbox"
WHERE contains(tags, "#action/promote-to-adr")
SORT date DESC
```

### All accepted ADRs
```dataview
TABLE adr_id, title, date
FROM "10-adrs"
WHERE status = "accepted"
SORT adr_id ASC
```

## Hierarchy Navigation

### up:: field usage — notes with MOC parents
```dataview
TABLE up AS "Parent MOC", type
FROM ""
WHERE up
SORT up ASC
```

## Usage Notes

- Install the **Dataview** plugin in Obsidian (Community Plugins → Dataview)
- These queries go in any note body inside a ` ```dataview ` code block
- The `up::` inline field is readable by Dataview from anywhere in the note body
- `FROM [[note-name]]` finds all notes that link TO that note (backlinks)
- `FROM #tag` finds all notes with that tag
- Queries update live as notes change
