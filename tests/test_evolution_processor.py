"""Integration tests for EvolutionProcessor, register_existing_skills, and _run_skill_analysis wiring.

Phase 3 integration tests covering:
A. EvolutionProcessor.process_one / process_batch / get_stats (~15 tests)
B. EvolutionProcessor.run_health_scan (~5 tests)
C. register_existing_skills idempotency and error handling (~5 tests)
D. _run_skill_analysis wiring (import-level test)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from skills.evolution_processor import EvolutionProcessor
from skills.evolution_queue import EvolutionQueue, EvolutionRequest
from skills.skill_evolver import EvolutionError, SkillEvolver
from skills.skill_record import ExecutionStats, Origin, SkillLineage, SkillVersion
from skills.version_store import SkillVersionStore, register_existing_skills

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_skill_version(
    name: str = "test-skill",
    skill_id: str = "test-skill__imp_abc12345",
    origin: Origin = Origin.FIXED,
    generation: int = 1,
) -> SkillVersion:
    """Create a minimal SkillVersion for test assertions."""
    return SkillVersion(
        skill_id=skill_id,
        name=name,
        description=f"Test skill: {name}",
        path=f"/skills/{name}",
        lineage=SkillLineage(origin=origin, generation=generation),
        is_active=True,
        version="1.0.0",
        stats=ExecutionStats(),
    )


@pytest.fixture
def tmp_state_dir(tmp_path: Path) -> Path:
    """Provide a temporary state directory for EvolutionQueue persistence."""
    state = tmp_path / "state"
    state.mkdir()
    return state


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    """Provide a temporary SQLite db path for SkillVersionStore."""
    return tmp_path / "skills.db"


@pytest.fixture
def version_store(tmp_db: Path) -> SkillVersionStore:
    """Real SkillVersionStore backed by a temp SQLite database."""
    return SkillVersionStore(db_path=str(tmp_db))


@pytest.fixture
def evolution_queue(tmp_state_dir: Path) -> EvolutionQueue:
    """Real EvolutionQueue backed by a temp state directory."""
    return EvolutionQueue(state_dir=str(tmp_state_dir))


@pytest.fixture
def mock_evolver() -> MagicMock:
    """Mock SkillEvolver — all LLM-calling methods replaced."""
    evolver = MagicMock(spec=SkillEvolver)
    evolver.evolve_fix.return_value = None
    evolver.evolve_derived.return_value = None
    evolver.evolve_captured.return_value = None
    evolver.detect_improvement_candidates.return_value = []
    evolver.detect_novel_patterns.return_value = []
    return evolver


@pytest.fixture
def processor(
    version_store: SkillVersionStore,
    evolution_queue: EvolutionQueue,
    mock_evolver: MagicMock,
) -> EvolutionProcessor:
    """EvolutionProcessor wired to real queue + mock evolver."""
    return EvolutionProcessor(
        version_store=version_store,
        evolution_queue=evolution_queue,
        skill_evolver=mock_evolver,
    )


@pytest.fixture
def processor_with_telegram(
    version_store: SkillVersionStore,
    evolution_queue: EvolutionQueue,
    mock_evolver: MagicMock,
) -> tuple[EvolutionProcessor, MagicMock]:
    """Processor with a mock telegram_fn attached."""
    telegram_fn = MagicMock()
    proc = EvolutionProcessor(
        version_store=version_store,
        evolution_queue=evolution_queue,
        skill_evolver=mock_evolver,
        telegram_fn=telegram_fn,
    )
    return proc, telegram_fn


def _enqueue_request(
    queue: EvolutionQueue,
    skill_name: str = "my-skill",
    skill_id: str = "my-skill__imp_aaa11111",
    evolution_type: str = "FIX",
    direction: str = "fix broken output",
    priority: int = 2,
    task_id: str = "task-001",
    context: str = "",
) -> EvolutionRequest:
    """Helper to build and enqueue a request, returning it for assertions."""
    req = EvolutionRequest(
        skill_id=skill_id,
        skill_name=skill_name,
        evolution_type=evolution_type,
        direction=direction,
        priority=priority,
        task_id=task_id,
        context=context,
    )
    assert queue.enqueue(req), f"Failed to enqueue {skill_name}/{evolution_type}"
    return req


# ===================================================================
# A. EvolutionProcessor tests
# ===================================================================


class TestProcessOne:
    """Tests for EvolutionProcessor.process_one()."""

    def test_process_one_fix_success(
        self, processor: EvolutionProcessor, evolution_queue: EvolutionQueue, mock_evolver: MagicMock
    ):
        """FIX evolution type dispatches to evolve_fix and returns success dict."""
        new_sv = _make_skill_version(name="my-skill", skill_id="my-skill__v_1_ff00ff00")
        mock_evolver.evolve_fix.return_value = new_sv

        _enqueue_request(evolution_queue, evolution_type="FIX", direction="fix parsing bug")
        result = processor.process_one()

        assert result is not None
        assert result["success"] is True
        assert result["type"] == "FIX"
        assert result["new_skill_id"] == new_sv.skill_id
        assert result["skill_name"] == new_sv.name
        assert result["error"] is None
        mock_evolver.evolve_fix.assert_called_once()

    def test_process_one_derived_success(
        self, processor: EvolutionProcessor, evolution_queue: EvolutionQueue, mock_evolver: MagicMock
    ):
        """DERIVED evolution type dispatches to evolve_derived."""
        new_sv = _make_skill_version(
            name="derived-skill", skill_id="derived-skill__v_1_bb00bb00", origin=Origin.DERIVED
        )
        mock_evolver.evolve_derived.return_value = new_sv

        _enqueue_request(
            evolution_queue,
            skill_name="derived-skill",
            skill_id="derived-skill__imp_orig",
            evolution_type="DERIVED",
            direction="enhance for JSON tasks",
        )
        result = processor.process_one()

        assert result is not None
        assert result["success"] is True
        assert result["type"] == "DERIVED"
        assert result["new_skill_id"] == new_sv.skill_id
        mock_evolver.evolve_derived.assert_called_once()

    def test_process_one_captured_success(
        self, processor: EvolutionProcessor, evolution_queue: EvolutionQueue, mock_evolver: MagicMock
    ):
        """CAPTURED evolution type dispatches to evolve_captured."""
        new_sv = _make_skill_version(
            name="new-pattern-skill", skill_id="new-pattern__v_0_cc00cc00", origin=Origin.CAPTURED
        )
        mock_evolver.evolve_captured.return_value = new_sv

        _enqueue_request(
            evolution_queue,
            skill_name="",
            skill_id="",
            evolution_type="CAPTURED",
            direction="novel deployment pattern",
            task_id="task-010",
            context="task_ids=task-010,task-011",
        )
        result = processor.process_one()

        assert result is not None
        assert result["success"] is True
        assert result["type"] == "CAPTURED"
        assert result["new_skill_id"] == new_sv.skill_id
        mock_evolver.evolve_captured.assert_called_once()
        # Verify task_examples were parsed from context
        call_kwargs = mock_evolver.evolve_captured.call_args
        assert "task-010" in call_kwargs.kwargs.get("task_examples", call_kwargs[1].get("task_examples", []))

    def test_process_one_empty_queue(self, processor: EvolutionProcessor):
        """Returns None when the queue is empty."""
        result = processor.process_one()
        assert result is None

    def test_process_one_evolution_error(
        self, processor: EvolutionProcessor, evolution_queue: EvolutionQueue, mock_evolver: MagicMock
    ):
        """EvolutionError is caught gracefully — result has success=False."""
        mock_evolver.evolve_fix.side_effect = EvolutionError("LLM refused to fix")

        _enqueue_request(evolution_queue)
        result = processor.process_one()

        assert result is not None
        assert result["success"] is False
        assert "LLM refused to fix" in result["error"]

    def test_process_one_unexpected_error(
        self, processor: EvolutionProcessor, evolution_queue: EvolutionQueue, mock_evolver: MagicMock
    ):
        """RuntimeError is caught gracefully with 'unexpected' prefix."""
        mock_evolver.evolve_fix.side_effect = RuntimeError("segfault simulation")

        _enqueue_request(evolution_queue)
        result = processor.process_one()

        assert result is not None
        assert result["success"] is False
        assert "unexpected" in result["error"]
        assert "segfault simulation" in result["error"]

    def test_process_one_returns_none(
        self, processor: EvolutionProcessor, evolution_queue: EvolutionQueue, mock_evolver: MagicMock
    ):
        """When evolver returns None, result is success=False with descriptive error."""
        mock_evolver.evolve_fix.return_value = None

        _enqueue_request(evolution_queue)
        result = processor.process_one()

        assert result is not None
        assert result["success"] is False
        assert result["new_skill_id"] is None
        assert "None" in result["error"]

    def test_unknown_evolution_type(
        self, processor: EvolutionProcessor, evolution_queue: EvolutionQueue, mock_evolver: MagicMock, caplog
    ):
        """Unknown evolution type logs error and returns success=False."""
        _enqueue_request(evolution_queue, evolution_type="INVALID")
        with caplog.at_level(logging.ERROR, logger="skills.evolution_processor"):
            result = processor.process_one()

        assert result is not None
        assert result["success"] is False
        # _dispatch_evolution returns None for unknown type -> error is about None
        assert result["error"] is not None


class TestProcessBatch:
    """Tests for EvolutionProcessor.process_batch()."""

    def test_process_batch_multiple(
        self, processor: EvolutionProcessor, evolution_queue: EvolutionQueue, mock_evolver: MagicMock
    ):
        """Batch processes up to max_items from queue."""
        sv1 = _make_skill_version(name="skill-a", skill_id="skill-a__v_1_aa")
        sv2 = _make_skill_version(name="skill-b", skill_id="skill-b__v_1_bb")
        sv3 = _make_skill_version(name="skill-c", skill_id="skill-c__v_1_cc")
        mock_evolver.evolve_fix.side_effect = [sv1, sv2, sv3]

        _enqueue_request(evolution_queue, skill_name="skill-a", skill_id="a__imp_1", priority=1)
        _enqueue_request(evolution_queue, skill_name="skill-b", skill_id="b__imp_2", priority=2)
        _enqueue_request(evolution_queue, skill_name="skill-c", skill_id="c__imp_3", priority=3)

        results = processor.process_batch(max_items=3)

        assert len(results) == 3
        assert all(r["success"] for r in results)
        assert results[0]["skill_name"] == "skill-a"
        assert results[1]["skill_name"] == "skill-b"
        assert results[2]["skill_name"] == "skill-c"

    def test_process_batch_partial(
        self, processor: EvolutionProcessor, evolution_queue: EvolutionQueue, mock_evolver: MagicMock
    ):
        """When queue has fewer items than max_items, processes what is available."""
        sv = _make_skill_version(name="only-skill", skill_id="only__v_1_dd")
        mock_evolver.evolve_fix.return_value = sv

        _enqueue_request(evolution_queue, skill_name="only-skill", skill_id="only__imp_1")

        results = processor.process_batch(max_items=5)

        assert len(results) == 1
        assert results[0]["success"] is True


class TestTelegramNotification:
    """Tests for telegram_fn integration in EvolutionProcessor."""

    def test_process_one_telegram_notification(
        self, processor_with_telegram, evolution_queue: EvolutionQueue, mock_evolver: MagicMock
    ):
        """telegram_fn is called on successful evolution."""
        proc, telegram_fn = processor_with_telegram
        sv = _make_skill_version(name="tg-skill", skill_id="tg__v_1_ee")
        mock_evolver.evolve_fix.return_value = sv

        _enqueue_request(evolution_queue, skill_name="tg-skill", skill_id="tg__imp_1")
        result = proc.process_one()

        assert result["success"] is True
        telegram_fn.assert_called_once()
        msg = telegram_fn.call_args[0][0]
        assert "tg-skill" in msg or "FIX" in msg

    def test_process_one_no_telegram_on_failure(
        self, processor_with_telegram, evolution_queue: EvolutionQueue, mock_evolver: MagicMock
    ):
        """telegram_fn is NOT called when evolution fails."""
        proc, telegram_fn = processor_with_telegram
        mock_evolver.evolve_fix.side_effect = EvolutionError("broken")

        _enqueue_request(evolution_queue, skill_name="fail-skill", skill_id="fail__imp_1")
        result = proc.process_one()

        assert result["success"] is False
        telegram_fn.assert_not_called()


class TestStatsTracking:
    """Tests for EvolutionProcessor.get_stats()."""

    def test_stats_tracking(
        self, processor: EvolutionProcessor, evolution_queue: EvolutionQueue, mock_evolver: MagicMock
    ):
        """get_stats reflects processed/successes/failures counts."""
        sv = _make_skill_version(name="stat-skill", skill_id="stat__v_1_ff")
        # First two succeed, third fails
        mock_evolver.evolve_fix.side_effect = [sv, sv, EvolutionError("fail")]

        _enqueue_request(evolution_queue, skill_name="stat-a", skill_id="sa__imp_1", priority=1)
        _enqueue_request(evolution_queue, skill_name="stat-b", skill_id="sb__imp_2", priority=2)
        _enqueue_request(evolution_queue, skill_name="stat-c", skill_id="sc__imp_3", priority=3)

        processor.process_batch(max_items=3)
        stats = processor.get_stats()

        assert stats["processed"] == 3
        assert stats["successes"] == 2
        assert stats["failures"] == 1
        assert stats["queue_size"] == 0

    def test_stats_initial(self, processor: EvolutionProcessor):
        """Initial stats are all zeros."""
        stats = processor.get_stats()
        assert stats["processed"] == 0
        assert stats["successes"] == 0
        assert stats["failures"] == 0


# ===================================================================
# B. Health scan tests
# ===================================================================


class TestRunHealthScan:
    """Tests for EvolutionProcessor.run_health_scan()."""

    def test_run_health_scan_with_candidates(self, processor: EvolutionProcessor, mock_evolver: MagicMock):
        """Improvement candidates are enqueued as DERIVED."""
        mock_evolver.detect_improvement_candidates.return_value = [
            {"skill_id": "s1__imp_aa", "skill_name": "skill-1", "suggestion": "specialize for JSON"},
            {"skill_id": "s2__imp_bb", "skill_name": "skill-2", "suggestion": "add retry logic"},
        ]
        mock_evolver.detect_novel_patterns.return_value = []

        result = processor.run_health_scan()

        assert result["candidates_found"] == 2
        assert result["enqueued"] == 2
        assert result["patterns_found"] == 0
        # Verify items were enqueued
        assert processor.evolution_queue.queue_size() == 2

    def test_run_health_scan_with_patterns(self, processor: EvolutionProcessor, mock_evolver: MagicMock):
        """Novel patterns are enqueued as CAPTURED."""
        mock_evolver.detect_improvement_candidates.return_value = []
        mock_evolver.detect_novel_patterns.return_value = [
            {"task_ids": ["t1", "t2"], "description_pattern": "deployment automation pattern"},
        ]

        result = processor.run_health_scan()

        assert result["patterns_found"] == 1
        assert result["enqueued"] == 1
        assert result["candidates_found"] == 0

    def test_run_health_scan_empty(self, processor: EvolutionProcessor, mock_evolver: MagicMock):
        """When nothing detected, enqueued=0 and queue stays empty."""
        mock_evolver.detect_improvement_candidates.return_value = []
        mock_evolver.detect_novel_patterns.return_value = []

        result = processor.run_health_scan()

        assert result["candidates_found"] == 0
        assert result["patterns_found"] == 0
        assert result["enqueued"] == 0
        assert processor.evolution_queue.queue_size() == 0

    def test_run_health_scan_with_dashboard(
        self, version_store: SkillVersionStore, evolution_queue: EvolutionQueue, mock_evolver: MagicMock
    ):
        """Dashboard.get_health_summary is called when dashboard is present."""
        mock_dashboard = MagicMock()
        mock_dashboard.get_health_summary.return_value = {
            "unhealthy_skills": 1,
            "frozen_skills": ["bad-skill"],
        }
        proc = EvolutionProcessor(
            version_store=version_store,
            evolution_queue=evolution_queue,
            skill_evolver=mock_evolver,
            dashboard=mock_dashboard,
        )

        result = proc.run_health_scan()

        mock_dashboard.get_health_summary.assert_called_once()
        assert result["health"]["unhealthy_skills"] == 1
        assert "bad-skill" in result["health"]["frozen_skills"]

    def test_run_health_scan_error_handling(self, processor: EvolutionProcessor, mock_evolver: MagicMock, caplog):
        """Errors in detect methods are caught; scan continues and returns partial results."""
        mock_evolver.detect_improvement_candidates.side_effect = RuntimeError("DB down")
        mock_evolver.detect_novel_patterns.return_value = [
            {"task_ids": ["tx"], "description_pattern": "novel pattern"},
        ]

        with caplog.at_level(logging.ERROR, logger="skills.evolution_processor"):
            result = processor.run_health_scan()

        assert result["candidates_found"] == 0  # failed branch returns 0
        assert result["patterns_found"] == 1  # second branch succeeded
        assert result["enqueued"] == 1


# ===================================================================
# C. register_existing_skills tests
# ===================================================================


@dataclass
class _FakeSkill:
    """Minimal stand-in for tools.skills.Skill (dataclass)."""

    name: str
    description: str = "A test skill"
    version: str = "1.0.0"
    path: Path = field(default_factory=Path)
    tags: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    body: str = ""
    raw: str = ""


class TestRegisterExistingSkills:
    """Tests for register_existing_skills()."""

    def test_register_new_skills(self, version_store: SkillVersionStore, tmp_path: Path):
        """New skills are registered and count is returned."""
        skill_dir = tmp_path / "skill-alpha"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("# Alpha Skill\nDoes alpha things.")

        skills = [
            _FakeSkill(name="skill-alpha", path=skill_dir / "SKILL.md"),
            _FakeSkill(name="skill-beta", path=Path(".")),
        ]

        count = register_existing_skills(version_store, skills)

        assert count == 2
        # Verify both are retrievable
        assert version_store.get_active_version("skill-alpha") is not None
        assert version_store.get_active_version("skill-beta") is not None

    def test_register_idempotent(self, version_store: SkillVersionStore):
        """Registering the same skills twice returns 0 the second time."""
        skills = [_FakeSkill(name="idem-skill")]

        first_count = register_existing_skills(version_store, skills)
        second_count = register_existing_skills(version_store, skills)

        assert first_count == 1
        assert second_count == 0

    def test_register_partial_failure(self, version_store: SkillVersionStore):
        """When one skill registration fails, the rest still proceed."""
        skills = [
            _FakeSkill(name="good-skill-1"),
            _FakeSkill(name="good-skill-2"),
        ]

        # Patch register_skill to fail on the first call but succeed on the second
        original_register = version_store.register_skill
        call_count = {"n": 0}

        def _register_with_fail(sv):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise RuntimeError("Simulated DB error")
            return original_register(sv)

        with patch.object(version_store, "register_skill", side_effect=_register_with_fail):
            count = register_existing_skills(version_store, skills)

        # First fails, second succeeds
        assert count == 1

    def test_register_empty_list(self, version_store: SkillVersionStore):
        """Empty skill list returns 0."""
        count = register_existing_skills(version_store, [])
        assert count == 0

    def test_register_sets_imported_origin(self, version_store: SkillVersionStore):
        """Registered skills have origin=IMPORTED and generation=0."""
        skills = [_FakeSkill(name="origin-test-skill")]
        register_existing_skills(version_store, skills)

        sv = version_store.get_active_version("origin-test-skill")
        assert sv is not None
        assert sv.lineage.origin == Origin.IMPORTED
        assert sv.lineage.generation == 0
        assert sv.is_active is True


# ===================================================================
# D. _run_skill_analysis wiring test
# ===================================================================


class TestRunSkillAnalysisWiring:
    """Test that _run_skill_analysis can be imported and invoked with mocks.

    The refactored _run_skill_analysis uses module-level singletons:
    - watcher._skill_version_store (SkillVersionStore)
    - watcher._skill_evolution_queue (EvolutionQueue)
    So we patch those module-level variables, not constructors.
    """

    def test_run_skill_analysis_no_skills_early_return(self):
        """Empty selected_names list returns None immediately."""
        from watcher import _run_skill_analysis

        result = _run_skill_analysis(
            stem="test-task",
            task_text="Do something",
            selected_names=[],
            log_text="some log output",
            passed=True,
            exit_code=0,
        )
        assert result is None

    def test_run_skill_analysis_no_store_early_return(self):
        """Returns None when _skill_version_store is None."""
        import watcher
        from watcher import _run_skill_analysis

        with patch.object(watcher, "_skill_version_store", None):
            result = _run_skill_analysis(
                stem="no-store-task",
                task_text="Do something",
                selected_names=["my-skill"],
                log_text="trace",
                passed=True,
                exit_code=0,
            )
        assert result is None

    def test_run_skill_analysis_with_selected_skills(self):
        """Full wiring: selected skills trigger analyze -> update_stats -> store_analysis."""
        import watcher
        from watcher import _run_skill_analysis

        mock_analysis = MagicMock()
        mock_analysis.evolution_suggestions = []
        mock_analysis.overall_quality = 0.75
        mock_analysis.skill_judgments = [
            MagicMock(skill_id="sk1", applied=True, completed=True),
        ]

        mock_analyzer_instance = MagicMock()
        mock_analyzer_instance.analyze.return_value = mock_analysis

        mock_store = MagicMock()

        with (
            patch.object(watcher, "_skill_version_store", mock_store),
            patch("skills.execution_analyzer.ExecutionAnalyzer", return_value=mock_analyzer_instance),
        ):
            result = _run_skill_analysis(
                stem="wired-task",
                task_text="Analyze this task",
                selected_names=["my-skill"],
                log_text="execution trace here",
                passed=True,
                exit_code=0,
            )

        # Verify full wiring: analyze -> update_stats -> store_analysis
        mock_analyzer_instance.analyze.assert_called_once()
        mock_analyzer_instance.update_stats.assert_called_once_with(mock_analysis)
        mock_store.store_analysis.assert_called_once_with(mock_analysis)
        assert result is mock_analysis

    def test_run_skill_analysis_with_suggestions_enqueues(self):
        """When analysis produces evolution suggestions, they are enqueued."""
        import watcher
        from watcher import _run_skill_analysis

        suggestion = MagicMock()
        suggestion.target_skill_id = "sk__imp_abc"
        suggestion.target_skill_name = "my-skill"
        suggestion.type = "FIX"
        suggestion.direction = "fix the output format"
        suggestion.priority = 2

        mock_analysis = MagicMock()
        mock_analysis.evolution_suggestions = [suggestion]
        mock_analysis.overall_quality = 0.3
        mock_analysis.skill_judgments = []

        mock_analyzer_instance = MagicMock()
        mock_analyzer_instance.analyze.return_value = mock_analysis

        mock_store = MagicMock()
        mock_queue = MagicMock()

        with (
            patch.object(watcher, "_skill_version_store", mock_store),
            patch.object(watcher, "_skill_evolution_queue", mock_queue),
            patch("skills.execution_analyzer.ExecutionAnalyzer", return_value=mock_analyzer_instance),
        ):
            _run_skill_analysis(
                stem="suggestion-task",
                task_text="Task with issues",
                selected_names=["my-skill"],
                log_text="error in output",
                passed=False,
                exit_code=1,
            )

        # Verify suggestion was enqueued
        mock_queue.enqueue.assert_called_once()
        enqueued_req = mock_queue.enqueue.call_args[0][0]
        assert enqueued_req.skill_name == "my-skill"
        assert enqueued_req.evolution_type == "FIX"
        assert enqueued_req.direction == "fix the output format"


# ===================================================================
# Edge-case / robustness tests
# ===================================================================


class TestEdgeCases:
    """Additional edge-case and robustness tests."""

    def test_process_one_complete_evolution_error(
        self, processor: EvolutionProcessor, evolution_queue: EvolutionQueue, mock_evolver: MagicMock, caplog
    ):
        """Error in complete_evolution is logged but does not crash."""
        sv = _make_skill_version()
        mock_evolver.evolve_fix.return_value = sv

        _enqueue_request(evolution_queue)

        # Patch complete_evolution to raise
        with (
            patch.object(evolution_queue, "complete_evolution", side_effect=RuntimeError("disk full")),
            caplog.at_level(logging.ERROR, logger="skills.evolution_processor"),
        ):
            result = processor.process_one()

        # Evolution itself succeeded even though complete_evolution failed
        assert result is not None
        assert result["success"] is True

    def test_process_batch_stops_on_none(
        self, processor: EvolutionProcessor, evolution_queue: EvolutionQueue, mock_evolver: MagicMock
    ):
        """process_batch stops iteration when process_one returns None (empty queue)."""
        sv = _make_skill_version(name="single", skill_id="single__v_1_xx")
        mock_evolver.evolve_fix.return_value = sv

        _enqueue_request(evolution_queue, skill_name="single", skill_id="single__imp_1")

        results = processor.process_batch(max_items=10)

        # Only 1 item in queue, so only 1 result
        assert len(results) == 1

    def test_captured_context_parsing(
        self, processor: EvolutionProcessor, evolution_queue: EvolutionQueue, mock_evolver: MagicMock
    ):
        """CAPTURED evolution correctly parses task_ids from context string."""
        sv = _make_skill_version(name="captured", skill_id="captured__v_0_zz", origin=Origin.CAPTURED)
        mock_evolver.evolve_captured.return_value = sv

        _enqueue_request(
            evolution_queue,
            skill_name="",
            skill_id="",
            evolution_type="CAPTURED",
            direction="new pattern",
            task_id="task-a",
            context="task_ids=task-a,task-b,task-c",
        )

        processor.process_one()

        call_kwargs = mock_evolver.evolve_captured.call_args
        # Access keyword args (could be positional or keyword depending on dispatch)
        task_examples = call_kwargs.kwargs.get("task_examples", [])
        assert "task-a" in task_examples
        assert "task-b" in task_examples
        assert "task-c" in task_examples

    def test_captured_pads_task_examples(
        self, processor: EvolutionProcessor, evolution_queue: EvolutionQueue, mock_evolver: MagicMock
    ):
        """CAPTURED with a single task_id pads to minimum 2 examples."""
        sv = _make_skill_version(name="cap-pad", skill_id="cap__v_0_pp", origin=Origin.CAPTURED)
        mock_evolver.evolve_captured.return_value = sv

        _enqueue_request(
            evolution_queue,
            skill_name="",
            skill_id="",
            evolution_type="CAPTURED",
            direction="single task pattern",
            task_id="solo-task",
            context="task_ids=solo-task",
        )

        processor.process_one()

        call_kwargs = mock_evolver.evolve_captured.call_args
        task_examples = call_kwargs.kwargs.get("task_examples", [])
        assert len(task_examples) >= 2
