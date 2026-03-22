"""Tests for utils/dual_memory_health.py — Cross-system health monitoring.

Covers:
- Checkpoint-to-diary sync detection
- Promoted pattern integrity checking
- Stale promotion detection
- Comprehensive health reporting
- Error handling and edge cases
"""

from unittest.mock import Mock

from utils.dual_memory_health import CheckResult, CrossSystemHealthChecker, HealthReport, format_health_report


class TestCheckResult:
    """Test CheckResult dataclass."""

    def test_check_result_creation(self):
        """Test creating CheckResult objects."""
        result = CheckResult(
            check_name="test_check", status="ok", issue_count=0, details=["All good"], recommendations=[]
        )

        assert result.check_name == "test_check"
        assert result.status == "ok"
        assert result.issue_count == 0
        assert result.details == ["All good"]
        assert result.recommendations == []

    def test_check_result_with_issues(self):
        """Test CheckResult with issues detected."""
        result = CheckResult(
            check_name="problem_check",
            status="gap_detected",
            issue_count=2,
            details=["Issue 1", "Issue 2"],
            recommendations=["Fix 1", "Fix 2"],
        )

        assert result.check_name == "problem_check"
        assert result.status == "gap_detected"
        assert result.issue_count == 2
        assert len(result.details) == 2
        assert len(result.recommendations) == 2


class TestHealthReport:
    """Test HealthReport dataclass."""

    def test_health_report_creation(self):
        """Test creating comprehensive HealthReport."""
        diary_sync = CheckResult("diary_sync", "ok", 0, [], [])
        promotion_integrity = CheckResult("promotion_integrity", "orphaned_flags", 2, [], [])
        vault_note_integrity = CheckResult("vault_note_integrity", "not_checked", 0, [], [])

        report = HealthReport(
            timestamp="2026-03-19T10:00:00",
            overall_status="drift_detected",
            checks_performed=["diary_sync", "promotion_integrity"],
            diary_sync=diary_sync,
            promotion_integrity=promotion_integrity,
            vault_note_integrity=vault_note_integrity,
            summary="Found 2 issues",
            total_issues=2,
        )

        assert report.overall_status == "drift_detected"
        assert report.total_issues == 2
        assert len(report.checks_performed) == 2
        assert report.diary_sync.status == "ok"
        assert report.promotion_integrity.status == "orphaned_flags"
        assert report.vault_note_integrity.status == "not_checked"


