#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["pytest", "pytest-cov"]
# ///
"""
Goal Tracker — automated goal progress assessment for strategic planning.

P15 implementation: Reads goals from vision.md and assesses progress
using pluggable assessors.

Usage:
    # Assess all goals
    python goal_tracker.py assess

    # Assess specific goal
    python goal_tracker.py assess --goal G5

    # Show goal summary
    python goal_tracker.py summary

    # Export for strategic planner
    python goal_tracker.py export
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol

# -----------------------------------------------------------------------------
# Test Result Cache (retro-ai-014 optimization)
# -----------------------------------------------------------------------------
# Strategic planning runs G1 (coverage) and G3 (stability) assessments.
# Both previously ran full pytest, taking ~20 minutes total.
# This cache ensures pytest runs ONCE per planning cycle.


@dataclass
class TestResultCache:
    """Cache for pytest results to avoid redundant test runs.

    Used within a single planning cycle to share results between
    G1 (test coverage) and G3 (stability) assessments.
    """

    tests_passed: bool | None = None
    coverage_percent: float | None = None
    coverage_data: dict | None = None
    ran_at: float = 0.0
    # When the coverage number itself was read from a trusted source —
    # unlike ran_at, this is NOT refreshed by test-only runs (G3), so a
    # newer Python commit invalidates the cached percent (W1-P2).
    coverage_read_at: float = 0.0
    source: str = ""  # "pytest_run" or "cache"

    # Cache is valid for 5 minutes (single planning cycle)
    CACHE_TTL_SECONDS: int = 300

    def is_valid(self) -> bool:
        """Check if cache is still valid."""
        if self.ran_at == 0.0:
            return False
        age = time.time() - self.ran_at
        return age < self.CACHE_TTL_SECONDS

    def invalidate(self) -> None:
        """Invalidate the cache."""
        self.tests_passed = None
        self.coverage_percent = None
        self.coverage_data = None
        self.ran_at = 0.0
        self.coverage_read_at = 0.0
        self.source = ""


# Global cache instance - shared across assessors within a planning cycle
_test_cache = TestResultCache()


def reset_test_cache() -> None:
    """Reset the global test cache.

    Call this between planning cycles or in tests to ensure fresh data.
    """
    global _test_cache
    _test_cache.invalidate()


def get_test_cache() -> TestResultCache:
    """Get the current test cache (for testing/debugging)."""
    return _test_cache


# -----------------------------------------------------------------------------
# Data Structures
# -----------------------------------------------------------------------------


class GoalStatus(str, Enum):
    """Goal progress status."""

    ON_TRACK = "on_track"
    AT_RISK = "at_risk"
    BLOCKED = "blocked"
    COMPLETE = "complete"
    NOT_STARTED = "not_started"
    VERIFIED = "verified"


@dataclass
class GoalDefinition:
    """A goal from vision.md."""

    id: str  # "G5"
    name: str  # "Autonomy"
    description: str  # "Enable fully autonomous operation"
    success_metric: str  # "Org runs without prompts for 24h"
    owner: str  # "forge-cto"
    target_value: float = 1.0  # Normalized target (1.0 = 100%)
    category: str | None = (
        None  # WS-054-002: product, revenue, infrastructure, quality, autonomy
    )
    period: str | None = None  # e.g. "Q1 2026", "Q2 2026"
    period_status: str | None = None  # "active", "complete", "planned"
    depends_on: list[str] = field(default_factory=list)  # e.g. ["G1", "G2"]


@dataclass
class GoalAssessment:
    """Result of assessing a goal's progress."""

    goal_id: str
    goal_name: str
    description: str
    success_metric: str
    owner: str
    current_value: float  # 0.0 to 1.0 (normalized)
    target_value: float  # Usually 1.0
    progress_percent: int  # 0-100
    status: GoalStatus
    status_reason: str  # Why this status
    blockers: list[str] = field(default_factory=list)
    next_actions: list[str] = field(default_factory=list)
    assessed_at: str = ""
    raw_data: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)  # copied from GoalDefinition
    # WS-113-003: Velocity tracking (percent change per day)
    velocity_percent_per_day: float = 0.0
    velocity_trend: str = "unknown"  # "improving", "stalled", "regressing"

    def __post_init__(self):
        if not self.assessed_at:
            self.assessed_at = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        d = asdict(self)
        d["status"] = self.status.value
        return d


# -----------------------------------------------------------------------------
# Goal Assessor Protocol
# -----------------------------------------------------------------------------


class GoalAssessor(Protocol):
    """Protocol for goal-specific assessors."""

    def assess(self, goal: GoalDefinition, company_dir: Path) -> GoalAssessment:
        """Assess progress toward a goal."""
        ...


# -----------------------------------------------------------------------------
# Built-in Assessors
# -----------------------------------------------------------------------------


# W1-P2 (2026-07-17): trust guards for coverage.json. A stale or subset
# measurement — or a file-count guess — must assess as unknown, never COMPLETE:
# the daemon steered on a phantom "Coverage at 100%" minted by the estimator.
G1_COVERAGE_MAX_AGE_SECONDS = 24 * 3600
G1_SCOPE_MIN_SOURCE_FILES = 10
G1_SCOPE_MIN_MEASURED_FRACTION = 0.3
_SOURCE_SKIP_DIRS = {".venv", ".git", "node_modules", "__pycache__", ".worktrees"}


def _last_commit_timestamp(
    project_root: Path, pathspec: str | None = None
) -> float | None:
    """Unix timestamp of the last commit (optionally limited to a pathspec),
    or None outside a git repo."""
    cmd = ["git", "-C", str(project_root), "log", "-1", "--format=%ct"]
    if pathspec:
        cmd += ["--", pathspec]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except Exception:
        pass
    return None


def _is_test_relpath(rel: Path) -> bool:
    return (
        rel.name.startswith("test_")
        or rel.name == "conftest.py"
        or "tests" in rel.parts
    )


def _count_source_py(root: Path) -> int:
    """Count non-test .py files under root, skipping junk directories."""
    count = 0
    try:
        for path in root.rglob("*.py"):
            try:
                rel = path.relative_to(root)
            except ValueError:
                continue
            if any(part in _SOURCE_SKIP_DIRS for part in rel.parts):
                continue
            if _is_test_relpath(rel):
                continue
            count += 1
    except OSError:
        pass
    return count


def _expected_measured_count(project_root: Path) -> int:
    """How many .py files the project's coverage setup would measure.

    Preference order: [tool.coverage.run] source dirs from pyproject.toml,
    then git-tracked non-test .py files, then a filesystem walk. Test files
    are excluded throughout — coverage configs routinely omit them, so
    counting them would let a canonical-scope run look like a subset (this
    repo: source=[.claude/hooks] measures 176 files while a naive repo walk
    sees 1000, a third of them inside a stale gitignored .worktrees copy).
    """
    sources = None
    try:
        import tomllib

        with open(project_root / "pyproject.toml", "rb") as f:
            cfg = tomllib.load(f)
        sources = cfg.get("tool", {}).get("coverage", {}).get("run", {}).get("source")
    except Exception:
        sources = None
    if isinstance(sources, list) and sources:
        count = 0
        for src in sources:
            src_root = project_root if src == "." else project_root / src
            if src_root.is_dir():
                count += _count_source_py(src_root)
        if count:
            return count

    try:
        result = subprocess.run(
            ["git", "-C", str(project_root), "ls-files", "--", "*.py"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            tracked = [
                line
                for line in result.stdout.splitlines()
                if line.strip() and not _is_test_relpath(Path(line))
            ]
            if tracked:
                return len(tracked)
    except Exception:
        pass

    return _count_source_py(project_root)


def _read_trusted_coverage(
    coverage_file: Path, project_root: Path
) -> tuple[float | None, str]:
    """Read coverage.json only if it is fresh and full-scope.

    Returns (percent_covered, "") when trustworthy, else (None, reason).
    Never raises: a malformed file must degrade to "unknown" for G1, not
    abort the whole goal-assessment batch.
    """
    try:
        with open(coverage_file) as f:
            cov_data = json.load(f)
        mtime = coverage_file.stat().st_mtime

        totals = cov_data.get("totals") or {}
        if not isinstance(totals, dict):
            return None, "coverage.json has no totals.percent_covered"
        percent = totals.get("percent_covered")
        if not isinstance(percent, (int, float)):
            return None, "coverage.json has no totals.percent_covered"

        # Freshness: a measurement older than the last change to Python
        # code describes code that no longer exists (docs/queue commits do
        # not invalidate it). Outside git, fall back to a wall-clock cap.
        commit_ts = _last_commit_timestamp(project_root, pathspec="*.py")
        if commit_ts is not None:
            if mtime < commit_ts:
                return None, "coverage.json predates the last Python change (stale)"
        elif (time.time() - mtime) > G1_COVERAGE_MAX_AGE_SECONDS:
            return None, "coverage.json older than 24h with no git history (stale)"

        # Scope: a run that measured only a slice of the configured source
        # says nothing about total coverage (a single-module cov run can
        # honestly read 100%). Compare against what the coverage config
        # itself would measure, not a naive repo-wide file count.
        files = cov_data.get("files") or {}
        measured = len(files) if isinstance(files, dict) else 0
        expected = _expected_measured_count(project_root)
        if expected >= G1_SCOPE_MIN_SOURCE_FILES and measured < (
            G1_SCOPE_MIN_MEASURED_FRACTION * expected
        ):
            return None, (
                f"coverage.json measures {measured} of ~{expected} source "
                "files expected by the coverage config (subset run)"
            )

        # Provenance: the measured files must be THIS project's files — a
        # coverage.json copied from (or generated for) another repo would
        # otherwise pass the count check and mint a phantom COMPLETE.
        if isinstance(files, dict) and files:
            sample = list(files)[:50]
            present = sum(1 for k in sample if (project_root / k).exists())
            if present < 0.5 * len(sample):
                return None, (
                    "coverage.json measures files not present in this "
                    "project (foreign measurement)"
                )

        return float(percent), ""
    except Exception:
        return None, "coverage.json unreadable or malformed"


def assess_test_coverage(
    goal: GoalDefinition, company_dir: Path, *, lightweight: bool = False
) -> GoalAssessment:
    """Assess G1: Test coverage goal.

    WS-013-005: Actually runs pytest with coverage instead of estimating.
    retro-ai-014: Uses cache to avoid redundant test runs with G3.
    P39: lightweight mode skips pytest entirely — uses existing coverage.json
    or file-based estimation.  Used by daemon to avoid 5-10 min blocking.
    W1-P2: only fresh, full-scope measurements count; without one the goal
    assesses as unknown (never COMPLETE from an estimate or a stale file).
    """
    global _test_cache

    project_root = company_dir.parent
    coverage_file = project_root / "coverage.json"
    measured_value: float | None = None
    distrust_reason = ""
    source = ""
    ran_tests = False
    tests_passed = False

    # retro-ai-014: Check cache first. The cached percent is only honored
    # while no Python change postdates the read it came from — otherwise a
    # freshly-stale measurement would keep reporting COMPLETE for the TTL
    # (and G3's test-only refreshes of ran_at could revive it indefinitely).
    if _test_cache.is_valid() and _test_cache.coverage_percent is not None:
        commit_ts = _last_commit_timestamp(project_root, pathspec="*.py")
        read_at = _test_cache.coverage_read_at or _test_cache.ran_at
        if commit_ts is not None and commit_ts > read_at:
            _test_cache.coverage_percent = None
            _test_cache.coverage_read_at = 0.0
        else:
            measured_value = _test_cache.coverage_percent / 100.0
            source = _test_cache.source or "cache"
            ran_tests = True
            tests_passed = _test_cache.tests_passed or False

    if measured_value is None and lightweight:
        # P39: In lightweight mode, read existing coverage.json or estimate.
        # Never run pytest — this keeps daemon cycles fast (<1s for G1).
        if coverage_file.exists():
            percent, distrust_reason = _read_trusted_coverage(
                coverage_file, project_root
            )
            if percent is not None:
                measured_value = percent / 100.0
                source = "coverage_json_file"
                ran_tests = True  # treat trusted file read as "ran"
                tests_passed = True  # assume passing if file is trusted

                _test_cache.coverage_percent = percent
                _test_cache.tests_passed = True
                _test_cache.ran_at = time.time()
                _test_cache.coverage_read_at = time.time()
                _test_cache.source = "coverage_json_file"
            else:
                _test_cache.coverage_percent = None
                _test_cache.coverage_read_at = 0.0
    elif measured_value is None:
        # WS-013-005: Actually run tests with coverage to get real data
        # WS-101: Add fcntl.flock to prevent multiple pytest processes
        # fighting over .coverage SQLite database (caused 130GB memory leak)
        lock_file_path = project_root / ".coverage.lock"
        try:
            # Mode "a", never "w": macOS APFS returns EDEADLK at open() on
            # a file with outstanding fcntl locks when opened with O_TRUNC
            # (PR #986 incident class; same rationale as QueueLock).
            with open(lock_file_path, "a") as lock_file:
                # Acquire exclusive lock - blocks if another pytest is running
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    # Use Popen for proper process control on timeout
                    proc = subprocess.Popen(
                        [
                            "uv",
                            "run",
                            "pytest",
                            "--cov=.",
                            "--cov-report=json",
                            "-q",
                            "--tb=no",
                            "--ignore=.venv",
                        ],
                        cwd=project_root,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        text=True,
                        start_new_session=True,  # Process group for clean kill
                    )
                    try:
                        stdout, stderr = proc.communicate(timeout=600)
                        ran_tests = True
                        tests_passed = proc.returncode == 0

                        # retro-ai-014: Populate cache for G3 to reuse
                        _test_cache.tests_passed = tests_passed
                        _test_cache.ran_at = time.time()
                        _test_cache.source = "pytest_run"
                    except subprocess.TimeoutExpired:
                        # WS-101: Kill entire process group to prevent zombie
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        except (ProcessLookupError, PermissionError):
                            pass
                        proc.wait()  # Reap zombie
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except Exception:
            # uv/pytest not available or lock contention, fall back
            pass

        # Read coverage.json (freshly generated or pre-existing) — same
        # trust guards apply: a run that failed to produce fresh data must
        # not silently fall back to a stale file.
        if coverage_file.exists():
            percent, distrust_reason = _read_trusted_coverage(
                coverage_file, project_root
            )
            if percent is not None:
                measured_value = percent / 100.0
                source = "pytest_run" if ran_tests else "coverage_json_file"

                # retro-ai-014: Cache coverage data
                _test_cache.coverage_percent = percent
                _test_cache.coverage_read_at = time.time()
            else:
                _test_cache.coverage_percent = None
                _test_cache.coverage_read_at = 0.0

    # W1-P2: without a trustworthy measurement the estimate is context, not
    # data — it must never set status (318 test files × 0.03 saturated at
    # "100%" and reported G1 COMPLETE while the OKR said ~15%).
    estimated_percent: float | None = None
    if measured_value is None:
        test_files = list(project_root.glob("tests/**/*.py"))
        test_files = [f for f in test_files if f.name.startswith("test_")]
        estimated_percent = min(len(test_files) * 3.0, 100.0)

    # Target is 50% coverage
    target_value = 0.5
    if measured_value is None:
        current_value = 0.0
        progress_percent = 0
        status = GoalStatus.AT_RISK
        reason = "No trustworthy coverage measurement"
        if distrust_reason:
            reason += f" ({distrust_reason})"
        status_reason = (
            f"{reason}; test-file estimate ~{estimated_percent:.0f}% not used"
        )
    else:
        current_value = measured_value
        progress = min(current_value / target_value, 1.0)
        progress_percent = int(progress * 100)

        if current_value >= target_value:
            status = GoalStatus.COMPLETE
            status_reason = f"Coverage at {current_value * 100:.0f}% meets target {target_value * 100:.0f}%"
        elif progress >= 0.8:
            status = GoalStatus.ON_TRACK
            status_reason = f"Coverage at {current_value * 100:.0f}%, close to target"
        elif progress >= 0.5:
            status = GoalStatus.AT_RISK
            status_reason = f"Coverage at {current_value * 100:.0f}%, needs attention"
        else:
            status = GoalStatus.AT_RISK
            status_reason = (
                f"Coverage at {current_value * 100:.0f}%, significantly below target"
            )

    if status == GoalStatus.COMPLETE:
        next_actions = []
    elif measured_value is None:
        # W1-P3 (2026-07-18): a full-suite coverage measurement is
        # deterministic long-running infra work (bin/forge-coverage-nightly),
        # not an LLM-worker task -- a worker session cannot finish
        # `pytest --cov` over a large suite and gets correctly blocked by the
        # deliverable gate, burning a queue slot. The [INFRA-ONLY] prefix is
        # recognized by strategic_planner's queue-fill filter so this text
        # is never minted as a worker task; it still displays as-is wherever
        # next_actions is shown directly (e.g. /dashboard).
        next_actions = [
            "[INFRA-ONLY] Nightly coverage job (bin/forge-coverage-nightly) "
            "produces coverage.json; no worker action needed. Once a "
            "measurement exists, add tests for the least-covered modules."
        ]
    else:
        # Generate concrete task: name specific uncovered modules instead of
        # bare goal metric. This prevents phantom "improve coverage to 50%" tasks
        # where the diff adds tests for module X but the title claims a coverage target.
        uncovered = _find_uncovered_modules(company_dir.parent, measured_value, limit=2)
        if uncovered:
            modules_str = ", ".join(uncovered)
            next_actions = [
                f"Add tests for modules ({modules_str}) to improve coverage"
            ]
        else:
            next_actions = [
                "Add tests for least-covered modules to reach 50% coverage target"
            ]

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=target_value,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        next_actions=next_actions,
        raw_data={
            "coverage_percent": (
                measured_value * 100 if measured_value is not None else None
            ),
            "source": source
            or ("estimated" if estimated_percent is not None else "none"),
            "ran_tests": ran_tests,
            "measured": measured_value is not None,
            "distrust_reason": distrust_reason or None,
            "estimated_percent": estimated_percent,
        },
    )


def assess_tutorials(goal: GoalDefinition, company_dir: Path) -> GoalAssessment:
    """Assess G2: Tutorial adoption goal."""
    project_root = company_dir.parent

    # Count tutorial files
    docs_dir = project_root / "docs"
    tutorials_dir = docs_dir / "tutorials"
    getting_started = project_root / "GETTING_STARTED.md"

    tutorial_count = 0
    if tutorials_dir.exists():
        tutorial_count += len(list(tutorials_dir.glob("*.md")))
    if getting_started.exists():
        tutorial_count += 1

    # Target is 3 tutorials
    target = 3
    current_value = min(tutorial_count / target, 1.0)
    progress_percent = int(current_value * 100)

    if tutorial_count >= target:
        status = GoalStatus.COMPLETE
        status_reason = f"{tutorial_count} tutorials published"
    elif tutorial_count > 0:
        status = GoalStatus.ON_TRACK
        status_reason = f"{tutorial_count}/{target} tutorials, in progress"
    else:
        status = GoalStatus.NOT_STARTED
        status_reason = "No tutorials found"

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        next_actions=["Create tutorial docs"] if status != GoalStatus.COMPLETE else [],
        raw_data={"tutorial_count": tutorial_count, "target": target},
    )


