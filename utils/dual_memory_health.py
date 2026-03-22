"""Cross-system health monitoring for Fusion Memory + Obsidian Vault.

Implements Dual Memory Integration Phase 6: Cross-System Health detection.
Detects drift and staleness between Fusion Memory and Obsidian Vault.

Author: Claude Code (NovaCore Executive Agent)
Date: 2026-03-19
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CheckResult:
    """Result of a cross-system health check."""

    check_name: str
    status: str  # "ok", "gap_detected", "orphaned_flags", "orphaned_notes", "error"
    issue_count: int
    details: list[str]
    recommendations: list[str]


@dataclass
class HealthReport:
    """Comprehensive cross-system health report."""

    timestamp: str
    overall_status: str  # "healthy", "drift_detected", "critical"
    checks_performed: list[str]
    diary_sync: CheckResult
    promotion_integrity: CheckResult
    vault_note_integrity: CheckResult
    summary: str
    total_issues: int


class CrossSystemHealthChecker:
    """Health checker for drift detection between Fusion Memory and Obsidian Vault."""

    def __init__(self, max_checks_per_run: int = 2):
        """Initialize health checker.

        Args:
            max_checks_per_run: Maximum number of cross-system checks per invocation
                               (as per memory-health skill bounds)
        """
        self.max_checks_per_run = max_checks_per_run

    def check_checkpoint_diary_sync(self, fusion_memory_client: Any, obsidian_vault_client: Any) -> CheckResult:
        """Check A: Checkpoint-to-Diary Sync.

        Compare the latest Fusion Memory checkpoint with diary entries in Obsidian.
        Detects if checkpoints exist without corresponding diary entries.

        Args:
            fusion_memory_client: MCP client for nova-memory
            obsidian_vault_client: MCP client for nova-vault

        Returns:
            CheckResult with sync status
        """
        check_name = "Checkpoint-to-Diary Sync"
        try:
            # Step 1: Get the latest checkpoint from Fusion Memory
            logger.info("Checking latest Fusion Memory checkpoint...")
            checkpoint_result = fusion_memory_client.get_last_checkpoint(project="nova-core")

            if not checkpoint_result.get("result", {}).get("checkpoint"):
                return CheckResult(
                    check_name=check_name,
                    status="ok",
                    issue_count=0,
                    details=["No checkpoints found in Fusion Memory"],
                    recommendations=[],
                )

            checkpoint = checkpoint_result["result"]["checkpoint"]
            session_id = checkpoint["session_id"]

            # Step 2: Search for corresponding diary entries in Obsidian
            logger.info(f"Searching for diary entry for session: {session_id}")

            # Search in both 00-inbox/ (pending) and 90-diary/ (processed)
            inbox_search = obsidian_vault_client.vault_search(query=session_id, folder="00-inbox")
            diary_search = obsidian_vault_client.vault_search(query=session_id, folder="90-diary")

            # Step 3: Analyze results
            inbox_results = inbox_search.get("results", [])
            diary_results = diary_search.get("results", [])

            if not inbox_results and not diary_results:
                return CheckResult(
                    check_name=check_name,
                    status="gap_detected",
                    issue_count=1,
                    details=[
                        f"Checkpoint session '{session_id}' has no corresponding diary entry",
                        f"Checkpoint summary: {checkpoint.get('summary', 'N/A')[:100]}...",
                    ],
                    recommendations=[
                        "Run memory-checkpoint-to-diary skill to generate missing diary entry",
                        "Check if diary generation is properly configured",
                    ],
                )

            # Found diary entries
            entry_locations = []
            if inbox_results:
                entry_locations.extend([f"00-inbox/{r['path']}" for r in inbox_results[:2]])
            if diary_results:
                entry_locations.extend([f"90-diary/{r['path']}" for r in diary_results[:2]])

            return CheckResult(
                check_name=check_name,
                status="ok",
                issue_count=0,
                details=[
                    f"Checkpoint session '{session_id}' has diary entries",
                    f"Found in: {', '.join(entry_locations)}",
                ],
                recommendations=[],
            )

        except Exception as e:
            logger.error(f"Error in checkpoint-diary sync check: {e}")
            return CheckResult(
                check_name=check_name,
                status="error",
                issue_count=1,
                details=[f"Check failed with error: {e!s}"],
                recommendations=["Verify MCP server connectivity", "Check client configurations"],
            )

    def check_promoted_pattern_integrity(self, fusion_memory_client: Any, obsidian_vault_client: Any) -> CheckResult:
        """Check B: Promoted Pattern Integrity.

        Verify that Fusion Memory items marked as promoted_to_vault=true
        still have corresponding notes in Obsidian.

        Args:
            fusion_memory_client: MCP client for nova-memory
            obsidian_vault_client: MCP client for nova-vault

        Returns:
            CheckResult with integrity status
        """
        check_name = "Promoted Pattern Integrity"
        try:
            # Step 1: Query Fusion Memory for promoted patterns
            logger.info("Querying Fusion Memory for promoted patterns...")
            query_result = fusion_memory_client.query_memory(query="promoted patterns", top_k_final=10)

            if not query_result.get("result", {}).get("results"):
                return CheckResult(
                    check_name=check_name,
                    status="ok",
                    issue_count=0,
                    details=["No promoted patterns found in Fusion Memory"],
                    recommendations=[],
                )

            # Step 2: Filter for items with promoted_to_vault=true
            promoted_items = []
            for item in query_result["result"]["results"]:
                metadata = item.get("metadata", {})
                if metadata.get("promoted_to_vault") is True:
                    promoted_items.append(item)

            if not promoted_items:
                return CheckResult(
                    check_name=check_name,
                    status="ok",
                    issue_count=0,
                    details=["No items marked as promoted_to_vault=true found"],
                    recommendations=[],
                )

            # Step 3: Check each promoted item in Obsidian
            orphaned_flags = []
            verified_items = []

            for item in promoted_items[:5]:  # Limit to 5 items for performance
                item_id = item.get("id", "unknown")
                vault_path = item.get("metadata", {}).get("vault_path")

                if vault_path:
                    # Direct path check
                    try:
                        vault_result = obsidian_vault_client.vault_read(path=vault_path)
                        if vault_result:
                            verified_items.append(f"{item_id} -> {vault_path}")
                        else:
                            orphaned_flags.append(f"{item_id}: vault_path '{vault_path}' not found")
                    except Exception:
                        orphaned_flags.append(f"{item_id}: vault_path '{vault_path}' inaccessible")
                else:
                    # Search by content/title
                    search_text = item.get("text", "")[:50]  # First 50 chars
                    search_result = obsidian_vault_client.vault_search(query=search_text)

                    if search_result.get("results"):
                        verified_items.append(f"{item_id} -> found via search")
                    else:
                        orphaned_flags.append(f"{item_id}: no vault_path and not found via search")

            # Step 4: Generate result
            if orphaned_flags:
                return CheckResult(
                    check_name=check_name,
                    status="orphaned_flags",
                    issue_count=len(orphaned_flags),
                    details=orphaned_flags[:3] + (["..."] if len(orphaned_flags) > 3 else []),
                    recommendations=[
                        "Clear orphaned promotion flags via upsert_memory with promoted_to_vault: false",
                        "Re-run memory-promote-pattern skill to regenerate missing vault notes",
                    ],
                )
            else:
                return CheckResult(
                    check_name=check_name,
                    status="ok",
                    issue_count=0,
                    details=[f"All {len(promoted_items)} promoted patterns verified in vault"] + verified_items[:3],
                    recommendations=[],
                )

        except Exception as e:
            logger.error(f"Error in promoted pattern integrity check: {e}")
            return CheckResult(
                check_name=check_name,
                status="error",
                issue_count=1,
                details=[f"Check failed with error: {e!s}"],
                recommendations=["Verify MCP server connectivity", "Check query syntax"],
            )

    def check_stale_promotion_detection(self, fusion_memory_client: Any, obsidian_vault_client: Any) -> CheckResult:  # noqa: C901
        """Check C: Stale Promotion Detection.

        Check if Obsidian agent-patterns with source: nova-core-memory
        still have valid Fusion Memory source items.

        Args:
            fusion_memory_client: MCP client for nova-memory
            obsidian_vault_client: MCP client for nova-vault

        Returns:
            CheckResult with stale promotion status
        """
        check_name = "Stale Promotion Detection"
        try:
            # Step 1: List agent-patterns in Obsidian
            logger.info("Listing agent-pattern notes in Obsidian...")
            pattern_list = obsidian_vault_client.vault_list(folder="20-agent-patterns")

            if not pattern_list.get("files"):
                return CheckResult(
                    check_name=check_name,
                    status="ok",
                    issue_count=0,
                    details=["No agent-patterns folder or no files found"],
                    recommendations=[],
                )

            # Step 2: Filter for notes with source: nova-core-memory
            nova_sourced_notes = []
            for file_path in pattern_list["files"][:10]:  # Limit to 10 files
                try:
                    note_content = obsidian_vault_client.vault_read(path=file_path)
                    if note_content and "source: nova-core-memory" in note_content.get("content", ""):
                        nova_sourced_notes.append(file_path)
                except Exception as e:
                    logger.warning(f"Could not read {file_path}: {e}")
                    continue

            if not nova_sourced_notes:
                return CheckResult(
                    check_name=check_name,
                    status="ok",
                    issue_count=0,
                    details=["No agent-patterns with source: nova-core-memory found"],
                    recommendations=[],
                )

            # Step 3: Extract memory IDs and verify in Fusion Memory
            orphaned_notes = []
            verified_notes = []

            for note_path in nova_sourced_notes[:5]:  # Limit to 5 for performance
                try:
                    note_content = obsidian_vault_client.vault_read(path=note_path)
                    content = note_content.get("content", "")

                    # Extract memory IDs (look for common patterns)
                    memory_id_patterns = [
                        r"memory_id:\s*([a-zA-Z0-9_-]+)",  # YAML frontmatter
                        r"source_id:\s*([a-zA-Z0-9_-]+)",  # Alternative field
                        r"fusion_memory_id:\s*([a-zA-Z0-9_-]+)",  # Another alternative
                        r"id:\s*([a-zA-Z0-9_-]+)",  # Generic id field
                    ]

                    found_ids = []
                    for pattern in memory_id_patterns:
                        matches = re.findall(pattern, content, re.IGNORECASE)
                        found_ids.extend(matches)

                    if not found_ids:
                        # Try to search for the note content in Fusion Memory
                        # Extract a distinctive phrase from the note
                        title_match = re.search(r"^#\s*(.+)$", content, re.MULTILINE)
                        if title_match:
                            search_query = title_match.group(1)[:50]
                            search_result = fusion_memory_client.query_memory(query=search_query, top_k_final=3)

                            if search_result.get("result", {}).get("results"):
                                verified_notes.append(f"{note_path}: found via content search")
                            else:
                                orphaned_notes.append(f"{note_path}: no memory ID and not found via search")
                        else:
                            orphaned_notes.append(f"{note_path}: no memory ID and no searchable title")
                    else:
                        # Check if any of the found IDs exist in Fusion Memory
                        id_found = False
                        for memory_id in found_ids[:3]:  # Check first 3 IDs
                            # Try to query by ID (this may need adjustment based on MCP API)
                            search_result = fusion_memory_client.query_memory(query=memory_id, top_k_final=1)

                            results = search_result.get("result", {}).get("results", [])
                            if any(memory_id in str(result) for result in results):
                                id_found = True
                                verified_notes.append(f"{note_path}: memory_id {memory_id} verified")
                                break

                        if not id_found:
                            orphaned_notes.append(f"{note_path}: memory_id(s) {found_ids[:2]} not found")

                except Exception as e:
                    logger.warning(f"Error checking {note_path}: {e}")
                    continue

            # Step 4: Generate result
            if orphaned_notes:
                return CheckResult(
                    check_name=check_name,
                    status="orphaned_notes",
                    issue_count=len(orphaned_notes),
                    details=orphaned_notes[:3] + (["..."] if len(orphaned_notes) > 3 else []),
                    recommendations=[
                        "Flag orphaned vault notes with #status/orphaned for operator review",
                        "Do NOT auto-delete vault notes - manual review required",
                        "Consider re-sourcing patterns from current Fusion Memory state",
                    ],
                )
            else:
                return CheckResult(
                    check_name=check_name,
                    status="ok",
                    issue_count=0,
                    details=[f"All {len(nova_sourced_notes)} nova-core-memory patterns verified"] + verified_notes[:3],
                    recommendations=[],
                )

        except Exception as e:
            logger.error(f"Error in stale promotion detection: {e}")
            return CheckResult(
                check_name=check_name,
                status="error",
                issue_count=1,
                details=[f"Check failed with error: {e!s}"],
                recommendations=["Verify vault connectivity", "Check agent-patterns folder structure"],
            )

    def run_cross_system_health_check(
        self, fusion_memory_client: Any, obsidian_vault_client: Any, checks_to_run: list[str] | None = None
    ) -> HealthReport:
        """Run comprehensive cross-system health check.

        Args:
            fusion_memory_client: MCP client for nova-memory
            obsidian_vault_client: MCP client for nova-vault
            checks_to_run: Optional list of specific checks to run
                         ["diary_sync", "promotion_integrity", "stale_detection"]
                         If None, runs up to max_checks_per_run based on priority

        Returns:
            HealthReport with comprehensive analysis
        """
        timestamp = datetime.utcnow().isoformat()

        # Default check priority order
        all_checks = ["diary_sync", "promotion_integrity", "stale_detection"]

        if checks_to_run:
            selected_checks = [c for c in checks_to_run if c in all_checks]
        else:
            # Select top priority checks up to the limit
            selected_checks = all_checks[: self.max_checks_per_run]

        logger.info(f"Running cross-system health checks: {selected_checks}")

        # Initialize results
        diary_sync = CheckResult("diary_sync", "not_checked", 0, [], [])
        promotion_integrity = CheckResult("promotion_integrity", "not_checked", 0, [], [])
        vault_note_integrity = CheckResult("vault_note_integrity", "not_checked", 0, [], [])

        # Run selected checks
        if "diary_sync" in selected_checks:
            diary_sync = self.check_checkpoint_diary_sync(fusion_memory_client, obsidian_vault_client)

        if "promotion_integrity" in selected_checks:
            promotion_integrity = self.check_promoted_pattern_integrity(fusion_memory_client, obsidian_vault_client)

        if "stale_detection" in selected_checks:
            vault_note_integrity = self.check_stale_promotion_detection(fusion_memory_client, obsidian_vault_client)

        # Analyze overall status
        all_results = [diary_sync, promotion_integrity, vault_note_integrity]
        checked_results = [r for r in all_results if r.status != "not_checked"]

        total_issues = sum(r.issue_count for r in checked_results)
        error_count = sum(1 for r in checked_results if r.status == "error")

        if error_count > 0:
            overall_status = "critical"
            summary = f"Cross-system health check encountered {error_count} errors"
        elif total_issues > 0:
            overall_status = "drift_detected"
            summary = f"Found {total_issues} cross-system drift issues across {len(checked_results)} checks"
        else:
            overall_status = "healthy"
            summary = f"All {len(checked_results)} cross-system checks passed - systems are synchronized"

        return HealthReport(
            timestamp=timestamp,
            overall_status=overall_status,
            checks_performed=selected_checks,
            diary_sync=diary_sync,
            promotion_integrity=promotion_integrity,
            vault_note_integrity=vault_note_integrity,
            summary=summary,
            total_issues=total_issues,
        )


def format_health_report(report: HealthReport) -> str:
    """Format a HealthReport for human-readable output."""

    status_emoji = {"healthy": "✅", "drift_detected": "⚠️", "critical": "❌"}

    check_status_emoji = {
        "ok": "✅",
        "gap_detected": "⚠️",
        "orphaned_flags": "⚠️",
        "orphaned_notes": "⚠️",
        "error": "❌",
        "not_checked": "⏭️",
    }

    lines = [
        f"# Cross-System Health Report {status_emoji.get(report.overall_status, '❓')}",
        "",
        f"**Timestamp:** {report.timestamp}",
        f"**Status:** {report.overall_status.title()}",
        f"**Summary:** {report.summary}",
        f"**Checks Performed:** {', '.join(report.checks_performed)}",
        f"**Total Issues:** {report.total_issues}",
        "",
        "## Check Details",
        "",
    ]

    # Diary Sync
    ds = report.diary_sync
    lines.extend(
        [
            f"### {check_status_emoji.get(ds.status, '❓')} Checkpoint-to-Diary Sync",
            f"**Status:** {ds.status}",
            f"**Issues:** {ds.issue_count}",
        ]
    )
    if ds.details:
        lines.extend([f"- {detail}" for detail in ds.details])
    if ds.recommendations:
        lines.extend(["**Recommendations:**"] + [f"- {rec}" for rec in ds.recommendations])
    lines.append("")

    # Promotion Integrity
    pi = report.promotion_integrity
    lines.extend(
        [
            f"### {check_status_emoji.get(pi.status, '❓')} Promoted Pattern Integrity",
            f"**Status:** {pi.status}",
            f"**Issues:** {pi.issue_count}",
        ]
    )
    if pi.details:
        lines.extend([f"- {detail}" for detail in pi.details])
    if pi.recommendations:
        lines.extend(["**Recommendations:**"] + [f"- {rec}" for rec in pi.recommendations])
    lines.append("")

    # Vault Note Integrity
    vni = report.vault_note_integrity
    lines.extend(
        [
            f"### {check_status_emoji.get(vni.status, '❓')} Vault Note Integrity",
            f"**Status:** {vni.status}",
            f"**Issues:** {vni.issue_count}",
        ]
    )
    if vni.details:
        lines.extend([f"- {detail}" for detail in vni.details])
    if vni.recommendations:
        lines.extend(["**Recommendations:**"] + [f"- {rec}" for rec in vni.recommendations])

    return "\n".join(lines)