class TestCrossSystemHealthChecker:
    """Test CrossSystemHealthChecker class."""

    def setup_method(self):
        """Set up test fixtures."""
        self.checker = CrossSystemHealthChecker(max_checks_per_run=2)
        self.mock_fusion_client = Mock()
        self.mock_vault_client = Mock()

    def test_checker_initialization(self):
        """Test checker initialization with parameters."""
        checker = CrossSystemHealthChecker(max_checks_per_run=3)
        assert checker.max_checks_per_run == 3

        # Default initialization
        checker_default = CrossSystemHealthChecker()
        assert checker_default.max_checks_per_run == 2

    # Checkpoint-to-Diary Sync Tests

    def test_checkpoint_diary_sync_no_checkpoint(self):
        """Test sync check when no checkpoint exists."""
        # Mock: No checkpoint in Fusion Memory
        self.mock_fusion_client.get_last_checkpoint.return_value = {"result": {}}

        result = self.checker.check_checkpoint_diary_sync(self.mock_fusion_client, self.mock_vault_client)

        assert result.check_name == "Checkpoint-to-Diary Sync"
        assert result.status == "ok"
        assert result.issue_count == 0
        assert "No checkpoints found" in result.details[0]

    def test_checkpoint_diary_sync_gap_detected(self):
        """Test sync check when checkpoint exists but no diary entry."""
        # Mock: Checkpoint exists
        self.mock_fusion_client.get_last_checkpoint.return_value = {
            "result": {"checkpoint": {"session_id": "session-2026-03-19-test", "summary": "Test session summary"}}
        }

        # Mock: No diary entries found
        self.mock_vault_client.vault_search.return_value = {"results": []}

        result = self.checker.check_checkpoint_diary_sync(self.mock_fusion_client, self.mock_vault_client)

        assert result.check_name == "Checkpoint-to-Diary Sync"
        assert result.status == "gap_detected"
        assert result.issue_count == 1
        assert "session-2026-03-19-test" in result.details[0]
        assert "memory-checkpoint-to-diary" in result.recommendations[0]

        # Verify vault was searched in both locations
        assert self.mock_vault_client.vault_search.call_count == 2

    def test_checkpoint_diary_sync_found_in_inbox(self):
        """Test sync check when diary entry found in inbox."""
        # Mock: Checkpoint exists
        self.mock_fusion_client.get_last_checkpoint.return_value = {
            "result": {"checkpoint": {"session_id": "session-2026-03-19-test", "summary": "Test session summary"}}
        }

        # Mock: Entry found in inbox
        self.mock_vault_client.vault_search.side_effect = [
            {"results": [{"path": "diary-2026-03-19-test.md"}]},  # inbox search
            {"results": []},  # diary search
        ]

        result = self.checker.check_checkpoint_diary_sync(self.mock_fusion_client, self.mock_vault_client)

        assert result.check_name == "Checkpoint-to-Diary Sync"
        assert result.status == "ok"
        assert result.issue_count == 0
        assert "00-inbox" in result.details[1]

    def test_checkpoint_diary_sync_error_handling(self):
        """Test sync check error handling."""
        # Mock: Exception in get_last_checkpoint
        self.mock_fusion_client.get_last_checkpoint.side_effect = Exception("Connection error")

        result = self.checker.check_checkpoint_diary_sync(self.mock_fusion_client, self.mock_vault_client)

        assert result.check_name == "Checkpoint-to-Diary Sync"
        assert result.status == "error"
        assert result.issue_count == 1
        assert "Connection error" in result.details[0]
        assert "connectivity" in result.recommendations[0]

    # Promoted Pattern Integrity Tests

    def test_promoted_pattern_integrity_no_patterns(self):
        """Test integrity check when no promoted patterns exist."""
        # Mock: No promoted patterns found
        self.mock_fusion_client.query_memory.return_value = {"result": {"results": []}}

        result = self.checker.check_promoted_pattern_integrity(self.mock_fusion_client, self.mock_vault_client)

        assert result.check_name == "Promoted Pattern Integrity"
        assert result.status == "ok"
        assert result.issue_count == 0
        assert "No promoted patterns found" in result.details[0]

    def test_promoted_pattern_integrity_none_marked_promoted(self):
        """Test integrity check when patterns exist but none marked as promoted."""
        # Mock: Patterns found but none promoted
        self.mock_fusion_client.query_memory.return_value = {
            "result": {
                "results": [
                    {"id": "pattern1", "metadata": {"promoted_to_vault": False}},
                    {
                        "id": "pattern2",
                        "metadata": {},  # No promoted_to_vault field
                    },
                ]
            }
        }

        result = self.checker.check_promoted_pattern_integrity(self.mock_fusion_client, self.mock_vault_client)

        assert result.check_name == "Promoted Pattern Integrity"
        assert result.status == "ok"
        assert result.issue_count == 0
        assert "No items marked as promoted_to_vault=true" in result.details[0]

    def test_promoted_pattern_integrity_with_vault_path(self):
        """Test integrity check with valid vault_path."""
        # Mock: Pattern with vault_path
        self.mock_fusion_client.query_memory.return_value = {
            "result": {
                "results": [
                    {
                        "id": "pattern1",
                        "metadata": {"promoted_to_vault": True, "vault_path": "20-agent-patterns/test-pattern.md"},
                    }
                ]
            }
        }

        # Mock: Vault file exists
        self.mock_vault_client.vault_read.return_value = {"content": "Pattern content"}

        result = self.checker.check_promoted_pattern_integrity(self.mock_fusion_client, self.mock_vault_client)

        assert result.check_name == "Promoted Pattern Integrity"
        assert result.status == "ok"
        assert result.issue_count == 0
        assert "promoted patterns verified" in result.details[0]

    def test_promoted_pattern_integrity_orphaned_flags(self):
        """Test integrity check with orphaned promotion flags."""
        # Mock: Pattern marked as promoted but vault file missing
        self.mock_fusion_client.query_memory.return_value = {
            "result": {
                "results": [
                    {
                        "id": "pattern1",
                        "metadata": {"promoted_to_vault": True, "vault_path": "20-agent-patterns/missing-pattern.md"},
                    },
                    {"id": "pattern2", "text": "Pattern without vault_path", "metadata": {"promoted_to_vault": True}},
                ]
            }
        }

        # Mock: First vault file missing, search fails for second
        self.mock_vault_client.vault_read.return_value = None
        self.mock_vault_client.vault_search.return_value = {"results": []}

        result = self.checker.check_promoted_pattern_integrity(self.mock_fusion_client, self.mock_vault_client)

        assert result.check_name == "Promoted Pattern Integrity"
        assert result.status == "orphaned_flags"
        assert result.issue_count == 2
        assert "pattern1" in result.details[0]
        assert "pattern2" in result.details[1]
        assert "upsert_memory" in result.recommendations[0]

    def test_promoted_pattern_integrity_search_fallback(self):
        """Test integrity check falling back to search when no vault_path."""
        # Mock: Pattern without vault_path
        self.mock_fusion_client.query_memory.return_value = {
            "result": {
                "results": [{"id": "pattern1", "text": "Test pattern content", "metadata": {"promoted_to_vault": True}}]
            }
        }

        # Mock: Found via search
        self.mock_vault_client.vault_search.return_value = {"results": [{"path": "20-agent-patterns/found-pattern.md"}]}

        result = self.checker.check_promoted_pattern_integrity(self.mock_fusion_client, self.mock_vault_client)

        assert result.check_name == "Promoted Pattern Integrity"
        assert result.status == "ok"
        assert result.issue_count == 0
        assert "found via search" in str(result.details)

    # Stale Promotion Detection Tests

    def test_stale_promotion_detection_no_patterns_folder(self):
        """Test stale detection when no agent-patterns folder exists."""
        # Mock: No files in agent-patterns
        self.mock_vault_client.vault_list.return_value = {"files": []}

        result = self.checker.check_stale_promotion_detection(self.mock_fusion_client, self.mock_vault_client)

        assert result.check_name == "Stale Promotion Detection"
        assert result.status == "ok"
        assert result.issue_count == 0
        assert "No agent-patterns" in result.details[0]

    def test_stale_promotion_detection_no_nova_sourced(self):
        """Test stale detection when no nova-core-memory sourced patterns."""
        # Mock: Files exist but none sourced from nova-core-memory
        self.mock_vault_client.vault_list.return_value = {"files": ["20-agent-patterns/other-pattern.md"]}
        self.mock_vault_client.vault_read.return_value = {"content": "---\nsource: manual\n---\nOther pattern"}

        result = self.checker.check_stale_promotion_detection(self.mock_fusion_client, self.mock_vault_client)

        assert result.check_name == "Stale Promotion Detection"
        assert result.status == "ok"
        assert result.issue_count == 0
        assert "No agent-patterns with source: nova-core-memory" in result.details[0]

    def test_stale_promotion_detection_with_memory_id(self):
        """Test stale detection with explicit memory_id in frontmatter."""
        # Mock: Nova-sourced file with memory_id
        self.mock_vault_client.vault_list.return_value = {"files": ["20-agent-patterns/test-pattern.md"]}
        self.mock_vault_client.vault_read.return_value = {
            "content": """---
source: nova-core-memory
memory_id: abc123-def456-789
---
# Test Pattern
Content here"""
        }

        # Mock: Memory ID found in Fusion Memory
        self.mock_fusion_client.query_memory.return_value = {
            "result": {"results": [{"id": "abc123-def456-789", "text": "Found pattern"}]}
        }

        result = self.checker.check_stale_promotion_detection(self.mock_fusion_client, self.mock_vault_client)

        assert result.check_name == "Stale Promotion Detection"
        assert result.status == "ok"
        assert result.issue_count == 0
        assert "verified" in result.details[0]

    def test_stale_promotion_detection_orphaned_notes(self):
        """Test stale detection with orphaned vault notes."""
        # Mock: Nova-sourced file with missing memory_id
        self.mock_vault_client.vault_list.return_value = {"files": ["20-agent-patterns/orphaned-pattern.md"]}
        self.mock_vault_client.vault_read.return_value = {
            "content": """---
source: nova-core-memory
memory_id: missing-id-999
---
# Orphaned Pattern
This pattern's source no longer exists"""
        }

        # Mock: Memory ID not found in Fusion Memory
        self.mock_fusion_client.query_memory.return_value = {"result": {"results": []}}

        result = self.checker.check_stale_promotion_detection(self.mock_fusion_client, self.mock_vault_client)

        assert result.check_name == "Stale Promotion Detection"
        assert result.status == "orphaned_notes"
        assert result.issue_count == 1
        assert "orphaned-pattern.md" in result.details[0]
        assert "missing-id-999" in result.details[0]
        assert "#status/orphaned" in result.recommendations[0]
        assert "Do NOT auto-delete" in result.recommendations[1]

    def test_stale_promotion_detection_content_search_fallback(self):
        """Test stale detection falling back to content search."""
        # Mock: Nova-sourced file without memory_id
        self.mock_vault_client.vault_list.return_value = {"files": ["20-agent-patterns/no-id-pattern.md"]}
        self.mock_vault_client.vault_read.return_value = {
            "content": """---
source: nova-core-memory
---
# Search Test Pattern
Unique content for searching"""
        }

        # Mock: Found via content search
        self.mock_fusion_client.query_memory.return_value = {
            "result": {"results": [{"id": "found-via-search", "text": "Search Test Pattern"}]}
        }

        result = self.checker.check_stale_promotion_detection(self.mock_fusion_client, self.mock_vault_client)

        assert result.check_name == "Stale Promotion Detection"
        assert result.status == "ok"
        assert result.issue_count == 0
        assert "found via content search" in str(result.details)

    # Comprehensive Health Check Tests

    def test_run_cross_system_health_check_default(self):
        """Test running health check with default parameters."""
        # Mock successful checks
        self.mock_fusion_client.get_last_checkpoint.return_value = {"result": {}}
        self.mock_fusion_client.query_memory.return_value = {"result": {"results": []}}

        result = self.checker.run_cross_system_health_check(self.mock_fusion_client, self.mock_vault_client)

        assert isinstance(result, HealthReport)
        assert result.overall_status == "healthy"
        assert len(result.checks_performed) == 2  # max_checks_per_run
        assert result.total_issues == 0
        assert "synchronized" in result.summary

    def test_run_cross_system_health_check_specific_checks(self):
        """Test running health check with specific checks."""
        # Mock successful diary sync
        self.mock_fusion_client.get_last_checkpoint.return_value = {"result": {}}

        result = self.checker.run_cross_system_health_check(
            self.mock_fusion_client, self.mock_vault_client, checks_to_run=["diary_sync"]
        )

        assert result.checks_performed == ["diary_sync"]
        assert result.diary_sync.status == "ok"
        assert result.promotion_integrity.status == "not_checked"
        assert result.vault_note_integrity.status == "not_checked"

    def test_run_cross_system_health_check_with_issues(self):
        """Test health check when issues are detected."""
        # Mock checkpoint with no diary entry (gap detected)
        self.mock_fusion_client.get_last_checkpoint.return_value = {
            "result": {"checkpoint": {"session_id": "test-session", "summary": "Test"}}
        }
        self.mock_vault_client.vault_search.return_value = {"results": []}

        # Mock promoted pattern with orphaned flags
        self.mock_fusion_client.query_memory.return_value = {
            "result": {
                "results": [
                    {"id": "orphaned-pattern", "metadata": {"promoted_to_vault": True, "vault_path": "missing.md"}}
                ]
            }
        }
        self.mock_vault_client.vault_read.return_value = None

        result = self.checker.run_cross_system_health_check(self.mock_fusion_client, self.mock_vault_client)

        assert result.overall_status == "drift_detected"
        assert result.total_issues == 2  # 1 gap + 1 orphaned flag
        assert "drift issues" in result.summary
        assert result.diary_sync.status == "gap_detected"
        assert result.promotion_integrity.status == "orphaned_flags"

    def test_run_cross_system_health_check_errors(self):
        """Test health check when errors occur."""
        # Mock error in get_last_checkpoint
        self.mock_fusion_client.get_last_checkpoint.side_effect = Exception("Test error")

        # Mock successful other check
        self.mock_fusion_client.query_memory.return_value = {"result": {"results": []}}

        result = self.checker.run_cross_system_health_check(self.mock_fusion_client, self.mock_vault_client)

        assert result.overall_status == "critical"
        assert "errors" in result.summary
        assert result.diary_sync.status == "error"
        assert result.promotion_integrity.status == "ok"

    def test_run_cross_system_health_check_invalid_checks(self):
        """Test health check with invalid check names."""
        result = self.checker.run_cross_system_health_check(
            self.mock_fusion_client,
            self.mock_vault_client,
            checks_to_run=["invalid_check", "diary_sync", "another_invalid"],
        )

        # Should only run valid checks
        assert result.checks_performed == ["diary_sync"]


