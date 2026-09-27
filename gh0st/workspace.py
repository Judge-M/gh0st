"""Prepare a private, task-scoped clone before any worker receives a ticket."""

import os
from pathlib import Path
import re
import shutil
import struct
import subprocess
import zlib
from typing import Optional


_TASK_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg",
                ".jpeg": "image/jpeg", ".webp": "image/webp"}
MAX_IMAGE_COUNT = 8
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def image_header_matches(media_type: str, header: bytes) -> bool:
    if media_type == "image/png":
        return header.startswith(b"\x89PNG\r\n\x1a\n")
    if media_type == "image/jpeg":
        return header.startswith(b"\xff\xd8\xff")
    if media_type == "image/webp":
        return len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP"
    return False


def _png_dimensions(data: bytes) -> tuple[int, int]:
    signature = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(signature):
        raise ValueError("Image contents do not match the declared media type")
    offset = len(signature)
    while offset + 12 <= len(data):
        size = int.from_bytes(data[offset:offset + 4], "big")
        end = offset + 12 + size
        if end > len(data):
            raise ValueError("PNG image has a truncated chunk")
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:offset + 8 + size]
        checksum = int.from_bytes(data[offset + 8 + size:end], "big")
        if zlib.crc32(kind + payload) & 0xFFFFFFFF != checksum:
            raise ValueError("PNG image has an invalid chunk checksum")
        if kind == b"IHDR":
            if size != 13:
                raise ValueError("PNG IHDR chunk must contain 13 bytes")
            width, height = struct.unpack(">II", payload[:8])
            if not width or not height or width * height > 100_000_000:
                raise ValueError("PNG dimensions are outside the supported limit")
            return width, height
        if kind in {b"IDAT", b"IEND"}:
            raise ValueError("PNG image is missing its IHDR chunk")
        offset = end
    raise ValueError("PNG image is missing a complete IHDR chunk")


def conservative_image_tokens(media_type: str, data: bytes) -> int:
    """Upper bound image dimensions at 512-pixel tiles, plus protocol overhead."""
    if not image_header_matches(media_type, data[:32]):
        raise ValueError("Image contents do not match the declared media type")
    width = height = 0
    if media_type == "image/png":
        width, height = _png_dimensions(data)
    elif media_type == "image/jpeg":
        index = 2
        while index + 4 <= len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            index += 2
            if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                continue
            if index + 2 > len(data):
                break
            length = int.from_bytes(data[index:index + 2], "big")
            if length < 2 or index + length > len(data):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF} and length >= 7:
                height, width = struct.unpack(">HH", data[index + 3:index + 7])
                break
            index += length
    elif media_type == "image/webp" and len(data) >= 30:
        if int.from_bytes(data[4:8], "little") + 8 > len(data):
            raise ValueError("WebP image is truncated")
        chunk = data[12:16]
        if chunk == b"VP8X":
            width = int.from_bytes(data[24:27], "little") + 1
            height = int.from_bytes(data[27:30], "little") + 1
        elif chunk == b"VP8 " and len(data) >= 30 and data[23:26] == b"\x9d\x01\x2a":
            width = int.from_bytes(data[26:28], "little") & 0x3FFF
            height = int.from_bytes(data[28:30], "little") & 0x3FFF
        elif chunk == b"VP8L" and len(data) >= 25 and data[20] == 0x2F:
            bits = int.from_bytes(data[21:25], "little")
            width = (bits & 0x3FFF) + 1
            height = ((bits >> 14) & 0x3FFF) + 1
    if width <= 0 or height <= 0:
        raise ValueError("Image dimensions could not be validated")
    tiles = ((width + 511) // 512) * ((height + 511) // 512)
    return tiles * 1024 + 512


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
        for remote in _git("-C", str(target), "remote").splitlines():
            if remote:
                _git("-C", str(target), "remote", "remove", remote)
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
            media_type = _IMAGE_TYPES.get(source.suffix.lower())
            if media_type is None:
                raise ValueError(f"Unsupported image type for {image_path}; use PNG, JPEG, or WebP")
            size = source.stat().st_size
            if size <= 0 or size > MAX_IMAGE_BYTES:
                raise ValueError(f"Each image must be between 1 byte and {MAX_IMAGE_BYTES} bytes")
            with source.open("rb") as stream:
                header = stream.read(12)
            if not image_header_matches(media_type, header):
                raise ValueError(f"Image contents do not match the {media_type} file extension")
            content = source.read_bytes()
            if conservative_image_tokens(media_type, content) > 131_072:
                raise ValueError("Image dimensions exceed the supported input limit")
            total_bytes += size
            if total_bytes > MAX_IMAGE_BYTES:
                raise ValueError(f"Attached images may not exceed {MAX_IMAGE_BYTES} bytes in total")
            prepared.append({"source": str(source), "media_type": media_type, "size": size,
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
                size = int(item["size"])
                if source.stat().st_size != size or size > MAX_IMAGE_BYTES:
                    raise ValueError("Image input changed after validation")
                extension = ".jpg" if media_type == "image/jpeg" else ".png" if media_type == "image/png" else ".webp"
                destination = task_inputs / f"image_{index:02d}{extension}"
                with source.open("rb") as source_stream, destination.open("xb") as target_stream:
                    shutil.copyfileobj(source_stream, target_stream)
                copied = destination.read_bytes()
                if (len(copied) != size or not image_header_matches(media_type, copied[:32])
                        or conservative_image_tokens(media_type, copied) > 131_072):
                    raise ValueError("Image input changed during task staging")
                try:
                    os.chmod(destination, 0o600)
                except OSError:
                    pass
                records.append({"path": str(destination), "media_type": media_type,
                                "filename": str(item["filename"]), "size": size,
                                "reserved_tokens": conservative_image_tokens(media_type, copied)})
        except Exception:
            shutil.rmtree(task_inputs)
            raise
        return records
