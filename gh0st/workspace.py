"""Prepare a private, task-scoped clone before any worker receives a ticket."""

import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Optional


_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_IMAGE_TYPES = {
    ".png": ("image/png", b"\x89PNG\r\n\x1a\n"),
    ".jpg": ("image/jpeg", b"\xff\xd8\xff"),
    ".jpeg": ("image/jpeg", b"\xff\xd8\xff"),
    ".webp": ("image/webp", b"RIFF"),
}
MAX_IMAGE_COUNT = 8
MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_TOKEN_RESERVE = 4096


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

    @staticmethod
    def validate_image_inputs(image_paths: list[str]) -> list[dict[str, object]]:
        if len(image_paths) > MAX_IMAGE_COUNT:
            raise ValueError(f"At most {MAX_IMAGE_COUNT} images can be attached to one task")
        prepared: list[dict[str, object]] = []
        total_bytes = 0
        for image_path in image_paths:
            source = Path(image_path).expanduser().resolve(strict=True)
            if not source.is_file():
                raise ValueError(f"Image input is not a regular file: {image_path}")
            media = _IMAGE_TYPES.get(source.suffix.lower())
            if media is None:
                raise ValueError(f"Unsupported image type for {image_path}; use PNG, JPEG, or WebP")
            size = source.stat().st_size
            if size <= 0 or size > MAX_IMAGE_BYTES:
                raise ValueError(f"Each image must be between 1 byte and {MAX_IMAGE_BYTES} bytes")
            with source.open("rb") as stream:
                header = stream.read(12)
            if not header.startswith(media[1]) or (media[0] == "image/webp" and header[8:12] != b"WEBP"):
                raise ValueError(f"Image contents do not match the {media[0]} file extension")
            total_bytes += size
            if total_bytes > MAX_IMAGE_BYTES:
                raise ValueError(f"Attached images may not exceed {MAX_IMAGE_BYTES} bytes in total")
            prepared.append({"source": str(source), "media_type": media[0], "size": size,
                             "filename": source.name})
        return prepared

    def store_image_inputs(self, task_id: str, workspace_path: str,
                           prepared: list[dict[str, object]]) -> list[dict[str, object]]:
        if not _TASK_ID.fullmatch(task_id):
            raise ValueError("Task ID must contain only letters, digits, underscores, or hyphens")
        workspace = Path(workspace_path).resolve(strict=True)
        if workspace.parent != self.root or workspace.name != task_id:
            raise ValueError("Image inputs require this task's workspace")
        if not prepared:
            return []
        inputs_root = self.root / ".gh0st-inputs"
        if inputs_root.is_symlink():
            raise ValueError("Task image input directory must not be a symlink")
        inputs_root.mkdir(mode=0o700, exist_ok=True)
        if inputs_root.resolve() != self.root / ".gh0st-inputs":
            raise ValueError("Task image input directory is outside the workspace root")
        task_inputs = inputs_root / task_id
        task_inputs.mkdir(mode=0o700, exist_ok=False)
        records = []
        try:
            for index, item in enumerate(prepared, start=1):
                source = Path(str(item["source"])).resolve(strict=True)
                media_type = str(item["media_type"])
                extension = ".jpg" if media_type == "image/jpeg" else ".png" if media_type == "image/png" else ".webp"
                destination = task_inputs / f"image_{index:02d}{extension}"
                with source.open("rb") as source_stream, destination.open("xb") as target_stream:
                    shutil.copyfileobj(source_stream, target_stream)
                try:
                    os.chmod(destination, 0o600)
                except OSError:
                    pass
                records.append({"path": str(destination), "media_type": media_type,
                                "filename": str(item["filename"]), "size": int(item["size"])})
        except Exception:
            shutil.rmtree(task_inputs)
            raise
        return records