class TestFormatHealthReport:
    """Test health report formatting."""

    def test_format_healthy_report(self):
        """Test formatting a healthy report."""
        diary_sync = CheckResult("diary_sync", "ok", 0, ["All good"], [])
        promotion_integrity = CheckResult("promotion_integrity", "ok", 0, ["Verified"], [])
        vault_note_integrity = CheckResult("vault_note_integrity", "not_checked", 0, [], [])

        report = HealthReport(
            timestamp="2026-03-19T10:00:00",
            overall_status="healthy",
            checks_performed=["diary_sync", "promotion_integrity"],
            diary_sync=diary_sync,
            promotion_integrity=promotion_integrity,
            vault_note_integrity=vault_note_integrity,
            summary="All systems operational",
            total_issues=0,
        )

        formatted = format_health_report(report)

        assert "# Cross-System Health Report ✅" in formatted
        assert "**Status:** Healthy" in formatted
        assert "**Total Issues:** 0" in formatted
        assert "### ✅ Checkpoint-to-Diary Sync" in formatted
        assert "### ✅ Promoted Pattern Integrity" in formatted
        assert "### ⏭️ Vault Note Integrity" in formatted
        assert "All good" in formatted

    def test_format_report_with_issues(self):
        """Test formatting a report with detected issues."""
        diary_sync = CheckResult(
            "diary_sync", "gap_detected", 1, ["Gap found in session-123"], ["Run memory-checkpoint-to-diary"]
        )
        promotion_integrity = CheckResult(
            "promotion_integrity",
            "orphaned_flags",
            2,
            ["Pattern1 orphaned", "Pattern2 missing"],
            ["Clear flags", "Regenerate patterns"],
        )
        vault_note_integrity = CheckResult("vault_note_integrity", "not_checked", 0, [], [])

        report = HealthReport(
            timestamp="2026-03-19T10:00:00",
            overall_status="drift_detected",
            checks_performed=["diary_sync", "promotion_integrity"],
            diary_sync=diary_sync,
            promotion_integrity=promotion_integrity,
            vault_note_integrity=vault_note_integrity,
            summary="Found 3 drift issues",
            total_issues=3,
        )

        formatted = format_health_report(report)

        assert "# Cross-System Health Report ⚠️" in formatted
        assert "**Status:** Drift_Detected" in formatted
        assert "**Total Issues:** 3" in formatted
        assert "### ⚠️ Checkpoint-to-Diary Sync" in formatted
        assert "### ⚠️ Promoted Pattern Integrity" in formatted
        assert "Gap found in session-123" in formatted
        assert "**Recommendations:**" in formatted
        assert "- Run memory-checkpoint-to-diary" in formatted
        assert "- Clear flags" in formatted

    def test_format_report_with_errors(self):
        """Test formatting a report with critical errors."""
        diary_sync = CheckResult("diary_sync", "error", 1, ["Connection failed"], ["Check connectivity"])
        promotion_integrity = CheckResult("promotion_integrity", "not_checked", 0, [], [])
        vault_note_integrity = CheckResult("vault_note_integrity", "not_checked", 0, [], [])

        report = HealthReport(
            timestamp="2026-03-19T10:00:00",
            overall_status="critical",
            checks_performed=["diary_sync"],
            diary_sync=diary_sync,
            promotion_integrity=promotion_integrity,
            vault_note_integrity=vault_note_integrity,
            summary="System errors encountered",
            total_issues=1,
        )

        formatted = format_health_report(report)

        assert "# Cross-System Health Report ❌" in formatted
        assert "**Status:** Critical" in formatted
        assert "### ❌ Checkpoint-to-Diary Sync" in formatted
        assert "Connection failed" in formatted
        assert "- Check connectivity" in formatted


