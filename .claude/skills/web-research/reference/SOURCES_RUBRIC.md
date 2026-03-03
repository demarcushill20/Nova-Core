# Source Quality Rubric

## Tier 1 — Primary / Official (prefer these)
- Official documentation sites (docs.*.com, *.readthedocs.io)
- Project repositories (github.com/<org>/<project>)
- RFC / specification documents
- Peer-reviewed publications
- Vendor announcements and changelogs

**Trust**: High. Cite directly. No hedging needed.

## Tier 2 — Reputable Secondary
- Stack Overflow answers with >10 upvotes
- Well-known tech blogs (e.g., Martin Fowler, Julia Evans)
- Established news outlets (Ars Technica, The Register, Hacker News top posts)
- Conference talks and slides from recognized events

**Trust**: Medium-high. Cross-reference with Tier 1 when possible.

## Tier 3 — Community / Informal
- Personal blogs, Medium posts, Dev.to articles
- Stack Overflow answers with low votes
- Forum threads (Reddit, Discourse)
- Tutorial sites without clear authorship

**Trust**: Medium. Use for leads and context, not as sole source. Always note provenance.

## Tier 4 — Low confidence
- AI-generated content farms
- Undated pages with no author
- SEO-optimized listicles
- Paywalled content where only snippets are visible

**Trust**: Low. Avoid citing as authoritative. Use only to corroborate Tier 1-2 findings.

## Handling conflicts between sources

1. **Tier wins**: Higher-tier source takes precedence.
2. **Recency wins** (within same tier): More recent information is preferred, especially for versioned software.
3. **Specificity wins**: A source discussing the exact version/config beats a general guide.
4. **Flag it**: If Tier 1 sources conflict with each other, report the conflict explicitly in Findings and set Confidence to `medium` or `low`.

## Red flags — disqualify or hedge
- Source contradicts official docs without explanation
- Source is >2 years old for a fast-moving project
- Source URL is a known content farm or scraper site
- Source contains factual errors you can verify independently