def assess_stability(
    goal: GoalDefinition, company_dir: Path, *, lightweight: bool = False
) -> GoalAssessment:
    """Assess G3: Stability goal (zero P0 bugs).

    retro-ai-014: Reuses G1 test results from cache instead of running pytest again.
    P39: lightweight mode skips pytest — uses cache or assumes passing.
    """
    global _test_cache

    # Check for open issues labeled P0 or critical
    # For now, check if tests pass
    project_root = company_dir.parent
    inconclusive = False

    # retro-ai-014: Reuse cached test results from G1 assessment
    if _test_cache.is_valid() and _test_cache.tests_passed is not None:
        tests_pass = _test_cache.tests_passed
    elif lightweight:
        # P39: In lightweight mode, assume tests pass if no recent failure
        # data.  The cache from G1 lightweight assessment will have been
        # populated if coverage.json existed, so reaching here means no
        # data at all — default to True to avoid blocking daemon cycles.
        tests_pass = True
    else:
        # Fallback: run tests if cache is stale (shouldn't happen in planning cycle)
        # WS-101 pattern (see assess_test_coverage): use Popen with its own
        # process group and killpg on timeout, not subprocess.run(timeout=...)
        # — the latter only kills the immediate "uv" process, leaving any
        # subprocess pytest itself spawns (e.g. tests that shell out) as an
        # orphan that keeps consuming memory after we give up on it.
        try:
            proc = subprocess.Popen(
                ["uv", "run", "pytest", "-q", "--tb=no", "-x"],
                cwd=project_root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,  # own process group for clean kill
            )
            try:
                proc.communicate(timeout=600)  # 10 minutes - tests can take a while
                tests_pass = proc.returncode == 0

                # Populate cache for future use
                _test_cache.tests_passed = tests_pass
                _test_cache.ran_at = time.time()
                _test_cache.source = "pytest_run_g3"
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                proc.wait()  # reap zombie
                # A timeout means we don't know whether tests pass — it is
                # not evidence they fail. Don't poison the shared cache or
                # report a false "Tests failing" (retro: G7/G15 assessor
                # bugs were the assessor lying, not the product breaking).
                inconclusive = True
                tests_pass = True
        except Exception:
            inconclusive = True
            tests_pass = True

    # Also check for TODO/FIXME with P0/critical in source files only
    critical_issues = 0
    try:
        result = subprocess.run(
            [
                "grep",
                "-r",
                "-i",
                "-c",
                "--include=*.py",
                "--include=*.ts",
                "--include=*.js",
                "--exclude-dir=.venv",
                "--exclude-dir=.git",
                "--exclude-dir=logs",
                "--exclude-dir=node_modules",
                "--exclude=goal_tracker.py",  # Don't count ourselves
                "TODO.*P0\\|FIXME.*critical\\|BUG.*critical",
            ],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.stdout.strip():
            for line in result.stdout.strip().split("\n"):
                if ":" in line:
                    count = line.split(":")[-1]
                    if count.isdigit():
                        critical_issues += int(count)
    except Exception:
        pass

    if tests_pass and critical_issues == 0:
        status = GoalStatus.COMPLETE
        status_reason = "Tests pass, no critical issues found"
        current_value = 1.0
    elif tests_pass:
        status = GoalStatus.AT_RISK
        status_reason = f"Tests pass but {critical_issues} critical TODOs found"
        current_value = 0.7
    else:
        status = GoalStatus.AT_RISK
        status_reason = "Tests failing"
        current_value = 0.3

    if inconclusive:
        # A timed-out/errored assessment must not assert either "tests
        # pass" or "tests fail" as fact — say plainly that we don't know.
        status_reason = (
            f"Test run inconclusive (timed out or errored) — {status_reason}"
        )

    # Generate concrete next_actions: name specific tests/TODOs instead of bare goal metric
    next_actions: list[str] = []
    if not tests_pass:
        next_actions.append("Fix failing tests to improve stability")
    elif critical_issues > 0:
        next_actions.append(f"Address {critical_issues} critical TODO(s)")

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=int(current_value * 100),
        status=status,
        status_reason=status_reason,
        blockers=["Fix failing tests"] if not tests_pass else [],
        next_actions=next_actions,
        raw_data={
            "tests_pass": tests_pass,
            "critical_issues": critical_issues,
            "inconclusive": inconclusive,
        },
    )


def assess_enterprise(goal: GoalDefinition, company_dir: Path) -> GoalAssessment:
    """Assess G4: Enterprise audit capabilities."""
    project_root = company_dir.parent

    # Check for audit-export command
    audit_cmd = project_root / ".claude" / "commands" / "audit-export.md"
    sbom_cmd = project_root / ".claude" / "commands" / "sbom.md"

    features = []
    if audit_cmd.exists():
        features.append("audit-export")
    if sbom_cmd.exists():
        features.append("sbom")

    current_value = len(features) / 2  # 2 enterprise features expected
    progress_percent = int(current_value * 100)

    if current_value >= 1.0:
        status = GoalStatus.COMPLETE
        status_reason = f"Enterprise features complete: {', '.join(features)}"
    elif current_value > 0:
        status = GoalStatus.ON_TRACK
        status_reason = f"Partial: {', '.join(features)}"
    else:
        status = GoalStatus.NOT_STARTED
        status_reason = "No enterprise features implemented"

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        raw_data={"features": features},
    )


def assess_autonomy(goal: GoalDefinition, company_dir: Path) -> GoalAssessment:
    """Assess G5: Autonomy goal (24h operation without prompts).

    Reads live daemon heartbeat for real-time status instead of relying
    on cached snapshots. Checks actual task completion and uptime data.
    WS-014: Fixed to return COMPLETE when daemon is actively working.
    """
    project_root = company_dir.parent

    # Check for autonomy infrastructure
    components = {
        "daemon": (
            project_root / ".claude" / "hooks" / "company" / "forge_daemon.py"
        ).exists(),
        "operation_loop": (
            project_root / ".claude" / "hooks" / "company" / "operation_loop.py"
        ).exists(),
        "work_queue": (company_dir / "state/work_queue.json").exists(),
        "circuit_breaker": (
            project_root / ".claude" / "hooks" / "company" / "loop_monitor.py"
        ).exists(),
        "proactive": (
            project_root / ".claude" / "hooks" / "company" / "initiative_engine.py"
        ).exists(),
        "strategic_planner": (
            project_root / ".claude" / "hooks" / "company" / "strategic_planner.py"
        ).exists(),
    }

    # Check daemon status from live heartbeat
    daemon_running = False
    daemon_healthy = False
    tasks_completed = 0
    tasks_failed = 0
    uptime_seconds = 0
    circuit_breaker_state = "unknown"
    heartbeat_file = company_dir / "runtime/daemon.heartbeat"
    if not heartbeat_file.exists():
        heartbeat_file = company_dir / "daemon.heartbeat"
    if heartbeat_file.exists():
        try:
            with open(heartbeat_file) as f:
                hb = json.load(f)
                # Check if heartbeat is recent (within 5 minutes)
                hb_time = datetime.fromisoformat(
                    hb.get("last_heartbeat", "2000-01-01T00:00:00+00:00")
                )
                age = (datetime.now(timezone.utc) - hb_time).total_seconds()
                daemon_running = age < 300

                # Read operational metrics from heartbeat
                tasks_completed = hb.get("tasks_completed_this_session", 0)
                tasks_failed = hb.get("tasks_failed_this_session", 0)
                uptime_seconds = hb.get("uptime_seconds", 0)
                circuit_breaker_state = hb.get("circuit_breaker_state", "unknown")

                # Daemon is healthy if running with closed circuit breaker
                daemon_healthy = daemon_running and circuit_breaker_state == "closed"
        except Exception:
            pass

    # Also check PID file as fallback
    if not daemon_running:
        pid_file = company_dir / "runtime/daemon.pid"
        if pid_file.exists():
            try:
                with open(pid_file) as f:
                    pid_data = json.load(f)
                    pid = pid_data.get("pid")
                    if isinstance(pid, int) and pid > 0:
                        import os

                        os.kill(pid, 0)
                        daemon_running = True
            except (ProcessLookupError, PermissionError, OSError):
                pass
            except Exception:
                pass

    # Calculate progress
    infra_score = sum(components.values()) / len(components)

    # Determine status based on real operational data
    if daemon_healthy and infra_score >= 0.8 and tasks_completed > 0:
        # Daemon running, circuit breaker healthy, tasks completing
        status = GoalStatus.COMPLETE
        success_rate = (
            tasks_completed / (tasks_completed + tasks_failed) * 100
            if (tasks_completed + tasks_failed) > 0
            else 0
        )
        uptime_hours = uptime_seconds / 3600
        status_reason = (
            f"Daemon active: {tasks_completed} tasks completed "
            f"({success_rate:.0f}% success), "
            f"uptime {uptime_hours:.1f}h, circuit breaker {circuit_breaker_state}"
        )
        current_value = 1.0
    elif daemon_running and infra_score >= 0.8:
        # Daemon running but no tasks yet or circuit breaker tripped
        status = GoalStatus.ON_TRACK
        status_reason = (
            f"Daemon running (circuit breaker: {circuit_breaker_state}), "
            f"{tasks_completed} tasks completed"
        )
        current_value = 0.8
    elif infra_score >= 0.8:
        status = GoalStatus.AT_RISK
        status_reason = "Infrastructure ready but daemon not running"
        current_value = 0.6
    elif infra_score >= 0.5:
        status = GoalStatus.AT_RISK
        status_reason = f"Partial infrastructure ({int(infra_score * 100)}%)"
        current_value = infra_score * 0.6
    else:
        status = GoalStatus.NOT_STARTED
        status_reason = "Autonomy infrastructure not built"
        current_value = infra_score * 0.3

    progress_percent = int(current_value * 100)
    missing = [k for k, v in components.items() if not v]
    blockers = [f"Missing: {m}" for m in missing]

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        blockers=blockers,
        next_actions=["Start daemon with /daemon start"] if not daemon_running else [],
        raw_data={
            "components": components,
            "daemon_running": daemon_running,
            "daemon_healthy": daemon_healthy,
            "tasks_completed": tasks_completed,
            "tasks_failed": tasks_failed,
            "uptime_seconds": uptime_seconds,
            "circuit_breaker_state": circuit_breaker_state,
        },
    )