# Integration-style tests (mocking MCP clients)


class TestCrossSystemHealthIntegration:
    """Integration-style tests with realistic mock data."""

    def setup_method(self):
        """Set up integration test fixtures."""
        self.checker = CrossSystemHealthChecker()
        self.mock_fusion_client = Mock()
        self.mock_vault_client = Mock()

    def test_realistic_healthy_system(self):
        """Test a realistic scenario with healthy cross-system state."""
        # Mock realistic checkpoint
        self.mock_fusion_client.get_last_checkpoint.return_value = {
            "result": {
                "checkpoint": {
                    "session_id": "autonomy-synthesis-final-2026-03-19",
                    "summary": "Completed dual memory integration Phase 6",
                    "last_event_seq": 200,
                }
            }
        }

        # Mock diary entry found
        self.mock_vault_client.vault_search.side_effect = [
            {
                "results": [
                    {"path": "00-inbox/daily-brief-2026-03-19.md", "snippet": "autonomy-synthesis-final-2026-03-19"}
                ]
            },
            {"results": []},
        ]

        # Mock promoted patterns
        self.mock_fusion_client.query_memory.return_value = {
            "result": {
                "results": [
                    {
                        "id": "pattern-cross-system-health",
                        "text": "Cross-system health monitoring pattern",
                        "metadata": {
                            "promoted_to_vault": True,
                            "vault_path": "20-agent-patterns/cross-system-health.md",
                        },
                    }
                ]
            }
        }

        # Mock vault pattern exists
        self.mock_vault_client.vault_read.return_value = {
            "content": "# Cross-System Health Pattern\nImplemented in Phase 6..."
        }

        result = self.checker.run_cross_system_health_check(self.mock_fusion_client, self.mock_vault_client)

        assert result.overall_status == "healthy"
        assert result.total_issues == 0
        assert result.diary_sync.status == "ok"
        assert result.promotion_integrity.status == "ok"
        assert "synchronized" in result.summary

    def test_realistic_drift_scenario(self):
        """Test a realistic scenario with cross-system drift."""
        # Mock checkpoint without diary entry
        self.mock_fusion_client.get_last_checkpoint.return_value = {
            "result": {
                "checkpoint": {
                    "session_id": "forgotten-session-2026-03-18",
                    "summary": "Session without diary generation",
                    "last_event_seq": 150,
                }
            }
        }

        # Mock no diary entries found
        self.mock_vault_client.vault_search.return_value = {"results": []}

        # Mock orphaned promoted patterns
        self.mock_fusion_client.query_memory.return_value = {
            "result": {
                "results": [
                    {
                        "id": "orphaned-pattern-1",
                        "metadata": {"promoted_to_vault": True, "vault_path": "20-agent-patterns/deleted-pattern.md"},
                    },
                    {
                        "id": "orphaned-pattern-2",
                        "text": "Pattern without vault path",
                        "metadata": {"promoted_to_vault": True},
                    },
                ]
            }
        }

        # Mock vault reads fail (files deleted)
        self.mock_vault_client.vault_read.return_value = None
        # Mock search fails too
        self.mock_vault_client.vault_search.return_value = {"results": []}

        result = self.checker.run_cross_system_health_check(self.mock_fusion_client, self.mock_vault_client)

        assert result.overall_status == "drift_detected"
        assert result.total_issues == 3  # 1 missing diary + 2 orphaned patterns
        assert result.diary_sync.status == "gap_detected"
        assert result.promotion_integrity.status == "orphaned_flags"
        assert "drift issues" in result.summary

        formatted_report = format_health_report(result)
        assert "⚠️" in formatted_report
        assert "forgotten-session-2026-03-18" in formatted_report
