#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Consult REAL git/GitHub PR state before a terminal queue transition (W2-P2).

Recovery and the daemon terminal-mark tasks (blocked / failed) from queue-file
attempt bookkeeping alone. But the ground truth — a branch pushed, a PR open, a
PR merged — lives in git/GitHub, not the queue file. A task can exhaust its
in-queue retries while a PR it produced is still open under review, or already
merged. Terminal-marking such a task strands shipped or under-review work in a
human queue:

  - 2026-07-16: task K1b sat in ``blocked`` while its PR was OPEN on GitHub.
  - 2026-07-17 triage: 5 ``blocked`` tasks were all ghosts of already-merged
    PRs (#220/#223/#226/#229/#230).
  - An "Exhausted retries" escalation fired although attempt 3 had actually
    delivered a PR that later merged.

``consult_pr_state()`` answers one question for a task id: is there a merged or
open PR that references it?

  - a merged PR references it  -> route the task to ``completed`` (work shipped)
  - an open PR references it    -> route the task to ``pr_open``  (under review)
  - neither                     -> caller proceeds with its normal terminal path

The merged signal REUSES ``work_allocator.check_task_already_merged`` (via its
TTL-cached wrapper) — never a reimplementation: git-log evidence plus a
task-id-validated ``gh`` merged search. The open signal is the analogous
``gh pr list --state open`` search, validated the same way (the task id must
actually appear in the returned PR — a fuzzy full-text search hit alone never
routes). A local branch probe is included as diagnostic metadata only; a bare
branch (no PR) is ambiguous — it could be a stale worktree branch from a failed
attempt — so it never drives routing.

FAIL-OPEN is absolute: not a git checkout, any git/gh error, timeout, or
malformed output -> lane ``NONE``. The consult must never hang, crash, or block
the caller on GitHub availability.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

try:
    from . import work_allocator as _wa
except ImportError:  # pragma: no cover - script / dual-import idiom
    import work_allocator as _wa  # type: ignore[no-redef]

logger = logging.getLogger(__name__)


class PRLane(str, Enum):
    """The queue lane a task should route to based on real PR state."""

    COMPLETED = "completed"  # a merged PR references the task — work shipped
    PR_OPEN = "pr_open"  # an open PR references the task — under review
    NONE = "none"  # no PR evidence — caller proceeds with terminal transition


@dataclass
class PRStateVerdict:
    """Outcome of a PR-state consult. ``lane`` drives routing; the rest is
    context for logging / the audit trail."""

    lane: PRLane
    pr_number: int | None = None
    pr_url: str | None = None
    signal: str | None = None
    branch: str | None = None

    @property
    def routes_to_completed(self) -> bool:
        return self.lane is PRLane.COMPLETED

    @property
    def routes_to_pr_open(self) -> bool:
        return self.lane is PRLane.PR_OPEN

    @property
    def is_terminal_ok(self) -> bool:
        """True when NO PR evidence was found — the caller's normal terminal
        transition (blocked / failed / escalate) should proceed unchanged."""
        return self.lane is PRLane.NONE


def _gh_search_open_pr_for_task_id(
    task_id: str,
    *,
    runner: Any = subprocess.run,
    project_root: Path,
    timeout: int = 10,
) -> dict | None:
    """The first OPEN PR that genuinely references ``task_id``.

    ``gh pr list --search <task_id>`` is a fuzzy full-text search, so we require
    the task id to actually appear in the returned PR's title, body, head-branch
    name, or url before treating it as a match (the task-id-trailer validation
    the merged path's reality-audit lineage relies on). A similar-but-unrelated
    PR must never route a task to ``pr_open``.

    Returns None on any gh error, timeout, or malformed output — inconclusive is
    treated the same as "no match"; the caller fails open regardless.
    """
    try:
        result = runner(
            [
                "gh",
                "pr",
                "list",
                "--search",
                task_id,
                "--state",
                "open",
                "--json",
                "number,title,url,headRefName,body",
            ],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    try:
        prs = json.loads((result.stdout or "").strip() or "[]")
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(prs, list):
        return None
    trailer = f"[{task_id}]"
    for pr in prs:
        if not isinstance(pr, dict):
            continue
        # OWNERSHIP signals only (PR 266 review blocker): the daemon's
        # auto-PR trailer in the TITLE, or the task id in the worker-created
        # BRANCH name. A body/url mention is citation, not delivery — briefs
        # and reissue notes routinely quote other tasks' ids, and routing on
        # a mention lets the pr_open reconcile silently complete undelivered
        # work when the citing PR merges (the trap PR 262 closed for the
        # merged path).
        if trailer in str(pr.get("title") or "") or task_id in str(
            pr.get("headRefName") or ""
        ):
            return pr
    return None


def _git_branch_for_task_id(
    task_id: str,
    *,
    runner: Any = subprocess.run,
    project_root: Path,
    timeout: int = 10,
) -> str | None:
    """First local/remote branch name containing ``task_id`` (diagnostic only).

    Never drives routing — a bare branch with no PR is ambiguous. Returned purely
    as verdict metadata so the audit trail can show "a branch was pushed but no
    PR was ever opened" (e.g. PR creation failed after the push). Fails to None
    on any error.
    """
    try:
        result = runner(
            ["git", "branch", "--all", "--format=%(refname:short)"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if getattr(result, "returncode", 1) != 0:
        return None
    for line in (result.stdout or "").splitlines():
        name = line.strip()
        if name and task_id in name:
            return name
    return None


def consult_pr_state(
    task_id: str,
    *,
    runner: Any = subprocess.run,
    project_root: Path | None = None,
    timeout: int = 10,
    include_open: bool = True,
) -> PRStateVerdict:
    """Return the lane a task should route to based on real PR state.

    See the module docstring. Fails open to lane ``NONE`` on anything
    inconclusive — a missing checkout, any git/gh error, timeout, or unexpected
    exception. The check is short-circuited cheapest-first: a filesystem repo
    check, then the (cached) task-id merged-work signals, then the open-PR
    search.
    """
    verdict = PRStateVerdict(lane=PRLane.NONE)
    if not task_id:
        return verdict

    try:
        root = (
            Path(project_root)
            if project_root is not None
            else _wa.get_company_dir().parent
        )
    except Exception:
        return verdict

    try:
        # Guard against ever spawning git/gh against a directory that plainly
        # isn't a checkout (keeps callers' tmp_path-isolated tests subprocess
        # free — the same gate work_allocator's own merged-work check relies on).
        if not _wa._is_git_repo(root):
            return verdict
    except Exception:
        return verdict

    try:
        # (1) MERGED PR -> completed. Reuse work_allocator's merged-work check
        #     (never a reimplementation). Passing an empty title restricts it to
        #     the task-id-precise signals (git-log / gh merged search) and skips
        #     the fuzzy title-similarity probe, which is too weak to
        #     auto-complete a task on — a similarly-titled PR can be genuinely
        #     new work. Only CONFIRMED_MERGE_SIGNALS route here.
        merged = _wa._cached_check_task_already_merged(
            task_id,
            "",
            runner=runner,
            project_root=root,
            timeout=timeout,
            skip_network=False,
        )
        if merged and merged.get("signal") in _wa.CONFIRMED_MERGE_SIGNALS:
            return PRStateVerdict(
                lane=PRLane.COMPLETED,
                pr_number=merged.get("pr_number"),
                pr_url=merged.get("pr_url"),
                signal=merged.get("signal"),
            )

        # (2) OPEN PR -> pr_open. Skippable (include_open=False) so the
        #     per-failure merged short-circuit stays cheap and only TERMINAL
        #     transitions pay the uncached open-search round trip — and so a
        #     first failure with an open-but-doomed PR retries normally
        #     instead of parking (PR 266 review major).
        if not include_open:
            verdict.branch = _git_branch_for_task_id(
                task_id, runner=runner, project_root=root, timeout=timeout
            )
            return verdict
        open_pr = _gh_search_open_pr_for_task_id(
            task_id, runner=runner, project_root=root, timeout=timeout
        )
        if open_pr:
            return PRStateVerdict(
                lane=PRLane.PR_OPEN,
                pr_number=open_pr.get("number"),
                pr_url=open_pr.get("url"),
                signal="gh_open_search",
                branch=open_pr.get("headRefName"),
            )

        # (3) Neither: proceed terminal. Record any matching branch as
        #     diagnostic metadata only — it does NOT change the lane.
        verdict.branch = _git_branch_for_task_id(
            task_id, runner=runner, project_root=root, timeout=timeout
        )
    except Exception:
        return PRStateVerdict(lane=PRLane.NONE)

    return verdict


def route_task_to_pr_open(
    queue_path: Path,
    task: dict,
    *,
    pr_url: str | None = None,
    pr_number: int | None = None,
) -> bool:
    """Move ``task`` into the queue's ``pr_open`` lane atomically.

    Mirrors ``failure_recovery._requeue_task``'s locking + atomic-write pattern
    but targets ``pr_open`` instead of ``pending``. The task_id is removed from
    ``pending`` / ``in_progress`` / ``failed`` / ``blocked`` / ``pr_open`` first
    (so it can never be double-listed or retried), pr_open metadata is stamped,
    and it is appended to ``pr_open``. The caller must NOT already hold the queue
    lock.

    Returns True on success, False on any error (fail-safe: the caller keeps
    today's behavior if the move could not be completed).
    """
    task_id = task.get("task_id")
    if not task_id or not queue_path.exists():
        return False

    try:
        lock_path = queue_path.parent.parent / "runtime" / "queue.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with _wa.QueueLock(lock_path):
            try:
                with open(queue_path, encoding="utf-8") as handle:
                    queue = json.load(handle)
            except (json.JSONDecodeError, OSError):
                return False

            moved = dict(task)
            moved["status"] = "pr_open"
            moved["pr_open_at"] = datetime.now(timezone.utc).isoformat()
            if pr_url:
                moved["pr_url"] = pr_url
            if pr_number is not None:
                moved["pr_number"] = pr_number
            # Clear assignment so the task is not seen as still-claimed.
            moved["claimed_by"] = None
            moved["claimed_at"] = None
            moved["assigned_to"] = None
            moved["assigned_at"] = None

            for section in (
                "pending",
                "in_progress",
                "failed",
                "blocked",
                "pr_open",
            ):
                queue[section] = [
                    entry
                    for entry in queue.get(section, [])
                    if entry.get("task_id") != task_id
                ]
            queue.setdefault("pr_open", []).append(moved)

            fd, tmp = tempfile.mkstemp(
                dir=str(queue_path.parent), prefix=".wq_", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(queue, handle, indent=2)
                os.replace(tmp, str(queue_path))
            except Exception:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                return False
    except Exception as exc:
        logger.debug(
            "[PRReality] route_task_to_pr_open failed for %s: %s", task_id, exc
        )
        return False

    return True
