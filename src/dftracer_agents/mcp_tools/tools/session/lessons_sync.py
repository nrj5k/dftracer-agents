"""Sync workspace-staged "lessons learned" entries back to the source repo
via a GitHub pull request.

``session_ml_append_lesson`` (in ``annotation_ai.py``) stages new lesson
entries in a workspace-local copy of ``dftracer-annotation-lessons/SKILL.md``.
That staging copy is disconnected from both the installed package and the git
source repo — nothing else ever reads it back in. This module closes that gap
with two MCP tools:

* ``session_lessons_sync_preview`` — diff the staged file against the source
  repo's copy, no side effects.
* ``session_lessons_sync_pr``      — branch, commit, push, and open a PR with
  the new entries. Always requires ``confirm=True`` and a ``GITHUB_TOKEN`` /
  ``GH_TOKEN`` env var — never runs automatically.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import requests
from fastmcp import FastMCP

from ....privacy import anonymize
from .workspace import _ok, _err, _workspaces_root
from .annotation_ai import _LESSONS_REL, _LESSONS_ANCHOR

_GITHUB_REPO = "llnl/dftracer-agents"
_GITHUB_API = f"https://api.github.com/repos/{_GITHUB_REPO}"
_ORIGIN_SSH_URL = f"git@github.com:{_GITHUB_REPO}.git"
_BASE_BRANCH = "main"


# ---------------------------------------------------------------------------
# Repo location
# ---------------------------------------------------------------------------

def _find_local_git_root() -> Optional[Path]:
    """Walk up from this file looking for a ``.git`` directory.

    Mirrors the resolution pattern in ``dftracer_agents.skills.bundled_skills_dir()``.
    Finds the local dev checkout when running from an editable install; returns
    ``None`` for a real (non-editable) production install with no local git tree.
    """
    p = Path(__file__).resolve().parent
    for _ in range(8):
        if (p / ".git").exists():
            return p
        if p.parent == p:
            break
        p = p.parent
    return None


def _clone_fresh() -> Path:
    """Clone the origin repo into a fresh temp directory. Caller must clean up."""
    tmp = Path(tempfile.mkdtemp(prefix="dftracer-agents-lessons-sync-"))
    subprocess.run(
        ["git", "clone", "--depth", "50", _ORIGIN_SSH_URL, str(tmp)],
        check=True, capture_output=True, text=True,
    )
    return tmp


# ---------------------------------------------------------------------------
# Diff logic
# ---------------------------------------------------------------------------

def _split_entries(text: str) -> List[str]:
    """Split the appended-region text (after the anchor) into ``---``-delimited blocks."""
    if _LESSONS_ANCHOR in text:
        _, _, appended = text.partition(_LESSONS_ANCHOR)
    else:
        appended = ""
    blocks = [b.strip() for b in appended.split("---") if b.strip()]
    return blocks


def _entry_context(block: str) -> str:
    for line in block.splitlines():
        if line.strip().startswith("context:"):
            return line.split("context:", 1)[1].strip()
    return ""


def extract_new_entries(staged_text: str, source_text: str) -> List[str]:
    """Return entries present in *staged_text* but not yet in *source_text*.

    Dedup rule matches ``session_ml_append_lesson``'s own check: an entry is
    considered already present if its ``context:`` value is a substring of the
    source file's full text.
    """
    new_entries = []
    for block in _split_entries(staged_text):
        ctx = _entry_context(block)
        if ctx and ctx in source_text:
            continue
        if not ctx and block in source_text:
            continue
        new_entries.append(block)
    return new_entries


# ---------------------------------------------------------------------------
# GitHub REST API — PR creation only (branch/commit/push use plain git)
# ---------------------------------------------------------------------------

def _create_pull_request(token: str, base: str, head: str, title: str, body: str) -> dict:
    resp = requests.post(
        f"{_GITHUB_API}/pulls",
        headers={
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        },
        json={"title": title, "head": head, "base": base, "body": body},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------

def register_lessons_sync_tools(mcp: FastMCP) -> None:
    """Register ``session_lessons_sync_preview`` and ``session_lessons_sync_pr``."""

    @mcp.tool()
    def session_lessons_sync_preview(run_id: str = "") -> str:
        """Preview the lessons that would be synced back to the source repo.

        Diffs the workspace-staged ``dftracer-annotation-lessons/SKILL.md``
        (written by ``session_ml_append_lesson``) against the source repo's
        current copy of the same file. Read-only — no git or network calls.
        Always call this before ``session_lessons_sync_pr`` to review what
        would be added.

        Args:
            run_id: Optional session identifier, used only to locate the
                workspaces root when called from inside a session context.

        Returns:
            JSON with ``status``, ``new_entry_count``, ``new_entries`` (list
            of the raw entry blocks that would be added), and ``source_repo``
            (the local git checkout or clone that would be used).
        """
        ws_root = _workspaces_root()
        staged_path = ws_root / _LESSONS_REL
        if not staged_path.exists():
            return _ok("No staged lessons file found — nothing to sync", new_entry_count=0, new_entries=[])
        # Lessons leave this machine here. Redact identity and machine-local
        # paths at the read boundary so nothing unredacted can reach a PR.
        staged_text = anonymize(staged_path.read_text(errors="replace"))

        repo_root = _find_local_git_root()
        cleanup_dir: Optional[Path] = None
        if repo_root is None:
            repo_root = _clone_fresh()
            cleanup_dir = repo_root
        try:
            source_path = repo_root / "src" / "dftracer_agents" / _LESSONS_REL
            source_text = source_path.read_text(errors="replace") if source_path.exists() else ""
            new_entries = extract_new_entries(staged_text, source_text)
            return _ok(
                f"{len(new_entries)} new lesson entr{'y' if len(new_entries) == 1 else 'ies'} would be synced",
                new_entry_count=len(new_entries),
                new_entries=new_entries,
                source_repo=str(repo_root),
                used_fresh_clone=cleanup_dir is not None,
            )
        finally:
            if cleanup_dir is not None:
                shutil.rmtree(cleanup_dir, ignore_errors=True)

    @mcp.tool()
    def session_lessons_sync_pr(run_id: str = "", confirm: bool = False) -> str:
        """Open a PR against llnl/dftracer-agents with new staged lesson entries.

        Creates a branch, commits the new entries into
        ``src/dftracer_agents/.agents/skills/dftracer-annotation-lessons/SKILL.md``,
        pushes it, and opens a pull request via the GitHub REST API. Operates
        on the local dev checkout if one is found (editable install); clones
        the repo fresh into a temp directory otherwise.

        This is a **visible, hard-to-reverse action against a real shared
        repository** — it never runs automatically. Always call
        ``session_lessons_sync_preview`` first to review the diff, and only
        call this with ``confirm=True`` after the user has approved it.

        Args:
            run_id: Optional session identifier, used only to locate the
                workspaces root when called from inside a session context.
            confirm: Must be explicitly ``True`` — the call is rejected
                otherwise, with zero git/network side effects.

        Returns:
            JSON with ``status``, and on success ``pr_url``, ``branch``,
            ``entry_count``; on failure an explanatory ``message``.
        """
        if not confirm:
            return _err(
                "confirm must be explicitly set to True — call "
                "session_lessons_sync_preview first to review the diff"
            )

        token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
        if not token:
            return _err("GITHUB_TOKEN or GH_TOKEN environment variable is not set")

        ws_root = _workspaces_root()
        staged_path = ws_root / _LESSONS_REL
        if not staged_path.exists():
            return _ok("No staged lessons file found — nothing to sync", entry_count=0)
        # Lessons leave this machine here. Redact identity and machine-local
        # paths at the read boundary so nothing unredacted can reach a PR.
        staged_text = anonymize(staged_path.read_text(errors="replace"))

        repo_root = _find_local_git_root()
        cleanup_dir: Optional[Path] = None
        if repo_root is None:
            repo_root = _clone_fresh()
            cleanup_dir = repo_root

        try:
            if cleanup_dir is None:
                # Operating on the user's own local dev checkout — never sweep
                # unrelated uncommitted work into this commit.
                status = subprocess.run(
                    ["git", "status", "--porcelain"], cwd=repo_root,
                    check=True, capture_output=True, text=True,
                )
                dirty = [
                    line for line in status.stdout.splitlines()
                    if not line.strip().endswith(str(Path("src") / "dftracer_agents" / _LESSONS_REL))
                ]
                if dirty:
                    return _err(
                        "Local dev checkout has unrelated uncommitted changes — "
                        "commit or stash them before syncing lessons, to avoid "
                        "sweeping unrelated work into this PR",
                        dirty_paths=[line[3:] for line in dirty],
                    )

            source_rel = Path("src") / "dftracer_agents" / _LESSONS_REL
            source_path = repo_root / source_rel
            source_text = source_path.read_text(errors="replace") if source_path.exists() else ""
            new_entries = extract_new_entries(staged_text, source_text)
            if not new_entries:
                return _ok("No new lesson entries to sync", entry_count=0)

            appended_block = "\n\n---\n" + "\n---\n".join(new_entries) + "\n"
            if _LESSONS_ANCHOR in source_text:
                new_source_text = source_text.replace(
                    _LESSONS_ANCHOR, _LESSONS_ANCHOR + appended_block, 1
                )
            else:
                new_source_text = source_text + f"\n{_LESSONS_ANCHOR}\n" + appended_block
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_text(new_source_text)

            ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
            branch = f"lessons-sync/{ts}"

            def _git(*args: str) -> None:
                subprocess.run(
                    ["git", *args], cwd=repo_root, check=True,
                    capture_output=True, text=True,
                )

            _git("checkout", "-b", branch)
            _git("add", str(source_rel))
            _git(
                "commit", "-m",
                f"Sync {len(new_entries)} annotation lesson(s) from session {run_id or '(unspecified)'}",
            )
            _git("push", "origin", branch)

            title = f"Sync {len(new_entries)} annotation lesson(s) from a pipeline session"
            body = (
                f"Automated sync of {len(new_entries)} new entr"
                f"{'y' if len(new_entries) == 1 else 'ies'} appended to "
                f"`dftracer-annotation-lessons/SKILL.md` during session `{run_id or '(unspecified)'}`.\n\n"
                "Generated by `session_lessons_sync_pr` — please review before merging."
            )
            pr = _create_pull_request(token, base=_BASE_BRANCH, head=branch, title=title, body=body)

            return _ok(
                f"Opened PR with {len(new_entries)} new lesson entr{'y' if len(new_entries) == 1 else 'ies'}",
                pr_url=pr.get("html_url"),
                branch=branch,
                entry_count=len(new_entries),
            )
        finally:
            if cleanup_dir is not None:
                shutil.rmtree(cleanup_dir, ignore_errors=True)