def assess_economics(goal: GoalDefinition, company_dir: Path) -> GoalAssessment:
    """Assess G6: Economics goal (token consumption tracking).

    Simplified scope: economics is complete when token/efficiency tracking
    exists. No longer requires separate budget and cost_tracking features.
    """
    # Check for token/efficiency tracking infrastructure
    has_efficiency_tracker = (
        company_dir.parent / ".claude" / "hooks" / "company" / "efficiency_tracker.py"
    ).exists()
    has_efficiency_data = (company_dir / "state/efficiency_data.json").exists()

    org_file = company_dir / "org.json"
    has_economics_section = False
    if org_file.exists():
        try:
            with open(org_file) as f:
                org = json.load(f)
            has_economics_section = "economics" in org
        except Exception:
            pass

    features = {
        "token_tracking": has_efficiency_tracker,
        "efficiency": has_economics_section or has_efficiency_data,
    }

    current_value = sum(features.values()) / len(features)
    progress_percent = int(current_value * 100)

    if current_value >= 1.0:
        status = GoalStatus.COMPLETE
        status_reason = "Token consumption tracking via efficiency_tracker"
    elif current_value >= 0.5:
        status = GoalStatus.ON_TRACK
        status_reason = f"Economics {progress_percent}% complete"
    else:
        status = GoalStatus.AT_RISK
        status_reason = "Economics needs token tracking setup"

    missing = [k for k, v in features.items() if not v]

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        next_actions=[f"Set up {m}" for m in missing],
        raw_data={"features": features},
    )


# W1-P2 (2026-07-17): G7's success rate is computed over the most recent
# window of operation instead of lifetime-cumulative (which let early
# bring-up debt cap the metric forever). Sessions are prorated by how much
# of their duration overlaps the recency window (counts are per-session, so
# a long session's tasks are attributed uniformly across its runtime), and
# a minimum recent sample is required before the rate can mint COMPLETE or
# an improve-success-rate next-action.
G7_SUCCESS_WINDOW_DAYS = 7.0
G7_MIN_WINDOW_TASKS = 5


