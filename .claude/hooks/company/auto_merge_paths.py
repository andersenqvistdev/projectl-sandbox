#!/usr/bin/env python3
"""Single source of truth for auto-merge path holds (PR 257/260, 2026-07-18).

Every auto-merge arming point — pr_output_manager.enable_github_auto_merge,
forge_daemon._check_security_gates, the legacy arming branch, and the CI
Auto-Merge job — must agree on which changed files hold a daemon PR for
operator review. That agreement is only guaranteed if they share ONE seed
list and ONE matching function, which is this module's entire job. Keep it
dependency-free: the CI job fetches this file from the BASE ref (origin/main)
and imports it standalone, so a PR editing this module cannot weaken the
gate that judges that same PR.

reviewPaths semantics: a daemon PR touching any matching file must NOT arm
GitHub auto-merge. It stays OPEN with normal CI and the
``awaiting-operator-review`` label; the operator merges manually after
review. This is a hold tier, not a block — distinct from blockedPaths
(exact-match files that must never auto-merge at all).
"""

from __future__ import annotations

import fnmatch

# The self-governance surface: gates, judges, assessors, arming code, the
# daemon core, and the shipped config template. Conventions (*audit*,
# *judge*, *admission*, *steering*) cover renames and new files in those
# families without list edits. This module protects itself (a PR editing it
# is by definition gate work). The operator's forge-config.json can override
# via autonomy.autoMerge.reviewPaths; an ABSENT key means THIS list, never
# "no holds".
DEFAULT_REVIEW_PATHS: list[str] = [
    ".claude/hooks/company/*audit*",
    ".claude/hooks/company/*judge*",
    ".claude/hooks/company/*admission*",
    ".claude/hooks/company/*steering*",
    ".claude/hooks/company/goal_tracker.py",
    ".claude/hooks/company/board_governance.py",
    ".claude/hooks/company/pr_output_manager.py",
    ".claude/hooks/company/forge_daemon.py",
    ".claude/hooks/company/employee_activator.py",
    ".claude/hooks/company/operation_loop.py",
    ".claude/hooks/company/failure_recovery.py",
    ".claude/hooks/company/infra_failure.py",
    ".claude/hooks/company/auto_merge_paths.py",
    ".github/workflows/*.yml",
    ".github/workflows/*.yaml",
    ".claude/forge-config.json",
]

REVIEW_HOLD_LABEL = "awaiting-operator-review"


def resolve_review_paths(configured: object) -> list[str]:
    """Resolve the effective reviewPaths from a config value.

    - ``None`` (key absent) → the built-in default seed.
    - A list of strings → that list (operator override; an explicit ``[]``
      is honored as "operator disabled holds" — visible in config review,
      unlike a silent code fallback).
    - Anything else (malformed) → the default seed. Malformed config must
      never fail open into "no holds" on this surface.
    """
    if configured is None:
        return list(DEFAULT_REVIEW_PATHS)
    if isinstance(configured, list) and all(isinstance(p, str) for p in configured):
        return list(configured)
    return list(DEFAULT_REVIEW_PATHS)


def _normalize(path: str) -> str:
    return path[2:] if path.startswith("./") else path


def match_review_paths(
    changed_files: list[str], patterns: list[str] | None = None
) -> list[str]:
    """Changed files that match a reviewPaths glob (posix paths, fnmatch)."""
    pats = DEFAULT_REVIEW_PATHS if patterns is None else patterns
    return [
        f for f in changed_files if any(fnmatch.fnmatch(_normalize(f), p) for p in pats)
    ]
