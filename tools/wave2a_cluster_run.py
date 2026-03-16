#!/usr/bin/env python3
"""Wave 2A cluster dry-run: context7-docs, firecrawl-site-crawl, firecrawl-extract."""

import json
import sys

from tools.skill_optimizer.optimize_all_skills import run_optimization
from tools.skill_optimizer.skill_discovery import discover_all

all_records = discover_all()
target_names = ["context7-docs", "firecrawl-site-crawl", "firecrawl-extract"]
skills = [r for r in all_records if r.name in target_names]

print(f"Found {len(skills)} skills:", file=sys.stderr)
for s in skills:
    print(f"  - {s.name}", file=sys.stderr)

output = run_optimization(
    skills=skills,
    mode="dry-run",
    verbose=True,
)

# Write JSON output to a result file
result_path = "/home/nova/nova-core/OUTPUT/wave2a_result.json"
with open(result_path, "w") as f:
    json.dump(output, f, indent=2, default=str)

print(f"\nResult written to {result_path}", file=sys.stderr)
print(json.dumps(output, indent=2, default=str))