def _parse_window_ts(value: Any) -> float | None:
    """ISO-8601 string → epoch seconds; naive timestamps are read as UTC."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _recent_failure_examples(
    company_dir: Path, cutoff_ts: float | None, limit: int = 5
) -> list[dict[str, str]]:
    """Latest attempt per task_id from task_failure_log.jsonl since cutoff_ts.

    G7's "improve success rate" next_action used to be a bare percentage
    with no pointer to which tasks actually failed, forcing every dispatch
    to re-derive the failing task IDs from raw logs before it could tell a
    real bug from an already-fixed or correct-by-design rejection. cutoff_ts
    of None means "no window anchor available" — falls back to unfiltered
    (used only when daemon_metrics has no bounded sessions to anchor on).
    """
    failure_log = company_dir / "state/task_failure_log.jsonl"
    if not failure_log.exists():
        return []
    latest_by_task: dict[str, dict[str, Any]] = {}
    try:
        with open(failure_log) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                task_id = entry.get("task_id")
                if not task_id:
                    continue
                ts = _parse_window_ts(entry.get("timestamp"))
                if cutoff_ts is not None and (ts is None or ts < cutoff_ts):
                    continue
                existing = latest_by_task.get(task_id)
                if existing is None or (ts or 0) >= existing["_ts"]:
                    latest_by_task[task_id] = {
                        "task_id": task_id,
                        "title": str(entry.get("title", ""))[:80],
                        "category": str(entry.get("category", "unknown")),
                        "_ts": ts or 0,
                    }
    except OSError:
        return []
    examples = sorted(latest_by_task.values(), key=lambda e: e["_ts"], reverse=True)
    for e in examples:
        e.pop("_ts", None)
    return examples[:limit]


def _find_uncovered_modules(
    project_root: Path, coverage_percent: float | None = None, limit: int = 3
) -> list[str]:
    """Find Python modules with lowest or zero test coverage for G3.

    Returns concrete module names that could be targeted for test improvements.
    If coverage.json exists, reads it to identify uncovered files; otherwise
    returns empty list so the assessor falls back to generic next_actions.
    """
    if coverage_percent is None or coverage_percent >= 0.5:
        return []

    coverage_file = project_root / "coverage.json"
    if not coverage_file.exists():
        return []

    try:
        with open(coverage_file) as f:
            cov_data = json.load(f)
        files = cov_data.get("files") or {}
        if not isinstance(files, dict):
            return []

        # Extract files with 0% coverage
        uncovered = []
        for file_path, file_data in files.items():
            if not isinstance(file_data, dict):
                continue
            summary = file_data.get("summary") or {}
            pct_covered = summary.get("percent_covered", 100)
            if pct_covered == 0:
                uncovered.append(Path(file_path).stem)

        return uncovered[:limit]
    except Exception:
        return []


def _find_failing_ci_tests(project_root: Path) -> list[str]:
    """Find specific failing test names from recent CI failures for G16.

    Returns concrete test file/function names that failed recently so tasks
    can target specific tests instead of generic "fix CI failures".
    """
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "merged",
                "--limit",
                "3",
                "--json",
                "number,statusCheckRollup",
            ],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []

        prs = json.loads(result.stdout)
        if not isinstance(prs, list):
            return []

        # Look for any failed checks
        failed_tests = set()
        for pr in prs:
            rollup = pr.get("statusCheckRollup", [])
            for check in rollup:
                if check.get("conclusion") != "SUCCESS" and check.get("name"):
                    # Extract test name if available
                    test_name = check.get("name", "")
                    if test_name and "test" in test_name.lower():
                        failed_tests.add(test_name[:80])

        return list(failed_tests)[:3]
    except Exception:
        return []


def assess_sustained_autonomy(
    goal: GoalDefinition, company_dir: Path, *, lightweight: bool = False
) -> GoalAssessment:
    """Assess G7: Sustained Autonomy — 7 days cumulative, >90% success.

    Unlike G5 (Autonomy) which checks current session status, G7 requires
    CUMULATIVE multi-session data: 7 days total uptime across multiple daemon
    restarts, with a >90% task success rate sustained over the most recent
    G7_SUCCESS_WINDOW_DAYS of recorded operation.
    """
    total_uptime_seconds = 0
    total_completed = 0
    total_failed = 0
    # (start_epoch, end_epoch, tasks_completed, tasks_failed) per uptime
    # window — feeds the windowed success rate below. Either epoch may be
    # None when the record carries no parseable timestamp.
    window_records: list[tuple[float | None, float | None, int, int]] = []

    # 1. Read uptime from daemon_metrics.json (queryable heartbeat records).
    # This is the primary uptime source — it captures all sessions including
    # those killed with SIGKILL, using the last_uptime_seconds checkpoint
    # written every 30s by update_heartbeat().
    daemon_metrics_file = company_dir / "state/daemon_metrics.json"
    used_daemon_metrics = False
    if daemon_metrics_file.exists():
        try:
            with open(daemon_metrics_file) as f:
                dm = json.load(f)
            uptime_windows = dm.get("uptime_windows", [])
            for idx, window in enumerate(uptime_windows):
                dur = window.get("duration_seconds")
                if isinstance(dur, (int, float)) and dur > 0:
                    # Closed session with known duration
                    total_uptime_seconds += dur
                elif window.get("ended_at") is None:
                    # Open session (still running or killed) — use last checkpoint
                    checkpoint = window.get("last_uptime_seconds")
                    if isinstance(checkpoint, (int, float)) and checkpoint > 0:
                        total_uptime_seconds += checkpoint
                    else:
                        # No checkpoint yet — compute from started_at
                        try:
                            started = datetime.fromisoformat(
                                window["started_at"].replace("Z", "+00:00")
                            )
                            total_uptime_seconds += (
                                datetime.now(timezone.utc) - started
                            ).total_seconds()
                        except Exception:
                            pass
                # Accumulate task counts from checkpointed windows
                completed = window.get("tasks_completed", 0)
                failed = window.get("tasks_failed", 0)
                total_completed += completed
                total_failed += failed
                start_ts = _parse_window_ts(window.get("started_at"))
                end_ts = _parse_window_ts(window.get("ended_at"))
                if end_ts is None:
                    span = window.get("duration_seconds")
                    if not (isinstance(span, (int, float)) and span > 0):
                        span = window.get("last_uptime_seconds")
                    if (
                        start_ts is not None
                        and isinstance(span, (int, float))
                        and span > 0
                    ):
                        end_ts = start_ts + span
                    elif start_ts is not None:
                        # Open window with no checkpoint: only the NEWEST
                        # record can be the live session — older open
                        # records are crashed-session zombies (production
                        # carries dozens) and stamping them "now" would
                        # drag the recency anchor to wall clock forever.
                        if idx == len(uptime_windows) - 1:
                            end_ts = datetime.now(timezone.utc).timestamp()
                        else:
                            end_ts = start_ts
                if start_ts is None and end_ts is not None:
                    start_ts = end_ts
                window_records.append((start_ts, end_ts, completed, failed))
            used_daemon_metrics = True
        except Exception:
            pass

    # 2. Read current session data from daemon heartbeat.
    # Used for has_heartbeat detection and as uptime fallback when no daemon_metrics.
    # Check both locations: new runtime/ path and legacy root path
    heartbeat_file = company_dir / "runtime/daemon.heartbeat"
    if not heartbeat_file.exists():
        heartbeat_file = company_dir / "daemon.heartbeat"
    if heartbeat_file.exists():
        try:
            with open(heartbeat_file) as f:
                hb = json.load(f)
            if not used_daemon_metrics:
                # Fallback: use heartbeat for uptime when daemon_metrics unavailable
                total_uptime_seconds += hb.get("uptime_seconds", 0)
                total_completed += hb.get("tasks_completed_this_session", 0)
                total_failed += hb.get("tasks_failed_this_session", 0)
        except Exception:
            pass

    # 3. Read historical session data from session_state.json for task counts.
    # Only used for uptime when both daemon_metrics and heartbeat are unavailable.
    session_state_file = company_dir / "state/session_state.json"
    if session_state_file.exists():
        try:
            with open(session_state_file) as f:
                session_data = json.load(f)
            if not used_daemon_metrics:
                total_uptime_seconds += session_data.get("total_uptime_seconds", 0)
            # Task counts from session_state are used when daemon_metrics has no tasks
            if total_completed == 0 and total_failed == 0:
                loop_metrics = session_data.get("loop_metrics", {})
                total_completed += loop_metrics.get("tasks_completed", 0)
                total_failed += loop_metrics.get("tasks_failed", 0)
                for session in session_data.get("sessions", []):
                    total_completed += session.get("tasks_completed", 0)
                    total_failed += session.get("tasks_failed", 0)
        except Exception:
            pass

    # 3. Read historical goal snapshots from strategic_state.json
    strategic_state_file = company_dir / "state/strategic_state.json"
    if strategic_state_file.exists():
        try:
            with open(strategic_state_file) as f:
                strategic_data = json.load(f)
            for snapshot in strategic_data.get("goal_snapshots", []):
                snap_goal_id = snapshot.get("goal_id", "")
                if snap_goal_id in ("G5", "G7"):
                    raw = snapshot.get("raw_data", {})
                    snap_uptime = raw.get("uptime_seconds", 0)
                    if snap_uptime > 0:
                        total_uptime_seconds += snap_uptime
        except Exception:
            pass

    # 4. Calculate cumulative uptime in days
    uptime_days = total_uptime_seconds / 86400.0

    # 5. Calculate success rate — windowed over the most recent
    # G7_SUCCESS_WINDOW_DAYS of recorded operation (anchored to the newest
    # window, not wall clock, so an idle daemon still reports its last week).
    # Lifetime accumulation let one rocky bring-up week permanently cap the
    # metric near 64% (626 clean completions needed to recover), driving an
    # endless "improve success rate" autofill treadmill — see
    # TestAssessSustainedAutonomy. Uptime stays cumulative: the 7-day uptime
    # target has no equivalent permanent-debt problem.
    total_tasks = total_completed + total_failed
    lifetime_success_rate = total_completed / total_tasks if total_tasks > 0 else 0.0

    # Each session is prorated by how much of its runtime overlaps the
    # recency window ending at the newest session END (not wall clock, so an
    # idle daemon still reports its last active week; not session START, so
    # a >7-day session's recent failures cannot vanish after a restart).
    windowed_completed = 0.0
    windowed_failed = 0.0
    bounded = [r for r in window_records if r[1] is not None]
    window_cutoff: float | None = None
    if bounded:
        anchor = max(r[1] for r in bounded)
        cutoff = anchor - G7_SUCCESS_WINDOW_DAYS * 86400.0
        window_cutoff = cutoff
        for start_ts, end_ts, completed, failed in bounded:
            if start_ts is None or start_ts >= end_ts:
                fraction = 1.0 if end_ts >= cutoff else 0.0
            else:
                overlap = min(end_ts, anchor) - max(start_ts, cutoff)
                fraction = max(0.0, min(1.0, overlap / (end_ts - start_ts)))
            windowed_completed += completed * fraction
            windowed_failed += failed * fraction
    # Windows with no parseable timestamps at all stay OUT of the windowed
    # rate (a corrupt record must never become permanent, un-ageable debt);
    # their counts still appear in the lifetime figures.

    windowed_tasks = windowed_completed + windowed_failed
    if used_daemon_metrics and windowed_tasks > 0:
        success_rate = windowed_completed / windowed_tasks
        sample_size = windowed_tasks
    elif used_daemon_metrics:
        # No recent task sample — surface the lifetime rate for context,
        # but the empty sample must not mint COMPLETE or an improve-rate
        # next-action (an idle-gap restart would otherwise resurrect the
        # autofill treadmill from historical debt).
        success_rate = lifetime_success_rate
        sample_size = 0.0
    else:
        # Fallback sources (heartbeat/session_state) carry no per-window
        # timestamps — keep the lifetime calculation there.
        success_rate = lifetime_success_rate
        sample_size = float(total_tasks)
    sample_ok = sample_size >= G7_MIN_WINDOW_TASKS
    windowed_rate = windowed_completed / windowed_tasks if windowed_tasks > 0 else None
    if not sample_ok and total_tasks > 0:
        # Too few recent tasks to speak for the trend — every displayed
        # surface (status ladder, progress, reason) uses the better-
        # supported lifetime rate; the thin windowed rate stays visible in
        # raw_data only. Otherwise one post-restart task would show 100%
        # (or 0%) progress to every dashboard consumer.
        success_rate = lifetime_success_rate

    # 6. Calculate progress (60% uptime weight, 40% success rate weight)
    uptime_progress = min(uptime_days / 7.0, 1.0)
    success_progress = min(success_rate / 0.9, 1.0)  # target is 90%
    progress = uptime_progress * 0.6 + success_progress * 0.4
    progress_percent = int(progress * 100)

    # 7. Determine status — daemon data required (heartbeat or daemon_metrics must exist)
    has_heartbeat = heartbeat_file.exists() or daemon_metrics_file.exists()
    if not has_heartbeat:
        status = GoalStatus.NOT_STARTED
        status_reason = "No daemon heartbeat data found"
    elif uptime_days >= 7 and success_rate >= 0.9 and sample_ok:
        status = GoalStatus.COMPLETE
        status_reason = (
            f"Sustained autonomy achieved: {uptime_days:.1f} days uptime, "
            f"{success_rate * 100:.0f}% recent success rate"
        )
    elif uptime_days >= 3 and success_rate >= 0.8:
        status = GoalStatus.ON_TRACK
        status_reason = (
            f"Progressing: {uptime_days:.1f}/7 days, "
            f"{success_rate * 100:.0f}% success ({total_completed} tasks)"
        )
    elif uptime_days >= 1 or success_rate >= 0.5:
        status = GoalStatus.AT_RISK
        status_reason = (
            f"Early stage: {uptime_days:.1f} days uptime, "
            f"{success_rate * 100:.0f}% success rate"
        )
    else:
        status = GoalStatus.AT_RISK
        status_reason = (
            f"Minimal data: {uptime_days:.2f} days uptime, {total_tasks} tasks recorded"
        )

    next_actions = []
    if not has_heartbeat:
        next_actions.append("Start daemon with /daemon start")
    elif uptime_days < 7:
        next_actions.append(
            f"Continue daemon operation ({7 - uptime_days:.1f} days remaining)"
        )
    failure_examples: list[dict[str, str]] = []
    if total_tasks > 0 and success_rate < 0.9 and sample_ok:
        failure_examples = _recent_failure_examples(company_dir, window_cutoff)
        next_actions.append(
            f"Improve success rate from {success_rate * 100:.0f}% to 90%+"
        )
        if failure_examples:
            # Separate entry (not appended to the action above) so the
            # QUEUE-FILL task title — built from next_actions[0], truncated
            # to 60 chars by strategic_planner.py — stays clean; this
            # renders as a numbered "Additional Actions" line in the task
            # description instead, where a worker can actually read it.
            pointer = "; ".join(
                f"{e['task_id']} ({e['category']})" for e in failure_examples
            )
            next_actions.append(f"Recent window failures: {pointer}")

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=progress,
        target_value=1.0,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        next_actions=next_actions,
        raw_data={
            "uptime_days": uptime_days,
            "uptime_seconds": total_uptime_seconds,
            "tasks_completed": total_completed,
            "tasks_failed": total_failed,
            "success_rate": success_rate,
            "success_rate_lifetime": lifetime_success_rate,
            "success_rate_windowed": windowed_rate,
            "success_window_days": G7_SUCCESS_WINDOW_DAYS,
            "windowed_tasks_completed": round(windowed_completed, 2),
            "windowed_tasks_failed": round(windowed_failed, 2),
            "success_sample_size": round(sample_size, 2),
            "success_sample_ok": sample_ok,
            "uptime_progress": uptime_progress,
            "success_progress": success_progress,
            "recent_failure_examples": failure_examples,
        },
    )


# -----------------------------------------------------------------------------
# Assessor Registry
# -----------------------------------------------------------------------------


def assess_self_improvement(
    goal: GoalDefinition, company_dir: Path, *, lightweight: bool = False
) -> GoalAssessment:
    """Assess G8: Self-Improvement — 10+ approved improvement proposals."""
    total_approved = 0

    # Count approved initiatives from strategic_state.json
    strategic_file = company_dir / "state/strategic_state.json"
    if strategic_file.exists():
        try:
            with open(strategic_file) as f:
                data = json.load(f)
            for initiative in data.get("active_initiatives", []):
                if initiative.get("approved_at") is not None:
                    total_approved += 1
            for initiative in data.get("completed_initiatives", []):
                if initiative.get("approved_at") is not None:
                    total_approved += 1
        except Exception:
            pass

    # Also check improvement_cycles.json if it exists
    improvement_file = company_dir / "improvement_cycles.json"
    if improvement_file.exists():
        try:
            with open(improvement_file) as f:
                imp_data = json.load(f)
            # Count approved proposals from improvement cycles
            for cycle in imp_data if isinstance(imp_data, list) else [imp_data]:
                for proposal in cycle.get("proposals", []):
                    if proposal.get("approved_at") is not None:
                        total_approved += 1
        except Exception:
            pass

    target = 10
    current_value = min(total_approved / target, 1.0)
    progress_percent = int(current_value * 100)

    if total_approved >= target:
        status = GoalStatus.COMPLETE
        status_reason = (
            f"{total_approved} approved improvement proposals (target: {target})"
        )
    elif total_approved >= 5:
        status = GoalStatus.ON_TRACK
        status_reason = f"{total_approved}/{target} approved proposals, on track"
    elif total_approved >= 1:
        status = GoalStatus.AT_RISK
        status_reason = f"Only {total_approved}/{target} approved proposals"
    else:
        status = GoalStatus.NOT_STARTED
        status_reason = "No approved improvement proposals yet"

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        next_actions=["Run /improve to generate proposals"]
        if status != GoalStatus.COMPLETE
        else [],
        raw_data={"total_approved": total_approved, "target": target},
    )


def assess_employee_initiative(
    goal: GoalDefinition, company_dir: Path, *, lightweight: bool = False
) -> GoalAssessment:
    """Assess G10: Employee Initiative — 50% tasks from employees."""
    idea_count = 0
    initiative_task_count = 0
    total_completed = 0

    # Count non-rejected ideas from employee_ideas.json
    ideas_file = company_dir / "employee_ideas.json"
    if ideas_file.exists():
        try:
            with open(ideas_file) as f:
                ideas_data = json.load(f)
            ideas_list = (
                ideas_data
                if isinstance(ideas_data, list)
                else ideas_data.get("ideas", [])
            )
            for idea in ideas_list:
                if idea.get("status") != "rejected":
                    idea_count += 1
        except Exception:
            pass

    # Build set of valid employee IDs for proposed_by validation
    valid_employee_ids: set[str] = set()
    org_file = company_dir / "org.json"
    if org_file.exists():
        try:
            with open(org_file) as f:
                org_data = json.load(f)
            for emp in org_data.get("employees", []):
                eid = emp.get("id", "")
                if eid:
                    valid_employee_ids.add(eid)
        except Exception:
            pass

    # Count completed tasks from work_queue.json
    created_by_counts: dict[str, int] = {}
    queue_file = company_dir / "state/work_queue.json"
    if queue_file.exists():
        try:
            with open(queue_file) as f:
                queue_data = json.load(f)
            completed = queue_data.get("completed", [])
            total_completed = len(completed)

            # Count initiative tasks: source="employee_initiative" (Layer 3: batch ideation)
            # OR proposed_by a valid employee (Layers 1-2: prompt injection / post-completion)
            for task in completed:
                is_initiative = task.get("source") == "employee_initiative"
                proposed_by = task.get("proposed_by", "")
                is_employee_proposal = proposed_by in valid_employee_ids
                if is_initiative or is_employee_proposal:
                    initiative_task_count += 1
                    creator = task.get("created_by") or proposed_by
                    if creator:
                        created_by_counts[creator] = (
                            created_by_counts.get(creator, 0) + 1
                        )
        except Exception:
            pass

    # Use initiative task count (from completed employee_initiative tasks) as numerator
    # This represents tasks completed from the idea_scanner pipeline
    ratio = initiative_task_count / max(total_completed, 1)
    target_ratio = 0.5
    current_value = min(ratio / target_ratio, 1.0)
    progress_percent = int(current_value * 100)

    if ratio >= target_ratio:
        status = GoalStatus.COMPLETE
        status_reason = (
            f"{initiative_task_count} completed initiative tasks vs {total_completed} total completed "
            f"({ratio:.0%} ratio, target: {target_ratio:.0%})"
        )
    elif ratio >= 0.25:
        status = GoalStatus.ON_TRACK
        status_reason = (
            f"{ratio:.0%} initiative ratio, approaching {target_ratio:.0%} target"
        )
    elif initiative_task_count > 0:
        status = GoalStatus.AT_RISK
        status_reason = (
            f"Only {initiative_task_count} initiative tasks ({ratio:.0%} ratio)"
        )
    else:
        status = GoalStatus.NOT_STARTED
        status_reason = "No employee initiative tasks completed"

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        next_actions=["Encourage employee initiative task completion"]
        if status != GoalStatus.COMPLETE
        else [],
        raw_data={
            "idea_count": idea_count,
            "initiative_task_count": initiative_task_count,
            "total_completed": total_completed,
            "ratio": ratio,
            "target_ratio": target_ratio,
            "created_by": created_by_counts,
        },
    )


def assess_queue_health(
    goal: GoalDefinition, company_dir: Path, *, lightweight: bool = False
) -> GoalAssessment:
    """Assess G11: Queue Health — queue never empty > 1 hour."""
    has_pending = False
    has_recent_activity = False
    has_throughput = False

    # Check pending tasks in work_queue.json
    queue_file = company_dir / "state/work_queue.json"
    in_progress = []
    pending = []
    completed = []
    # Read in_progress tasks (read-only — no heal_queue side effects during assessment)
    if queue_file.exists():
        try:
            with open(queue_file) as f:
                queue_data = json.load(f)
            pending = queue_data.get("pending", [])
            in_progress = queue_data.get("in_progress", [])
            completed = queue_data.get("completed", [])
            has_pending = len(pending) > 0 or len(in_progress) > 0
        except Exception:
            pass

    # Check daemon heartbeat for recent activity, throughput, and queue metrics.
    # Check runtime/ first (default config path), fall back to company_dir root.
    hb: dict = {}
    for hb_path in (
        company_dir / "runtime/daemon.heartbeat",
        company_dir / "daemon.heartbeat",
    ):
        if hb_path.exists():
            try:
                with open(hb_path) as f:
                    hb = json.load(f)
                break
            except Exception:
                pass

    uptime = 0
    throughput_grace = False
    if hb:
        hb_time = datetime.fromisoformat(
            hb.get("last_heartbeat", "2000-01-01T00:00:00+00:00")
        )
        age = (datetime.now(timezone.utc) - hb_time).total_seconds()
        has_recent_activity = age < 3600  # within 1 hour
        uptime = hb.get("uptime_seconds", 0)
        tasks_completed = hb.get("tasks_completed_this_session", 0)
        throughput_grace = tasks_completed == 0 and uptime < 600
        # G11 fix: also pass throughput if tasks are actively in_progress
        has_throughput = tasks_completed > 0 or uptime < 600

    # Fallback: if heartbeat shows no throughput but queue has recent in_progress work
    if not has_throughput and len(in_progress) > 0:
        # Only count as healthy if at least one task was claimed recently (within 2h)
        for ip_task in in_progress:
            claimed_at = ip_task.get("claimed_at") or ip_task.get("started_at", "")
            if claimed_at:
                try:
                    claimed_dt = datetime.fromisoformat(
                        claimed_at.replace("Z", "+00:00")
                    )
                    if (datetime.now(timezone.utc) - claimed_dt).total_seconds() < 7200:
                        has_throughput = True
                        break
                except (ValueError, TypeError):
                    pass

    # Fallback: check queue completed list for recent completions
    if not has_throughput and completed:
        for c_task in completed[-20:]:  # Check last 20 completed
            completed_at = c_task.get("completed_at") or c_task.get("updated_at", "")
            if completed_at:
                try:
                    c_dt = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
                    if (datetime.now(timezone.utc) - c_dt).total_seconds() < 7200:
                        has_throughput = True
                        break
                except (ValueError, TypeError):
                    pass

    # Enrich queue metrics from heartbeat when available (G11 instrumentation).
    queue_depth: int = hb.get("queue_depth", -1)
    queue_blocked: int = hb.get("queue_blocked", 0)
    blocked_ratio: float = hb.get("queue_blocked_ratio", 0.0)
    throughput_per_hour: float = hb.get("throughput_per_hour", 0.0)

    # Weighted scoring: pending=0.4, recent_activity=0.3, throughput=0.3
    current_value = 0.0
    if has_pending:
        current_value += 0.4
    if has_recent_activity:
        current_value += 0.3
    if has_throughput:
        current_value += 0.3

    criteria_met = sum([has_pending, has_recent_activity, has_throughput])
    progress_percent = int(current_value * 100)

    if criteria_met == 3:
        status = GoalStatus.COMPLETE
        status_reason = "Queue healthy: pending tasks, recent activity, and throughput"
    elif criteria_met == 2:
        status = GoalStatus.ON_TRACK
        missing = []
        if not has_pending:
            missing.append("no pending tasks")
        if not has_recent_activity:
            missing.append("no recent activity")
        if not has_throughput:
            missing.append("no throughput")
        status_reason = (
            f"Queue partially healthy ({criteria_met}/3): missing {', '.join(missing)}"
        )
    elif criteria_met == 1:
        status = GoalStatus.AT_RISK
        status_reason = f"Queue unhealthy: only {criteria_met}/3 health criteria met"
    else:
        status = GoalStatus.NOT_STARTED
        status_reason = "No queue health signals detected"

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        next_actions=["Start daemon and populate work queue"]
        if status != GoalStatus.COMPLETE
        else [],
        raw_data={
            "has_pending": has_pending,
            "has_recent_activity": has_recent_activity,
            "has_throughput": has_throughput,
            "criteria_met": criteria_met,
            "pending_count": len(pending),
            "in_progress_count": len(in_progress),
            "uptime_seconds": uptime,
            "throughput_grace": throughput_grace,
            "queue_depth": queue_depth,
            "queue_blocked": queue_blocked,
            "blocked_ratio": blocked_ratio,
            "throughput_per_hour": throughput_per_hour,
        },
    )


# -----------------------------------------------------------------------------
# G12–G16 Assessors (Sprint 2)
# -----------------------------------------------------------------------------


def assess_parallel_throughput(
    goal: GoalDefinition, company_dir: Path, *, lightweight: bool = False
) -> GoalAssessment:
    """Assess G12: Parallel Throughput — 2+ workers active simultaneously for 24h."""
    # WS-057-004: Check daemon worktree dir, not manual .worktrees/
    # Per-project base so the metric counts THIS project's workers only.
    try:
        from company_resolver import get_worktree_base

        daemon_wt_dir = get_worktree_base()
    except Exception:
        daemon_wt_dir = Path("/tmp/forge-worktrees")
    worktree_count = 0
    if (
        daemon_wt_dir.exists()
        and not daemon_wt_dir.is_symlink()
        and daemon_wt_dir.is_dir()
    ):
        worktree_count = len([d for d in daemon_wt_dir.iterdir() if d.is_dir()])
    if worktree_count == 0:
        manual_wt_dir = company_dir.parent / ".worktrees"
        if manual_wt_dir.exists():
            worktree_count = len([d for d in manual_wt_dir.iterdir() if d.is_dir()])

    # Read heartbeat for current worker status
    max_concurrent = 0
    hb: dict = {}
    for hb_path in (
        company_dir / "runtime/daemon.heartbeat",
        company_dir / "daemon.heartbeat",
    ):
        if hb_path.exists():
            try:
                with open(hb_path) as f:
                    hb = json.load(f)
                break
            except Exception:
                pass

    if hb:
        # WS-057-004: Prefer active_workers (actual threads) over queue_in_progress
        max_concurrent = max(
            1, hb.get("active_workers", hb.get("queue_in_progress", 0))
        )

    # Check session state for sustained uptime
    sustained_hours = 0.0
    session_file = company_dir / "state/session_state.json"
    if session_file.exists():
        try:
            with open(session_file) as f:
                sd = json.load(f)
            sustained_hours = sd.get("total_uptime_seconds", 0) / 3600.0
        except Exception:
            pass

    # Scoring: infra 30%, parallel 30%, duration 40%
    infra_score = 1.0 if worktree_count >= 1 else 0.0
    parallel_score = min(max_concurrent / 2.0, 1.0)
    duration_score = min(sustained_hours / 24.0, 1.0)
    current_value = infra_score * 0.3 + parallel_score * 0.3 + duration_score * 0.4
    progress_percent = int(current_value * 100)

    if max_concurrent >= 2 and sustained_hours >= 24.0 and worktree_count >= 1:
        status = GoalStatus.COMPLETE
        status_reason = (
            f"Parallel throughput achieved: {max_concurrent} concurrent workers, "
            f"{sustained_hours:.1f}h sustained"
        )
    elif max_concurrent >= 2 and worktree_count >= 1:
        status = GoalStatus.ON_TRACK
        status_reason = (
            f"{max_concurrent} workers active, need "
            f"{max(0, 24 - sustained_hours):.1f}h more for 24h target"
        )
    elif worktree_count >= 1:
        status = GoalStatus.AT_RISK
        status_reason = (
            "Worktrees available but parallel execution not yet demonstrated"
        )
    else:
        status = GoalStatus.NOT_STARTED
        status_reason = "Worktree isolation not yet set up"

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        blockers=[] if worktree_count >= 1 else ["Worktree isolation required"],
        next_actions=(
            ["Enable parallel workers via daemon --workers 2"]
            if max_concurrent < 2
            else []
        ),
        raw_data={
            "worktree_count": worktree_count,
            "max_concurrent_workers": max_concurrent,
            "sustained_uptime_hours": sustained_hours,
        },
    )


def assess_first_revenue(goal: GoalDefinition, company_dir: Path) -> GoalAssessment:
    """Assess G13: First Revenue — $1 revenue from any source."""
    project_root = company_dir.parent

    # Check for issued licenses
    licenses_issued: list[dict] = []
    for lic_name in ("forge-license.json", "forge-license-trial.json"):
        lic_file = project_root / lic_name
        if lic_file.exists():
            try:
                with open(lic_file) as f:
                    lic = json.load(f)
                licenses_issued.append(
                    {
                        "org": lic.get("org", "Unknown"),
                        "type": "trial" if "trial" in lic_name else "full",
                    }
                )
            except Exception:
                pass

    # Check economics in org.json for revenue
    total_revenue = 0.0
    org_file = company_dir / "org.json"
    if org_file.exists():
        try:
            with open(org_file) as f:
                org = json.load(f)
            total_revenue = float(org.get("economics", {}).get("total_revenue", 0.0))
        except Exception:
            pass

    # Check sales directory
    sales_dir = company_dir / "sales"
    has_sales_activity = (
        sales_dir.exists() and any(sales_dir.iterdir()) if sales_dir.exists() else False
    )

    # Scoring: revenue 70%, pipeline 30%
    revenue_score = 1.0 if total_revenue >= 1.0 else 0.0
    pipeline_score = (
        min(len(licenses_issued) / 1.0, 1.0)
        if licenses_issued
        else (0.3 if has_sales_activity else 0.0)
    )
    current_value = revenue_score * 0.7 + pipeline_score * 0.3
    progress_percent = int(current_value * 100)

    if total_revenue >= 1.0:
        status = GoalStatus.COMPLETE
        status_reason = f"First revenue achieved: ${total_revenue:.2f}"
    elif licenses_issued:
        status = GoalStatus.ON_TRACK
        status_reason = f"{len(licenses_issued)} license(s) issued, awaiting revenue"
    elif has_sales_activity:
        status = GoalStatus.AT_RISK
        status_reason = "Sales activity detected but no licenses issued"
    else:
        status = GoalStatus.NOT_STARTED
        status_reason = "No sales pipeline activity or revenue recorded"

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        blockers=[] if licenses_issued else ["No customer engagement yet"],
        # [OWNER-ONLY]: closing a sale is a human business-development action,
        # not something an LLM worker session can execute. Prefixed so
        # strategic_planner.autofill_queue_from_goals skips this goal instead
        # of minting an unbuildable [QUEUE-FILL] task (2026-07-20: ~20
        # admission-gate rejections at ~7min cadence before this fix).
        next_actions=(
            ["[OWNER-ONLY] Close first consulting engagement or license sale"]
            if status != GoalStatus.COMPLETE
            else []
        ),
        raw_data={
            "total_revenue": total_revenue,
            "licenses_issued": len(licenses_issued),
            "has_sales_activity": has_sales_activity,
        },
    )


def assess_product_packaging(goal: GoalDefinition, company_dir: Path) -> GoalAssessment:
    """Assess G14: Product Packaging — Working /compliance-pack output."""
    project_root = company_dir.parent

    # Check command exists
    has_command = (
        project_root / ".claude" / "commands" / "compliance-pack.md"
    ).exists()

    # Check product page exists
    has_product_page = (
        project_root / "forge-website" / "compliance-pack.html"
    ).exists()

    # Check license features
    license_features: list[str] = []
    lic_file = project_root / "forge-license.json"
    if lic_file.exists():
        try:
            with open(lic_file) as f:
                lic = json.load(f)
            for feat in lic.get("features", []):
                if feat in (
                    "audit-export",
                    "sbom",
                    "extended-secret-scanning",
                    "soc2-mapping",
                    "compliance-pack",
                ):
                    license_features.append(feat)
        except Exception:
            pass

    # Check security docs
    has_security_docs = (project_root / "forge-website" / "security.html").exists()

    # Scoring: command+page 40%, features 40%, docs 20%
    cp_score = (
        (1.0 if has_command else 0.0) + (1.0 if has_product_page else 0.0)
    ) / 2.0
    feat_score = min(len(license_features) / 3.0, 1.0)
    docs_score = 1.0 if has_security_docs else 0.0
    current_value = cp_score * 0.4 + feat_score * 0.4 + docs_score * 0.2
    progress_percent = int(current_value * 100)

    if has_command and has_product_page and len(license_features) >= 3:
        status = GoalStatus.COMPLETE
        status_reason = (
            f"Compliance Pack ready: command, page, {len(license_features)} features"
        )
    elif has_command and len(license_features) >= 1:
        status = GoalStatus.ON_TRACK
        status_reason = f"Command exists, {len(license_features)} feature(s) in license"
    elif has_command or has_product_page:
        status = GoalStatus.AT_RISK
        status_reason = "Partial packaging — missing command or product page"
    else:
        status = GoalStatus.NOT_STARTED
        status_reason = "Compliance Pack not yet initiated"

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        blockers=[],
        next_actions=(
            ["Ship compliance features and product page"]
            if status != GoalStatus.COMPLETE
            else []
        ),
        raw_data={
            "has_command": has_command,
            "has_product_page": has_product_page,
            "has_security_docs": has_security_docs,
            "license_features": license_features,
        },
    )


def _website_html_pages(project_root: Path) -> list[Path]:
    """Locate website HTML pages. forge-website is a sibling repo, not nested
    under project_root, and its pages live under site/ — not the repo root.

    FORGE_WEBSITE_ROOT overrides the website repo root (used by tests to
    avoid reaching outside project_root into whatever happens to sit next
    to a tmpdir).
    """
    override = os.environ.get("FORGE_WEBSITE_ROOT")
    website_root = Path(override) if override else project_root.parent / "forge-website"
    site_dir = website_root / "site"
    if not site_dir.is_dir():
        return []
    return sorted(site_dir.glob("*.html"))


def _website_has_trial_cta(pages: list[Path]) -> bool:
    """Detect an actual self-serve trial/checkout CTA.

    Plain keyword co-occurrence (e.g. "trial" + "form" anywhere on the page)
    false-positives on marketing copy that explicitly disclaims a trial (e.g.
    an FAQ answering "Is there a free trial?" with "There is nothing to
    trial"). Require either a real checkout/trial link/button, and treat an
    explicit no-trial disclaimer as authoritative.
    """
    disclaimer_markers = ("nothing to trial", "no trials", "no self-serve checkout")
    cta_markers = (
        "start trial",
        "start free trial",
        "start your trial",
        "buy.stripe.com",
        "checkout.stripe.com",
    )
    contents = []
    for page in pages:
        try:
            contents.append(page.read_text().lower())
        except OSError:
            continue
    if any(marker in content for content in contents for marker in disclaimer_markers):
        return False
    return any(marker in content for content in contents for marker in cta_markers)


def _website_has_inquiry_cta(pages: list[Path]) -> bool:
    """Detect a real inquiry/engagement CTA on the site.

    Option B (owner decision 2026-07-20): G15 measures the inquiry/signup
    path, not a self-serve trial. The truth-pass positioning is "every
    engagement starts with an email" — there is no self-serve checkout by
    design — so the signup path is a contact/booking CTA: a mailto: inquiry
    link, or a "book a demo" / "contact sales" / "talk to us" button. Its
    presence means a visitor can actually start an engagement; its absence
    means the path is dead.
    """
    cta_markers = (
        "mailto:sales@",
        "mailto:hello@",
        "book a demo",
        "book a call",
        "contact sales",
        "talk to",
        "get in touch",
    )
    for page in pages:
        try:
            content = page.read_text().lower()
        except OSError:
            continue
        if any(marker in content for marker in cta_markers):
            return True
    return False


def assess_website_conversion(
    goal: GoalDefinition, company_dir: Path
) -> GoalAssessment:
    """Assess G15: Website Conversion — inquiry/signup path works end to end.

    Option B (owner decision 2026-07-20): the self-serve "/license trial from
    website" metric was retired. The site's checkout/trial UI was deliberately
    removed in the truth-pass (commit 296a4c5) because the positioning is
    free-OSS core + human services ("every engagement starts with an email").
    G15 now measures the real KR (O2-KR1): the inquiry/signup path — a
    contact/booking CTA plus /license for post-sale activation — working end to
    end, with honest positioning (no undeliverable self-serve-trial promise).
    See docs/g15-license-trial-design-brief-20260717.md.
    """
    project_root = company_dir.parent

    # /license command exists (post-inquiry activation path).
    has_license_cmd = (project_root / ".claude" / "commands" / "license.md").exists()

    website_pages = _website_html_pages(project_root)
    # A real contact/booking CTA = a visitor can actually start an engagement.
    has_inquiry_cta = _website_has_inquiry_cta(website_pages)
    # Product is explained.
    has_platform = any(page.name == "platform.html" for page in website_pages)
    # Honest positioning: no self-serve trial/checkout promise the backend
    # can't fulfil — that dishonest promise is exactly what the truth-pass
    # removed. Absence of a self-serve-trial CTA (or an explicit no-trial
    # disclaimer) is honest.
    honest_positioning = not _website_has_trial_cta(website_pages)

    # Scoring: inquiry CTA 40%, /license 30%, platform 15%, honest 15%.
    current_value = (
        (0.40 if has_inquiry_cta else 0.0)
        + (0.30 if has_license_cmd else 0.0)
        + (0.15 if has_platform else 0.0)
        + (0.15 if honest_positioning else 0.0)
    )
    progress_percent = int(current_value * 100)

    core_path_live = has_inquiry_cta and has_license_cmd and honest_positioning
    if core_path_live and has_platform:
        status = GoalStatus.COMPLETE
        status_reason = (
            "Inquiry path live end to end: contact/booking CTA + /license "
            "activation, honest free-OSS + services positioning"
        )
    elif core_path_live:
        status = GoalStatus.ON_TRACK
        status_reason = "Inquiry CTA and /license activation live; product page pending"
    elif has_license_cmd:
        status = GoalStatus.AT_RISK
        status_reason = (
            "/license activation ready but the site has no contact/booking CTA "
            "for visitors to start an engagement"
        )
    else:
        status = GoalStatus.NOT_STARTED
        status_reason = "Inquiry/signup path not yet built"

    if status == GoalStatus.COMPLETE:
        next_actions = []
    elif not has_inquiry_cta:
        next_actions = [
            "Add a contact/booking CTA (mailto inquiry or 'book a demo') to the "
            "site so visitors can start an engagement, connected to /license "
            "for post-sale activation"
        ]
    elif not honest_positioning:
        next_actions = [
            "Remove the self-serve trial/checkout promise the backend can't "
            "fulfil (or finish the delivery/security fixes in "
            "PURCHASE_FLOW_AUDIT.md before making it)"
        ]
    else:
        next_actions = ["Publish the product/platform page"]

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        blockers=[],
        next_actions=next_actions,
        raw_data={
            "has_inquiry_cta": has_inquiry_cta,
            "has_license_cmd": has_license_cmd,
            "has_platform_page": has_platform,
            "honest_positioning": honest_positioning,
        },
    )


def assess_ci_green(goal: GoalDefinition, company_dir: Path) -> GoalAssessment:
    """Assess G16: CI Green — 0 failures on 3 consecutive PRs."""
    project_root = company_dir.parent

    # Query last 3 merged PRs via gh
    pr_success_count = 0
    pr_total = 0
    query_ok = False
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--state",
                "merged",
                "--limit",
                "3",
                "--json",
                "number,statusCheckRollup",
            ],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            prs = json.loads(result.stdout)
            if not isinstance(prs, list):
                prs = []
            pr_total = len(prs)
            for pr in prs:
                rollup = pr.get("statusCheckRollup", [])
                if rollup and all(c.get("conclusion") == "SUCCESS" for c in rollup):
                    pr_success_count += 1
            query_ok = True
    except Exception:
        pass

    # Check CI config exists
    has_ci = (project_root / ".github" / "workflows" / "ci.yml").exists()

    if not query_ok:
        # A `gh` failure (auth, network, rate limit, non-repo cwd in a worker
        # worktree) must never be scored the same as confirmed CI failures --
        # doing so previously reported "CI configured but recent PRs have
        # failures" from zero evidence, which minted an endless stream of
        # bogus [QUEUE-FILL] G16 tasks ("fix flaky tests") that no worker
        # session could act on, since there was nothing real to fix. Live
        # gh query confirmed the last 3 merged PRs were all fully green while
        # this was silently reporting 30%/at_risk. See goal_tracker.py's G1
        # [INFRA-ONLY] precedent for the same class of fix.
        return GoalAssessment(
            goal_id=goal.id,
            goal_name=goal.name,
            description=goal.description,
            success_metric=goal.success_metric,
            owner=goal.owner,
            current_value=0.3 if has_ci else 0.0,
            target_value=1.0,
            progress_percent=30 if has_ci else 0,
            status=GoalStatus.AT_RISK if has_ci else GoalStatus.NOT_STARTED,
            status_reason=(
                "Unable to query recent PR CI status via gh -- cannot verify "
                "green or red; not a confirmed failure"
                if has_ci
                else "CI not yet configured"
            ),
            blockers=[],
            next_actions=(
                [
                    "[INFRA-ONLY] gh query for recent PR CI status failed "
                    "(auth/network/rate-limit) -- no worker action possible; "
                    "re-assess once gh can reach GitHub"
                ]
                if has_ci
                else []
            ),
            raw_data={
                "pr_success_count": 0,
                "pr_total": 0,
                "has_ci_config": has_ci,
                "gh_query_ok": False,
            },
        )

    # Scoring: PR success 70%, CI config 30%
    pr_score = (pr_success_count / 3.0) if pr_total else 0.0
    current_value = pr_score * 0.7 + (1.0 if has_ci else 0.0) * 0.3
    progress_percent = int(current_value * 100)

    if pr_success_count >= 3:
        status = GoalStatus.COMPLETE
        status_reason = "CI Green: 3 consecutive PRs with all checks passing"
    elif pr_success_count >= 2:
        status = GoalStatus.ON_TRACK
        status_reason = f"{pr_success_count}/3 PRs passing all checks"
    elif has_ci and pr_success_count >= 1:
        status = GoalStatus.AT_RISK
        status_reason = f"Only {pr_success_count}/{pr_total} recent PRs fully green"
    elif has_ci:
        status = GoalStatus.AT_RISK
        status_reason = "CI configured but recent PRs have failures"
    else:
        status = GoalStatus.NOT_STARTED
        status_reason = "CI not yet configured"

    # Generate concrete next_actions naming specific failed tests instead of
    # bare goal metric ("0 failures"). This prevents phantom tasks where the
    # diff fixes assessor bugs but the task title claims "CI Green: 3 consecutive PRs".
    next_actions: list[str] = []
    if status != GoalStatus.COMPLETE:
        failing_tests = _find_failing_ci_tests(project_root)
        if failing_tests:
            tests_str = ", ".join(failing_tests)
            next_actions = [f"Fix failing CI checks: {tests_str}"]
        else:
            next_actions = ["Stabilize flaky tests to achieve 3 consecutive green PRs"]

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=progress_percent,
        status=status,
        status_reason=status_reason,
        blockers=[],
        next_actions=next_actions,
        raw_data={
            "pr_success_count": pr_success_count,
            "pr_total": pr_total,
            "has_ci_config": has_ci,
            "gh_query_ok": True,
        },
    )


_METRIC_ARTIFACT_RE = re.compile(r"`([^`]+)`|(/[a-zA-Z][\w-]*)")


def _verified_stamp_for_goal(goal: GoalDefinition, company_dir: Path) -> dict | None:
    """Return this goal's verification-state record iff it is a live, matching stamp.

    A stamp only counts if its recorded goal_name + success_metric still match the
    goal's CURRENT text — if vision.md redefines the goal after verification, the
    stale stamp must not silently keep reporting the (now different) goal complete.
    Returns None on any missing/corrupt/mismatched/unverified state (never raises —
    ``load_goal_verification_state`` already fails closed to {} on read errors).
    """
    record = load_goal_verification_state(company_dir).get(goal.id)
    if not isinstance(record, dict) or record.get("status") != "verified":
        return None
    if _normalize_fingerprint_text(
        record.get("goal_name", "")
    ) != _normalize_fingerprint_text(goal.name):
        return None
    if _normalize_fingerprint_text(
        record.get("success_metric", "")
    ) != _normalize_fingerprint_text(goal.success_metric):
        return None
    return record


def assess_generic_success_metric(
    goal: GoalDefinition, company_dir: Path, *, lightweight: bool = False
) -> GoalAssessment:
    """Product-agnostic fallback assessor, driven only by the goal's own text.

    Used for any goal that isn't the framework's own dogfood goal (see
    ``_is_framework_dogfood_goal``) — e.g. a goal ID like "G1" reused by a
    downstream product for something unrelated to Forge's own test coverage.
    Unlike the framework-specific assessors, this has no per-product domain
    knowledge, so it deliberately never *auto-derives* ON_TRACK or COMPLETE from
    the goal text alone — a wrong "complete" here would be written into
    goal_verification_state.json as a silent, hard-to-detect lie. It can only
    surface a weak, honest signal and otherwise defers to human/PR verification
    via ``verify_goal``.

    The one exception: if ``verify_goal`` has already stamped this exact goal
    (same id + matching name/metric text) as verified — via operator confirm or
    a passing PR judge — this returns COMPLETE so that stamp actually sticks
    (otherwise autofill_queue_from_goals would keep targeting a "done" goal
    forever; see ProjectK finding K1b).
    """
    stamp = _verified_stamp_for_goal(goal, company_dir)
    if stamp is not None:
        method = stamp.get("method", "verify_goal")
        pr_suffix = f" (PR #{stamp['pr_number']})" if stamp.get("pr_number") else ""
        return GoalAssessment(
            goal_id=goal.id,
            goal_name=goal.name,
            description=goal.description,
            success_metric=goal.success_metric,
            owner=goal.owner,
            current_value=1.0,
            target_value=1.0,
            progress_percent=100,
            status=GoalStatus.COMPLETE,
            status_reason=(
                f"Verified via {method} on {stamp.get('verified_at', 'unknown date')}"
                f"{pr_suffix}."
            ),
            next_actions=[],
            raw_data={
                "assessor": "generic_success_metric",
                "dogfood_match": False,
                "verified_stamp": True,
                "verified_at": stamp.get("verified_at"),
                "pr_number": stamp.get("pr_number"),
                "pr_url": stamp.get("pr_url"),
                "evidence": stamp.get("evidence"),
                "method": method,
            },
        )

    project_root = company_dir.parent
    success_metric = (goal.success_metric or "").strip()

    if not success_metric:
        return GoalAssessment(
            goal_id=goal.id,
            goal_name=goal.name,
            description=goal.description,
            success_metric=goal.success_metric,
            owner=goal.owner,
            current_value=0.0,
            target_value=1.0,
            progress_percent=0,
            status=GoalStatus.NOT_STARTED,
            status_reason="No success metric text provided; nothing to auto-assess.",
            raw_data={"assessor": "generic_success_metric", "dogfood_match": False},
        )

    # Best-effort: if the goal author named a concrete artifact (backtick-quoted
    # path/command, or a bare /slash-command), check whether it exists. This is
    # a weak signal, not proof the metric is satisfied — capped at AT_RISK.
    artifact = None
    match = _METRIC_ARTIFACT_RE.search(success_metric)
    if match:
        artifact = (match.group(1) or match.group(2) or "").strip()

    artifact_exists = False
    if artifact and not artifact.startswith("/") and ".." not in Path(artifact).parts:
        try:
            resolved_root = project_root.resolve()
            resolved_artifact = (project_root / artifact).resolve()
            if resolved_artifact.is_relative_to(resolved_root):
                artifact_exists = resolved_artifact.exists()
        except (OSError, ValueError):
            artifact_exists = False

    if artifact_exists:
        status = GoalStatus.AT_RISK
        current_value = 0.5
        status_reason = (
            f"Referenced artifact '{artifact}' exists on disk, but generic "
            "assessment cannot confirm it satisfies the full success metric; "
            f"requires manual verification (see /verify {goal.id})."
        )
    else:
        status = GoalStatus.NOT_STARTED
        current_value = 0.0
        status_reason = (
            f'Cannot auto-verify "{success_metric}" generically; requires '
            f"manual verification (see /verify {goal.id})."
        )

    return GoalAssessment(
        goal_id=goal.id,
        goal_name=goal.name,
        description=goal.description,
        success_metric=goal.success_metric,
        owner=goal.owner,
        current_value=current_value,
        target_value=1.0,
        progress_percent=int(current_value * 100),
        status=status,
        status_reason=status_reason,
        next_actions=[f"Manually verify and run /verify {goal.id}"],
        raw_data={
            "assessor": "generic_success_metric",
            "dogfood_match": False,
            "artifact": artifact,
            "artifact_exists": artifact_exists,
        },
    )


# -----------------------------------------------------------------------------
# Assessor Registry
# -----------------------------------------------------------------------------


GOAL_ASSESSORS: dict[str, Callable[[GoalDefinition, Path], GoalAssessment]] = {
    "G1": assess_test_coverage,
    "G2": assess_tutorials,
    "G3": assess_stability,
    "G4": assess_enterprise,
    "G5": assess_autonomy,
    "G6": assess_economics,
    "G7": assess_sustained_autonomy,
    "G8": assess_self_improvement,
    "G9": assess_stability,
    "G10": assess_employee_initiative,
    "G11": assess_queue_health,
    "G12": assess_parallel_throughput,
    "G13": assess_first_revenue,
    "G14": assess_product_packaging,
    "G15": assess_website_conversion,
    "G16": assess_ci_green,
}


# Default goal definitions (from vision.md)
DEFAULT_GOALS: list[GoalDefinition] = [
    GoalDefinition(
        "G1",
        "Quality",
        "Increase test coverage to 50%",
        "Coverage badge shows 50%+",
        "forge-cto",
        0.5,
    ),
    GoalDefinition(
        "G2",
        "Adoption",
        "Create getting-started tutorials",
        "3 tutorials published",
        "marketing-lead",
        1.0,
    ),
    GoalDefinition(
        "G3",
        "Stability",
        "Zero critical bugs in core workflow",
        "0 P0 bugs open",
        "forge-architect",
        1.0,
    ),
    GoalDefinition(
        "G4",
        "Enterprise",
        "Expand audit capabilities",
        "Audit export command exists",
        "forge-security-engineer",
        1.0,
    ),
    GoalDefinition(
        "G5",
        "Autonomy",
        "Enable fully autonomous operation",
        "Org runs without prompts for 24h",
        "forge-cto",
        1.0,
    ),
    GoalDefinition(
        "G6",
        "Economics",
        "Track token consumption in subscription",
        "Token tracking via efficiency_tracker",
        "forge-architect",
        1.0,
    ),
    GoalDefinition(
        "G7",
        "Sustained Autonomy",
        "Self-sustaining operations",
        "7 days autonomous, >90% success rate",
        "forge-cto",
        1.0,
        period="Q2 2026",
        period_status="active",
    ),
    GoalDefinition(
        "G8",
        "Self-Improvement",
        "Self-improvement capability",
        "10+ approved improvement proposals",
        "forge-cto",
        1.0,
        period="Q2 2026",
        period_status="active",
    ),
    GoalDefinition(
        "G9",
        "Stability",
        "All tests passing, zero regressions",
        "0 test failures",
        "forge-architect",
        1.0,
        period="Q2 2026",
        period_status="active",
    ),
    GoalDefinition(
        "G10",
        "Employee Initiative",
        "Employee-initiated tasks",
        "50% tasks from employee ideas",
        "forge-cto",
        1.0,
        period="Q2 2026",
        period_status="active",
    ),
    GoalDefinition(
        "G11",
        "Queue Health",
        "Self-generating work queue",
        "Queue never empty >1 hour",
        "forge-cto",
        1.0,
        period="Q2 2026",
        period_status="active",
    ),
    GoalDefinition(
        "G12",
        "Parallel Throughput",
        "Validate parallel worktree execution under load",
        "2+ workers active simultaneously for 24h",
        "forge-cto",
        1.0,
        period="Q2 2026",
        period_status="active",
    ),
    GoalDefinition(
        "G13",
        "First Revenue",
        "Close first consulting engagement or license sale",
        "$1 revenue",
        "revenue-sales-lead",
        1.0,
        period="Q2 2026",
        period_status="active",
    ),
    GoalDefinition(
        "G14",
        "Product Packaging",
        "Ship Compliance Pack as downloadable product",
        "Working /compliance-pack output",
        "forge-security-engineer",
        1.0,
        period="Q2 2026",
        period_status="active",
    ),
    GoalDefinition(
        "G15",
        "Website Conversion",
        "O2-KR1: inquiry/signup path (contact CTA + /license activation) works end to end",
        "Inquiry path live: contact CTA + /license activation",
        "external-webmaster",
        1.0,
        period="Q2 2026",
        period_status="active",
    ),
    GoalDefinition(
        "G16",
        "CI Green",
        "All CI checks passing on every PR",
        "0 failures on 3 consecutive PRs",
        "forge-architect",
        1.0,
        period="Q2 2026",
        period_status="active",
    ),
]


_DEFAULT_GOALS_BY_ID: dict[str, GoalDefinition] = {g.id: g for g in DEFAULT_GOALS}


def _normalize_fingerprint_text(text: str) -> str:
    """Normalize text for dogfood fingerprint comparison (case/whitespace only)."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _is_framework_dogfood_goal(goal: GoalDefinition) -> bool:
    """Is this goal actually Forge's own canonical goal for this ID?

    GOAL_ASSESSORS dispatches by goal ID string alone ("G1".."G16"), but any
    project using Forge can write its own vision.md reusing those same IDs
    for unrelated goals (e.g. a csv2md tool's "G1" meaning something totally
    different from Forge's own "G1: test coverage"). Routing those through
    Forge's framework-specific assessors produces nonsense results measuring
    the wrong thing entirely.

    A repo-identity check (e.g. "does .claude/commands/audit-export.md
    exist") is NOT sufficient here — any project scaffolded from Forge
    ships that same .claude/ directory, so it would still misroute.
    Instead, compare the parsed goal's own name + success_metric text
    against Forge's canonical DEFAULT_GOALS definition for that ID: only an
    exact (case/whitespace-normalized) match on BOTH fields counts as
    "this really is Forge's own goal." description/owner/period are
    deliberately excluded — they're either synthesized differently by the
    tolerant vision.md table parser or legitimately vary (e.g. reassigned
    ownership) without changing what the goal actually measures.
    """
    default = _DEFAULT_GOALS_BY_ID.get(goal.id)
    if default is None:
        return False
    return _normalize_fingerprint_text(goal.name) == _normalize_fingerprint_text(
        default.name
    ) and _normalize_fingerprint_text(
        goal.success_metric
    ) == _normalize_fingerprint_text(default.success_metric)


# -----------------------------------------------------------------------------
# Main Functions
# -----------------------------------------------------------------------------


def parse_period_markers(content: str) -> list[dict]:
    """Parse period markers from vision.md content.

    Looks for headers like: ### Period: Q1 2026 [status: complete]
    Returns list of dicts: [{"name": "Q1 2026", "status": "complete", "start_pos": N}, ...]
    """
    pattern = r"### Period:\s*(.+?)\s*\[status:\s*(\w+)\]"
    markers = []
    for match in re.finditer(pattern, content):
        markers.append(
            {
                "name": match.group(1).strip(),
                "status": match.group(2).strip(),
                "start_pos": match.start(),
            }
        )
    return markers


def get_active_period(vision_path: Path) -> str | None:
    """Get the name of the currently active period from vision.md."""
    if not vision_path.exists():
        return None

    try:
        content = vision_path.read_text()
    except Exception:
        return None

    markers = parse_period_markers(content)
    for marker in markers:
        if marker["status"] == "active":
            return marker["name"]
    return None


def check_period_transition(company_dir: Path) -> dict | None:
    """Check if the active period is complete and advance to next.

    Reads vision.md, assesses all goals in the active period.
    If ALL goals have status COMPLETE, rewrites vision.md to:
    - Mark active period as [status: complete]
    - Mark next period as [status: active]

    Returns:
        Dict with transition details if transition occurred, None otherwise.
    """
    import tempfile

    vision_path = company_dir / "vision.md"
    if not vision_path.exists():
        return None

    active_period = get_active_period(vision_path)
    if not active_period:
        return None

    goals = parse_goals_from_vision(vision_path, period=active_period)
    if not goals:
        return None

    # Assess each goal (lightweight to avoid expensive operations)
    all_complete = True
    for goal in goals:
        assessment = assess_goal(goal, company_dir, lightweight=True)
        if (
            assessment.status != GoalStatus.COMPLETE
            and assessment.progress_percent < 100
        ):
            all_complete = False
            break

    if not all_complete:
        return None

    # All goals complete — find the next period and transition
    content = vision_path.read_text()
    markers = parse_period_markers(content)

    if len(markers) < 2:
        return None

    # Find the active period index
    active_idx = None
    for i, marker in enumerate(markers):
        if marker["name"] == active_period and marker["status"] == "active":
            active_idx = i
            break

    if active_idx is None:
        return None

    # Check there is a next period
    if active_idx + 1 >= len(markers):
        return None

    next_period = markers[active_idx + 1]
    next_name = next_period["name"]
    next_status = next_period["status"]

    # Replace the active period's status marker with "complete"
    old_active_marker = f"### Period: {active_period} [status: active]"
    new_active_marker = f"### Period: {active_period} [status: complete]"
    updated = content.replace(old_active_marker, new_active_marker, 1)

    # Replace the next period's status marker with "active"
    old_next_marker = f"### Period: {next_name} [status: {next_status}]"
    new_next_marker = f"### Period: {next_name} [status: active]"

    if old_next_marker in updated:
        updated = updated.replace(old_next_marker, new_next_marker, 1)
    else:
        # Next period might not have a status marker — add one by finding
        # the period header line (e.g. "### Period: Q3 2026") without status
        # and appending the status marker.
        bare_pattern = re.compile(
            r"(###\s+Period:\s*" + re.escape(next_name) + r")\s*$",
            re.MULTILINE,
        )
        match = bare_pattern.search(updated)
        if match:
            updated = (
                updated[: match.end()] + " [status: active]" + updated[match.end() :]
            )

    # Atomic write to vision.md
    dir_path = vision_path.parent
    fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(updated)
        os.replace(tmp_path, str(vision_path))
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return None

    return {
        "old_period": active_period,
        "new_period": next_name,
        "goals_completed": len(goals),
        "transitioned_at": datetime.now(timezone.utc).isoformat(),
    }


def parse_goals_from_vision(
    vision_path: Path, *, period: str = "active"
) -> list[GoalDefinition]:
    """Parse goal definitions from vision.md.

    Args:
        vision_path: Path to vision.md file.
        period: Which period to parse goals from.
            "active" — only goals from the active period section.
            "all" — goals from all periods.
            A specific name (e.g. "Q1 2026") — goals from that period only.
    """
    if not vision_path.exists():
        return DEFAULT_GOALS

    try:
        content = vision_path.read_text()
    except Exception:
        return DEFAULT_GOALS

    # Parse period markers to determine content boundaries
    markers = parse_period_markers(content)

    # If no period markers found, fall back to existing behavior (parse all goals)
    if not markers:
        return _parse_goals_from_content(content, period_name=None, period_status=None)

    # Determine which sections to parse based on the period parameter
    sections: list[tuple[str, str | None, str | None]] = []  # (content, name, status)

    for i, marker in enumerate(markers):
        start = marker["start_pos"]
        end = markers[i + 1]["start_pos"] if i + 1 < len(markers) else len(content)
        section_content = content[start:end]
        m_name = marker["name"]
        m_status = marker["status"]

        if period == "all":
            sections.append((section_content, m_name, m_status))
        elif period == "active":
            if m_status == "active":
                sections.append((section_content, m_name, m_status))
        else:
            # Specific period name requested
            if m_name == period:
                sections.append((section_content, m_name, m_status))

    goals: list[GoalDefinition] = []
    for section_content, p_name, p_status in sections:
        goals.extend(
            _parse_goals_from_content(
                section_content, period_name=p_name, period_status=p_status
            )
        )

    return goals if goals else DEFAULT_GOALS


def _warn_dep_cycles(goals: list[GoalDefinition]) -> None:
    """Emit a stderr warning if circular dependsOn chains are detected (DFS)."""
    known = {g.id for g in goals}
    adj = {g.id: [d for d in g.depends_on if d in known] for g in goals}
    visited: set[str] = set()
    in_stack: set[str] = set()

    def _has_cycle(node: str) -> bool:
        if node in in_stack:
            return True
        if node in visited:
            return False
        visited.add(node)
        in_stack.add(node)
        result = any(_has_cycle(dep) for dep in adj.get(node, []))
        in_stack.discard(node)
        return result

    for goal_id in list(adj):
        if goal_id not in visited and _has_cycle(goal_id):
            print(
                f"WARNING goal_tracker: circular dependsOn detected involving {goal_id}; "
                "dep-blocked goals will be suppressed indefinitely",
                file=sys.stderr,
            )
            return


def _parse_goals_from_content(
    content: str, *, period_name: str | None, period_status: str | None
) -> list[GoalDefinition]:
    """Parse goal table rows from a content string.

    Args:
        content: Text content containing markdown goal tables.
        period_name: Period name to set on each parsed goal (e.g. "Q1 2026").
        period_status: Period status to set on each parsed goal (e.g. "active").
    """
    goals: list[GoalDefinition] = []
    seen_ids: set[str] = set()

    def _add_goal(goal_id, name, description, success_metric, owner):
        goal_id = goal_id.strip()
        if goal_id in seen_ids:
            return  # a row already matched by a stricter pattern wins
        seen_ids.add(goal_id)
        name = name.strip()
        description = description.strip()
        # Parse optional dependsOn annotation (e.g. "Enable autonomy dependsOn: G1,G2")
        depends_on: list[str] = []
        dep_match = re.search(r"\s+dependsOn:\s*", description, re.IGNORECASE)
        if dep_match:
            dep_text = description[dep_match.end() :]
            description = description[: dep_match.start()].strip()
            depends_on = re.findall(r"\bG\d+\b", dep_text)
        goals.append(
            GoalDefinition(
                goal_id,
                name,
                description,
                success_metric.strip(),
                owner.strip(),
                period=period_name,
                period_status=period_status,
                depends_on=depends_on,
            )
        )

    # Primary (canonical) format: | G1: Quality | Desc | Metric | Owner |
    # (optionally bold). Non-greedy ([^|]+?) matches multi-word names.
    strict_pattern = r"\|\s*\*{0,2}(G\d+):\s*([^|]+?)\*{0,2}\s*\|\s*([^|]+)\|\s*([^|]+)\|\s*([^|]+)\|"
    for match in re.finditer(strict_pattern, content):
        _add_goal(*match.groups())

    # Tolerated 5-column format with the ID in its OWN cell:
    # | G1 | Category | Goal | Success Criteria | Owner |  (no colon after the ID).
    # This is what the daemon's _create_default_vision historically emitted and a
    # natural shape a user might write (ProjectK K3). Anchored on `Gn` alone so it
    # never matches an arbitrary/non-goal table. The Category column is folded into
    # the description; the Goal column becomes the name.
    five_col_pattern = (
        r"\|\s*\*{0,2}(G\d+)\*{0,2}\s*\|\s*([^|]+)\|\s*([^|]+?)\s*\|"
        r"\s*([^|]+)\|\s*([^|]+)\|"
    )
    for match in re.finditer(five_col_pattern, content):
        goal_id, category, goal_name, success_metric, owner = match.groups()
        category = category.strip()
        goal_name = goal_name.strip()
        # Skip a Markdown separator row like | G1 |---|---|---|---| defensively.
        if set(goal_name) <= {"-", ":", " "}:
            continue
        description = (
            f"{category}: {goal_name}" if category and category != "-" else goal_name
        )
        _add_goal(goal_id, goal_name, description, success_metric, owner)

    _warn_dep_cycles(goals)
    return goals


def assess_goal(
    goal: GoalDefinition, company_dir: Path, *, lightweight: bool = False
) -> GoalAssessment:
    """Assess a single goal using the appropriate assessor.

    Framework-specific assessors in GOAL_ASSESSORS only apply when this goal
    is verifiably Forge's own dogfood goal for its ID (see
    ``_is_framework_dogfood_goal``) — otherwise a downstream product reusing
    goal IDs G1-G16 for unrelated goals would get measured by Forge's own
    test-coverage/tutorials/autonomy checks instead of its own success
    metric. Any other goal falls through to the generic, product-agnostic
    assessor.

    Args:
        lightweight: If True, skip expensive operations (pytest).
            Used by daemon to keep cycles fast.
    """
    assessor = GOAL_ASSESSORS.get(goal.id) if _is_framework_dogfood_goal(goal) else None
    if assessor is None:
        assessor = assess_generic_success_metric

    # Pass lightweight to assessors that accept it (G1, G3, generic, ...)
    import inspect

    sig = inspect.signature(assessor)
    if "lightweight" in sig.parameters:
        return assessor(goal, company_dir, lightweight=lightweight)
    return assessor(goal, company_dir)


def calculate_goal_velocity(
    goal_id: str,
    company_dir: Path,
    lookback_days: int = 7,
) -> tuple[float, str]:
    """
    WS-113-003: Calculate goal velocity (progress change per day).

    Uses goal snapshots from strategic state to determine how fast
    a goal is progressing, stalling, or regressing.

    Args:
        goal_id: The goal ID to calculate velocity for
        company_dir: Company directory path
        lookback_days: Number of days to look back for velocity calculation

    Returns:
        Tuple of (velocity_percent_per_day, trend)
        - velocity: positive = improving, negative = regressing, ~0 = stalled
        - trend: "improving", "stalled", or "regressing"
    """
    state_path = company_dir / "state" / "strategic_state.json"
    if not state_path.exists():
        return 0.0, "unknown"

    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f)
    except (json.JSONDecodeError, OSError):
        return 0.0, "unknown"

    snapshots = state.get("goal_snapshots", [])
    if len(snapshots) < 2:
        return 0.0, "unknown"

    # Get relevant snapshots within lookback period
    from datetime import datetime, timedelta
    from datetime import timezone as tz

    cutoff = datetime.now(tz.utc) - timedelta(days=lookback_days)
    recent_snapshots = []

    for snap in snapshots:
        try:
            snap_time = datetime.fromisoformat(snap["timestamp"].replace("Z", "+00:00"))
            if snap_time >= cutoff:
                recent_snapshots.append(snap)
        except (KeyError, ValueError):
            continue

    if len(recent_snapshots) < 2:
        # Fall back to last 2 snapshots regardless of time
        recent_snapshots = snapshots[-2:]

    # Find goal progress in first and last snapshot
    first_snap = recent_snapshots[0]
    last_snap = recent_snapshots[-1]

    first_progress = None
    last_progress = None

    for assess in first_snap.get("assessments", []):
        if assess.get("goal_id") == goal_id:
            first_progress = assess.get("progress", 0)
            break

    for assess in last_snap.get("assessments", []):
        if assess.get("goal_id") == goal_id:
            last_progress = assess.get("progress", 0)
            break

    if first_progress is None or last_progress is None:
        return 0.0, "unknown"

    # Calculate time delta
    try:
        first_time = datetime.fromisoformat(
            first_snap["timestamp"].replace("Z", "+00:00")
        )
        last_time = datetime.fromisoformat(
            last_snap["timestamp"].replace("Z", "+00:00")
        )
        days_elapsed = max((last_time - first_time).total_seconds() / 86400, 0.1)
    except (KeyError, ValueError):
        days_elapsed = 1.0

    # Calculate velocity (percent change per day)
    progress_delta = last_progress - first_progress
    velocity = progress_delta / days_elapsed

    # Determine trend
    if velocity > 2.0:  # >2% per day = improving
        trend = "improving"
    elif velocity < -1.0:  # <-1% per day = regressing
        trend = "regressing"
    else:
        trend = "stalled"

    return round(velocity, 2), trend


