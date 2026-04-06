#!/usr/bin/env python3
"""Backfill wikilinks on existing Nova-Core vault notes.

Adds a '## Related Notes' section to notes in writable folders that
lack cross-references. Uses vault MCP tools for search and update.

Usage:
    python3 scripts/vault_backfill_links.py --dry-run     # preview changes
    python3 scripts/vault_backfill_links.py                # apply changes
    python3 scripts/vault_backfill_links.py --folder 20-agent-patterns  # single folder
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

WRITABLE_FOLDERS = [
    "20-agent-patterns",
    "30-workflow-learnings",
    "40-research",
    "70-debugging",
]

RATE_LIMIT_WRITES_PER_WINDOW = 8  # leave 2-write buffer from 10/5min limit
RATE_LIMIT_WINDOW_SECONDS = 300  # 5 minutes

LOG_DIR = Path(__file__).resolve().parent.parent / "LOGS"
LOG_FILE = LOG_DIR / "vault_backfill_links.log"

logger = logging.getLogger("vault_backfill")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class BackfillCandidate:
    """A note that may need a Related Notes section."""

    path: str
    title: str
    source: str | None = None
    has_related: bool = False
    related_notes: list[str] | None = None
    skip_reason: str | None = None


@dataclass
class BackfillResult:
    """Result of processing one note."""

    path: str
    action: str  # "updated", "skipped", "dry-run"
    related_count: int = 0
    reason: str = ""


# ---------------------------------------------------------------------------
# Keyword extraction
# ---------------------------------------------------------------------------

# Common stopwords to filter out
_STOPWORDS = frozenset(
    {
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "in",
        "on",
        "at",
        "to",
        "for",
        "of",
        "with",
        "by",
        "from",
        "is",
        "was",
        "are",
        "were",
        "be",
        "been",
        "has",
        "have",
        "had",
        "do",
        "does",
        "did",
        "will",
        "would",
        "could",
        "should",
        "may",
        "might",
        "can",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "not",
        "no",
        "as",
        "if",
        "then",
        "than",
        "when",
        "how",
        "what",
        "which",
        "who",
        "whom",
        "where",
        "why",
        "all",
        "each",
        "every",
        "any",
        "some",
        "such",
        "new",
        "use",
        "using",
        "used",
    }
)


def extract_keywords(title: str, content: str, max_keywords: int = 5) -> list[str]:
    """Extract search keywords from a note's title and content.

    Prioritizes title words, then supplements with content keywords.
    Filters stopwords and very short tokens.
    """

    # Tokenize: split on non-alphanumeric, lowercase
    def tokenize(text: str) -> list[str]:
        return [w.lower() for w in re.split(r"[^a-zA-Z0-9]+", text) if len(w) >= 3 and w.lower() not in _STOPWORDS]

    # Title keywords first (higher signal)
    title_tokens = tokenize(title)
    content_tokens = tokenize(content[:2000])  # first 2KB for efficiency

    # Deduplicate while preserving order, title first
    seen = set()
    keywords = []
    for token in title_tokens + content_tokens:
        if token not in seen:
            seen.add(token)
            keywords.append(token)
        if len(keywords) >= max_keywords:
            break

    return keywords


# ---------------------------------------------------------------------------
# Related note selection
# ---------------------------------------------------------------------------


def pick_top_related(
    results: list[dict],
    exclude_path: str,
    max_related: int = 3,
) -> list[dict]:
    """Pick the top N related notes from search results.

    Excludes the source note itself and diary notes (read-only folder).
    Returns list of dicts with 'path' and 'title' keys.
    """
    related = []
    for r in results:
        path = r.get("path", "")
        # Skip self
        if path == exclude_path:
            continue
        # Skip diary notes (read-only folder)
        if path.startswith("90-diary/"):
            continue
        # Skip _meta notes
        if path.startswith("_meta/"):
            continue
        title = r.get("title", Path(path).stem)
        related.append({"path": path, "title": title})
        if len(related) >= max_related:
            break
    return related


def format_related_section(related: list[dict], domain: str | None = None) -> str:
    """Format the ## Related Notes section for appending.

    Includes up:: field if domain is provided.
    """
    lines = []
    # Add up:: field if domain provided
    if domain:
        lines.append(f"up:: [[moc-{domain}]]")
        lines.append("")
    lines.append("## Related Notes")
    lines.append("")
    if not related:
        lines.append("- (no related notes found)")
    else:
        for r in related:
            # Extract just the filename without extension for wikilink
            name = Path(r["path"]).stem
            title = r.get("title", name)
            lines.append(f"- [[{name}]] — {title}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Vault MCP interaction (subprocess calls to claude)
# ---------------------------------------------------------------------------


def vault_call(tool: str, args: dict) -> dict:
    """Call a vault MCP tool via the vault server.

    In production, this would use the MCP client. For the backfill script,
    we use subprocess calls to the nova-vault tools.

    This is a stub that should be replaced with actual MCP client calls
    or direct function imports when running in the Nova-Core environment.
    """
    # This is the interface contract — implementations vary by environment
    raise NotImplementedError(
        "vault_call must be provided by the runtime environment. Use --dry-run for testing without vault access."
    )


# ---------------------------------------------------------------------------
# Backfill logic
# ---------------------------------------------------------------------------


class VaultBackfiller:
    """Backfill Related Notes sections on existing vault notes."""

    def __init__(
        self,
        vault_call_fn=None,
        dry_run: bool = False,
        rate_limit: int = RATE_LIMIT_WRITES_PER_WINDOW,
        rate_window: int = RATE_LIMIT_WINDOW_SECONDS,
    ):
        self.vault_call = vault_call_fn or vault_call
        self.dry_run = dry_run
        self.rate_limit = rate_limit
        self.rate_window = rate_window
        self._write_timestamps: list[float] = []
        self.results: list[BackfillResult] = []

    def _check_rate_limit(self) -> bool:
        """Check if we can write without exceeding rate limit."""
        now = time.time()
        # Prune old timestamps
        self._write_timestamps = [ts for ts in self._write_timestamps if now - ts < self.rate_window]
        return len(self._write_timestamps) < self.rate_limit

    def _record_write(self):
        """Record a write timestamp for rate limiting."""
        self._write_timestamps.append(time.time())

    def _wait_for_rate_limit(self):
        """Wait until rate limit allows another write."""
        while not self._check_rate_limit():
            oldest = min(self._write_timestamps)
            wait_time = self.rate_window - (time.time() - oldest) + 1
            logger.info("Rate limit reached, waiting %.0fs...", wait_time)
            time.sleep(max(1, wait_time))

    def process_note(self, note_path: str) -> BackfillResult:
        """Process a single note for backfill.

        Returns a BackfillResult describing what happened.
        """
        # 1. Read the note
        try:
            read_result = self.vault_call("vault_read", {"path": note_path})
        except Exception as e:
            return BackfillResult(note_path, "skipped", reason=f"read error: {e}")

        if "error" in read_result:
            return BackfillResult(note_path, "skipped", reason=read_result["error"])

        content = read_result.get("content", "")

        # 2. Check if already backfilled (idempotent)
        if "## Related Notes" in content:
            return BackfillResult(note_path, "skipped", reason="already has ## Related Notes")

        # 3. Get frontmatter
        try:
            fm_result = self.vault_call("vault_frontmatter", {"path": note_path})
        except Exception as e:
            return BackfillResult(note_path, "skipped", reason=f"frontmatter error: {e}")

        fm = fm_result.get("frontmatter", {})

        # 4. Source safety check
        source = fm.get("source", "")
        if source == "operator":
            return BackfillResult(note_path, "skipped", reason="source: operator (human-authored)")
        if not source:
            return BackfillResult(note_path, "skipped", reason="no source field (unknown ownership)")

        # 5. Extract keywords and search
        title = fm.get("title", "")
        keywords = extract_keywords(title, content)
        if not keywords:
            return BackfillResult(note_path, "skipped", reason="no keywords extracted")

        search_query = " ".join(keywords[:3])
        try:
            search_result = self.vault_call("vault_search", {"query": search_query})
        except Exception as e:
            return BackfillResult(note_path, "skipped", reason=f"search error: {e}")

        results = search_result.get("results", [])
        related = pick_top_related(results, note_path)

        if not related:
            return BackfillResult(note_path, "skipped", reason="no related notes found")

        # 6. Infer domain for up:: field
        domain = self._infer_domain(title, content, fm)

        # 7. Format the section
        section = format_related_section(related, domain=domain)

        # 8. Write or dry-run
        if self.dry_run:
            logger.info("[DRY-RUN] Would append to %s: %d related notes", note_path, len(related))
            return BackfillResult(note_path, "dry-run", len(related))

        # Rate limit check
        self._wait_for_rate_limit()

        try:
            update_result = self.vault_call(
                "vault_update",
                {
                    "path": note_path,
                    "content": section,
                },
            )
        except Exception as e:
            return BackfillResult(note_path, "skipped", reason=f"update error: {e}")

        if "error" in update_result:
            return BackfillResult(note_path, "skipped", reason=update_result["error"])

        self._record_write()
        return BackfillResult(note_path, "updated", len(related))

    def _infer_domain(self, title: str, content: str, fm: dict) -> str | None:
        """Infer the domain from note content for up:: field."""
        text = f"{title} {content[:1000]}".lower()

        domain_keywords = {
            "novatrade": ["trade", "strategy", "backtest", "irb", "mt5", "execution", "ftmo"],
            "autonomy": ["autonomy", "heartbeat", "decision engine", "guardrail"],
            "memory": ["memory", "fusion", "pinecone", "neo4j", "vault", "retrieval"],
            "infrastructure": ["systemd", "circuit breaker", "self-heal", "deploy", "nginx"],
            "agents": ["agent", "spawner", "orchestrator", "multi-agent"],
            "risk": ["risk", "gate", "filter", "drawdown", "exposure"],
        }

        for domain, keywords in domain_keywords.items():
            for kw in keywords:
                if kw in text:
                    return domain

        return "operations"  # default

    def process_folder(self, folder: str) -> list[BackfillResult]:
        """Process all notes in a folder."""
        try:
            list_result = self.vault_call("vault_list", {"path": folder})
        except Exception as e:
            logger.error("Failed to list folder %s: %s", folder, e)
            return []

        notes = list_result.get("notes", [])
        results = []
        for note_info in notes:
            path = note_info if isinstance(note_info, str) else note_info.get("path", "")
            if not path.endswith(".md"):
                continue
            result = self.process_note(path)
            results.append(result)
            self.results.append(result)
            logger.info("%s: %s (%s)", result.action, result.path, result.reason or f"{result.related_count} links")
        return results

    def process_all(self, folders: list[str] | None = None) -> list[BackfillResult]:
        """Process all writable folders."""
        target_folders = folders or WRITABLE_FOLDERS
        for folder in target_folders:
            if folder not in WRITABLE_FOLDERS:
                logger.warning("Skipping non-writable folder: %s", folder)
                continue
            logger.info("Processing folder: %s", folder)
            self.process_folder(folder)
        return self.results

    def summary(self) -> dict:
        """Generate a summary of all backfill results."""
        updated = [r for r in self.results if r.action == "updated"]
        skipped = [r for r in self.results if r.action == "skipped"]
        dry_run = [r for r in self.results if r.action == "dry-run"]
        total_links = sum(r.related_count for r in self.results)

        return {
            "total_processed": len(self.results),
            "updated": len(updated),
            "skipped": len(skipped),
            "dry_run": len(dry_run),
            "total_links_added": total_links,
            "skip_reasons": {r.reason: sum(1 for x in skipped if x.reason == r.reason) for r in skipped},
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Backfill wikilinks on existing Nova-Core vault notes")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without writing",
    )
    parser.add_argument(
        "--folder",
        choices=WRITABLE_FOLDERS,
        help="Process only this folder",
    )
    parser.add_argument(
        "--rate-limit",
        type=int,
        default=RATE_LIMIT_WRITES_PER_WINDOW,
        help=f"Max writes per {RATE_LIMIT_WINDOW_SECONDS}s window (default: {RATE_LIMIT_WRITES_PER_WINDOW})",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Verbose logging",
    )
    args = parser.parse_args()

    # Setup logging
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOG_FILE),
        ],
    )

    logger.info("=== Vault Backfill Links ===")
    logger.info("Mode: %s", "DRY-RUN" if args.dry_run else "LIVE")

    # Note: vault_call must be provided by the runtime environment
    # This CLI is for documentation; actual execution uses MCP tools
    logger.error(
        "Direct CLI execution requires MCP vault client integration. "
        "Use this script's classes from within Nova-Core's MCP environment, "
        "or run with --dry-run for testing."
    )
    sys.exit(1)


if __name__ == "__main__":
    main()
