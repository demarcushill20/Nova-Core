#!/usr/bin/env python3
"""Run a 6-skill cluster dry-run for the skill optimizer pipeline.

This script invokes run_optimization() with a specific list of skills
to test them together as a cluster, checking for cross-skill interference.
"""

import json
import sys
import time

from tools.skill_optimizer.optimize_all_skills import run_optimization
from tools.skill_optimizer.skill_discovery import discover_all

CLUSTER_SKILLS = [
    "web-research",
    "firecrawl-deep-research",
    "http-fetch",
    "context7-docs",
    "firecrawl-site-crawl",
    "firecrawl-extract",
]


def main():
    all_records = discover_all()
    skills = [r for r in all_records if r.name in CLUSTER_SKILLS]
    # Sort to match cluster order
    skill_order = {name: i for i, name in enumerate(CLUSTER_SKILLS)}
    skills.sort(key=lambda s: skill_order.get(s.name, 999))

    found = [s.name for s in skills]
    missing = [n for n in CLUSTER_SKILLS if n not in found]
    if missing:
        print(f"ERROR: Skills not found: {missing}", file=sys.stderr)
        sys.exit(1)

    print(f"Cluster run: {len(skills)} skills", file=sys.stderr)
    for s in skills:
        print(f"  - {s.name} (risk={s.risk_level})", file=sys.stderr)

    start = time.time()
    output = run_optimization(
        skills=skills,
        mode="dry-run",
        verbose=True,
    )
    elapsed = time.time() - start

    output["elapsed_seconds"] = round(elapsed, 1)
    print(json.dumps(output, indent=2, default=str))


if __name__ == "__main__":
    main()
