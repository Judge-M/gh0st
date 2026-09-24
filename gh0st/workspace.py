"""Prepare a private, task-scoped clone before any worker receives a ticket."""

import os
from pathlib import Path
import re
import subprocess
from typing import Optional


_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _git(*args: str, cwd: Optional[Path] = None) -> str:
    environment = os.environ.copy()
    environment.update({
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_LFS_SKIP_SMUDGE": "1", "GIT_OPTIONAL_LOCKS": "0",
    })
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True,
        timeout=30, check=False, env=environment,
    )
    if result.returncode:
        raise ValueError(result.stderr.strip() or "Git operation failed")
    return result.stdout.strip()


class WorkspaceManager:
    def __init__(self, root: Optional[str] = None):
        configured = root or os.getenv("GH0ST_WORKSPACE_ROOT")
        self.root = Path(configured).expanduser().resolve() if configured else (Path.home() / ".gh0st" / "workspaces").resolve()

    def create(self, task_id: str, source_repo: Optional[str] = None) -> tuple[str, Optional[str]]:
        if not _TASK_ID.fullmatch(task_id):
            raise ValueError("Task ID must contain only letters, digits, underscores, or hyphens")
        self.root.mkdir(parents=True, exist_ok=True)
        target = (self.root / task_id).resolve()
        if target.parent != self.root or target.exists():
            raise ValueError("Task workspace already exists or is outside the configured root")

        if source_repo is None:
            target.mkdir()
            _git("init", "-q", str(target))
            _git("-C", str(target), "-c", "user.name=gh0st workspace",
                 "-c", "user.email=workspace@localhost", "commit", "--allow-empty", "-q", "-m", "Initial workspace")
            return str(target), _git("-C", str(target), "rev-parse", "HEAD")

        source = Path(source_repo).expanduser().resolve(strict=True)
        repository_root = Path(_git("-C", str(source), "rev-parse", "--show-toplevel")).resolve()
        if source != repository_root:
            source = repository_root
        source_commit = _git("-C", str(source), "rev-parse", "HEAD")
        if source == target or target.is_relative_to(source):
            raise ValueError("The task workspace cannot be inside the source repository")

        # No object hardlinks, shared Git directory, hooks, or remote credentials.
        _git("clone", "--no-hardlinks", "--no-checkout", "--", str(source), str(target))
        _git("-C", str(target), "remote", "remove", "origin")
        _git("-C", str(target), "-c", "core.hooksPath=/dev/null", "checkout", "--detach", source_commit)
        for key in ("user.name", "user.email"):
            value = subprocess.run(
                ["git", "-C", str(source), "config", "--get", key],
                capture_output=True, text=True, check=False,
            ).stdout.strip()
            if value:
                _git("-C", str(target), "config", "--local", key, value)
        return str(target), source_commit