def assess_all_goals(
    company_dir: Path, *, lightweight: bool = False, period: str = "active"
) -> list[GoalAssessment]:
    """Assess all goals and return assessments.

    Args:
        lightweight: If True, skip expensive operations (pytest).
            Daemon cycles use this to avoid 5-10 min blocking.
            Manual ``/strategy assess`` uses full mode by default.
        period: Which period to assess goals from ("active", "all", or a
            specific period name like "Q1 2026").
    """
    vision_path = company_dir.parent / ".company" / "vision.md"
    if not vision_path.exists():
        vision_path = company_dir / "vision.md"

    goals = parse_goals_from_vision(vision_path, period=period)
    assessments = []

    for goal in goals:
        assessment = assess_goal(goal, company_dir, lightweight=lightweight)

        # WS-113-003: Add velocity tracking
        velocity, trend = calculate_goal_velocity(goal.id, company_dir)
        assessment.velocity_percent_per_day = velocity
        assessment.velocity_trend = trend
        assessment.depends_on = goal.depends_on

        assessments.append(assessment)

    return assessments


_VERIFICATION_STATE_FILE = "state/goal_verification_state.json"


def load_goal_verification_state(company_dir: Path) -> dict[str, dict]:
    """Load goal verification state from disk.

    Returns dict mapping goal_id -> verification record (keys: status,
    verified_at, pr_number, pr_url, goal_name, success_metric).
    Returns {} when the file is absent or unreadable.
    """
    state_path = company_dir / _VERIFICATION_STATE_FILE
    if not state_path.exists():
        return {}
    try:
        with open(state_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _stamp_goal_verified(
    goal: GoalDefinition,
    company_dir: Path,
    *,
    pr_number: str | int | None,
    pr_url: str | None,
    evidence: str | None,
    method: str,
    progress_percent: int = 100,
) -> dict:
    """Write a verified record for ``goal`` and return the CLI-facing result dict."""
    verified_at = datetime.now(timezone.utc).isoformat()
    state = load_goal_verification_state(company_dir)
    state[goal.id] = {
        "status": "verified",
        "verified_at": verified_at,
        "pr_number": pr_number,
        "pr_url": pr_url,
        "goal_name": goal.name,
        "success_metric": goal.success_metric,
        "progress_percent": progress_percent,
        "evidence": evidence,
        "method": method,
    }

    state_path = company_dir / _VERIFICATION_STATE_FILE
    state_path.parent.mkdir(parents=True, exist_ok=True)
    with open(state_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

    return {
        "goal_id": goal.id,
        "status": "verified",
        "message": f"Goal {goal.id} verified via {method}.",
        "pr_number": pr_number,
        "pr_url": pr_url,
        "verified_at": verified_at,
    }


def _judge_goal_completion_via_pr(
    goal: GoalDefinition, pr_number: str | int
) -> dict | None:
    """Ask the deliverable judge whether a merged PR satisfies this goal's metric.

    Reuses deliverable_judge's existing fail-closed CLI plumbing (same judge used
    for the pre-merge task gate) instead of re-implementing CLI-detection/subprocess
    logic here. Returns None on ANY failure — no claude/uv CLI on PATH, no `gh`,
    judge timeout/error, unparseable output, low-confidence/negative verdict, or an
    import failure — so the caller never stamps a goal on an inconclusive signal.
    Never raises.
    """
    try:
        try:
            from . import deliverable_judge as _deliverable_judge
        except ImportError:
            import deliverable_judge as _deliverable_judge
    except ImportError:
        return None

    try:
        verdict = _deliverable_judge.judge_pr_deliverable(
            task_id=f"goal-{goal.id}",
            title=goal.name,
            description=goal.description,
            pr_number=pr_number,
            goal_metric=goal.success_metric,
            use_cache=False,
        )
    except Exception:
        return None

    if (
        verdict.error is not None
        or verdict.addresses_task is not True
        or verdict.blocked
        or verdict.needs_manual_review
    ):
        return None
    return {"reason": verdict.reason, "confidence": verdict.confidence}


def verify_goal(
    goal_id: str,
    company_dir: Path,
    *,
    pr_number: str | int | None = None,
    pr_url: str | None = None,
    confirm: bool = False,
    evidence: str | None = None,
) -> dict:
    """Run goal metric check post-merge and stamp verified state.

    Loads the goal from vision.md and determines completeness one of three ways,
    in order:

    1. **Assessment-based** (default, all goals): runs ``assess_goal``; if it
       reports COMPLETE, stamps verified. This is the only path available to
       Forge's own dogfood goals (see ``_is_framework_dogfood_goal``) — their
       framework-specific assessors are the sole source of truth, so ``confirm``
       has NO effect on them.
    2. **Operator confirm** (generic-assessed goals only): ``assess_generic_success_metric``
       deliberately never returns COMPLETE on its own (a wrong auto-complete would
       be a silent lie), so it needs a real escape hatch. Passing ``confirm=True``
       with non-empty ``evidence`` text stamps the goal directly, bypassing the
       assessment gate. Fails closed (not stamped) if evidence is missing/blank.
    3. **PR-judge** (generic-assessed goals only, opt-in via ``pr_number``): when
       the assessment didn't complete the goal and no confirm was given, asks the
       same LLM judge used for pre-merge deliverable gating whether the merged
       PR's diff satisfies the goal's success metric. Fails closed to not-stamped
       on any judge error, missing CLI, or a negative/low-confidence verdict.

    Args:
        goal_id: Goal identifier, e.g. "G1"
        company_dir: Path to .company directory
        pr_number: Merged PR number for evidence record / PR-judge lookup
        pr_url: Merged PR URL for evidence record
        confirm: Operator-asserted completion (generic-assessed goals only)
        evidence: Required justification text when ``confirm=True``

    Returns:
        Dict with keys: goal_id, status ("verified" | "not_complete" |
        "not_found"), message, pr_number, pr_url, and (when verified)
        verified_at. "not_complete" results also carry assessment_status.
    """
    vision_path = company_dir.parent / ".company" / "vision.md"
    if not vision_path.exists():
        vision_path = company_dir / "vision.md"

    goals = parse_goals_from_vision(vision_path)
    goal = next((g for g in goals if g.id == goal_id), None)
    if goal is None:
        return {
            "goal_id": goal_id,
            "status": "not_found",
            "message": f"Goal {goal_id} not found in vision.md",
        }

    is_dogfood = _is_framework_dogfood_goal(goal)

    # Idempotent for generic-assessed goals only: an already-live stamp wins
    # outright, so a repeat call never clobbers the original evidence/method with
    # a weaker record. Dogfood goals deliberately re-run their real assessor every
    # time (skip this shortcut) so a metric that later regresses (e.g. a new P0
    # bug after G3 was once verified) is never masked by a stale "already verified".
    existing_stamp = None if is_dogfood else _verified_stamp_for_goal(goal, company_dir)
    if existing_stamp is not None:
        return {
            "goal_id": goal_id,
            "status": "verified",
            "message": (
                f"Goal {goal_id} already verified via "
                f"{existing_stamp.get('method', 'verify_goal')} on "
                f"{existing_stamp.get('verified_at', 'unknown date')}."
            ),
            "pr_number": existing_stamp.get("pr_number"),
            "pr_url": existing_stamp.get("pr_url"),
            "verified_at": existing_stamp.get("verified_at"),
        }

    # Operator confirm: a legitimate bypass ONLY for generic-assessed goals, whose
    # own assessor can never return COMPLETE by design. Dogfood goals keep the
    # strict assessment gate unconditionally — confirm is silently ignored for them
    # rather than special-cased into an error, since callers may pass confirm=True
    # generically without knowing a goal's dogfood status.
    if confirm and not is_dogfood:
        if not evidence or not evidence.strip():
            return {
                "goal_id": goal_id,
                "status": "not_complete",
                "message": (
                    f"Cannot confirm goal {goal_id} without evidence text, e.g. "
                    f'/verify {goal_id} --confirm "evidence..."'
                ),
                "pr_number": pr_number,
                "pr_url": pr_url,
            }
        return _stamp_goal_verified(
            goal,
            company_dir,
            pr_number=pr_number,
            pr_url=pr_url,
            evidence=evidence.strip(),
            method="operator_confirm",
        )

    assessment = assess_goal(goal, company_dir)

    if assessment.status == GoalStatus.COMPLETE:
        return _stamp_goal_verified(
            goal,
            company_dir,
            pr_number=pr_number,
            pr_url=pr_url,
            evidence=None,
            method="assessment",
            progress_percent=assessment.progress_percent,
        )

    # PR-judge fallback: only meaningful for generic-assessed goals (dogfood goals
    # already have a real assessor; a second opinion from a generic LLM judge
    # would just be a lower-quality duplicate of that check).
    if pr_number is not None and not is_dogfood:
        judged = _judge_goal_completion_via_pr(goal, pr_number)
        if judged is not None:
            return _stamp_goal_verified(
                goal,
                company_dir,
                pr_number=pr_number,
                pr_url=pr_url,
                evidence=judged["reason"],
                method="pr_judge",
            )

    return {
        "goal_id": goal_id,
        "status": "not_complete",
        "assessment_status": assessment.status.value,
        "message": f"Goal {goal_id} metric check failed: {assessment.status_reason}",
        "pr_number": pr_number,
        "pr_url": pr_url,
    }


def get_company_dir() -> Path:
    """Find the company directory."""
    # Try current directory
    cwd = Path.cwd()
    if (cwd / ".company").exists():
        return cwd / ".company"

    # Try to find via company_resolver
    try:
        result = subprocess.run(
            ["uv", "run", str(Path(__file__).parent / "company_resolver.py"), "dir"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip())
    except Exception:
        pass

    return cwd / ".company"


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Goal tracker for strategic planning")
    parser.add_argument(
        "command",
        choices=["assess", "summary", "export", "verify"],
        help="Command to run",
    )
    parser.add_argument("--goal", "-g", help="Specific goal ID (e.g., G5)")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--pr", help="PR number (for verify command)")
    parser.add_argument("--pr-url", help="PR URL (for verify command)")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Operator-asserted completion for generic-assessed goals (verify command)",
    )
    parser.add_argument(
        "--evidence",
        help="Required justification text when using --confirm (verify command)",
    )

    args = parser.parse_args()

    company_dir = get_company_dir()

    if args.command == "assess":
        if args.goal:
            # Assess specific goal
            goals = parse_goals_from_vision(company_dir / "vision.md")
            goal = next((g for g in goals if g.id == args.goal), None)
            if not goal:
                print(f"Goal {args.goal} not found", file=sys.stderr)
                sys.exit(1)
            assessment = assess_goal(goal, company_dir)
            assessments = [assessment]
        else:
            # Assess all goals
            assessments = assess_all_goals(company_dir)

        if args.json:
            print(json.dumps([a.to_dict() for a in assessments], indent=2))
        else:
            for a in assessments:
                status_emoji = {
                    GoalStatus.COMPLETE: "✓",
                    GoalStatus.ON_TRACK: "→",
                    GoalStatus.AT_RISK: "!",
                    GoalStatus.BLOCKED: "✗",
                    GoalStatus.NOT_STARTED: "○",
                }.get(a.status, "?")
                print(
                    f"{status_emoji} {a.goal_id}: {a.goal_name} — {a.progress_percent}% ({a.status.value})"
                )
                print(f"  {a.status_reason}")
                if a.blockers:
                    print(f"  Blockers: {', '.join(a.blockers)}")
                if a.next_actions:
                    print(f"  Next: {', '.join(a.next_actions)}")
                print()

    elif args.command == "summary":
        assessments = assess_all_goals(company_dir)
        complete = sum(1 for a in assessments if a.status == GoalStatus.COMPLETE)
        on_track = sum(1 for a in assessments if a.status == GoalStatus.ON_TRACK)
        at_risk = sum(1 for a in assessments if a.status == GoalStatus.AT_RISK)
        blocked = sum(1 for a in assessments if a.status == GoalStatus.BLOCKED)

        overall = sum(a.progress_percent for a in assessments) / len(assessments)

        if args.json:
            print(
                json.dumps(
                    {
                        "overall_progress": overall,
                        "complete": complete,
                        "on_track": on_track,
                        "at_risk": at_risk,
                        "blocked": blocked,
                        "total": len(assessments),
                    },
                    indent=2,
                )
            )
        else:
            print(f"Overall Progress: {overall:.0f}%")
            print(f"  ✓ Complete: {complete}")
            print(f"  → On Track: {on_track}")
            print(f"  ! At Risk: {at_risk}")
            print(f"  ✗ Blocked: {blocked}")

    elif args.command == "export":
        assessments = assess_all_goals(company_dir)
        print(json.dumps([a.to_dict() for a in assessments], indent=2))

    elif args.command == "verify":
        if not args.goal:
            print("--goal is required for the verify command", file=sys.stderr)
            sys.exit(1)
        result = verify_goal(
            args.goal,
            company_dir,
            pr_number=args.pr,
            pr_url=getattr(args, "pr_url", None),
            confirm=getattr(args, "confirm", False),
            evidence=getattr(args, "evidence", None),
        )
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            icon = "✓" if result["status"] == "verified" else "✗"
            print(f"{icon} {result['goal_id']}: {result['message']}")


if __name__ == "__main__":
    main()
