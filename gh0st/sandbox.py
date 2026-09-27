"""Linux-only local command runner with a single writable task clone."""

from dataclasses import dataclass
import os
from pathlib import Path
import platform
import shutil
import signal
import subprocess
import tempfile
import time


@dataclass(frozen=True)
class CommandResult:
    command: str
    purpose: str
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    truncated: bool = False

    def as_dict(self) -> dict:
        return {
            "command": self.command, "purpose": self.purpose,
            "exit_code": self.exit_code, "stdout": self.stdout,
            "stderr": self.stderr, "duration_seconds": self.duration_seconds,
            "timed_out": self.timed_out, "truncated": self.truncated,
        }


class LinuxSandbox:
    """Run shell commands without host home, source checkout, or network access.

    The controller and model gateway stay outside this process namespace. No
    gateway credential or Docker socket is passed into the command environment.
    """

    def __init__(self, workspace_path: str, workspace_root: str,
                 timeout_seconds: int = 90, output_limit_bytes: int = 1024 * 1024,
                 runtime_path: str | None = None):
        if platform.system() != "Linux":
            raise RuntimeError("Local execution requires Linux and bubblewrap")
        self.workspace = Path(workspace_path).resolve(strict=True)
        root = Path(workspace_root).resolve(strict=True)
        if self.workspace.parent != root or not self.workspace.is_dir():
            raise ValueError("Ticket workspace is outside the configured task root")
        self.bwrap = shutil.which("bwrap")
        if not self.bwrap:
            raise RuntimeError("bubblewrap (bwrap) is required for local execution")
        self.timeout_seconds = timeout_seconds
        self.output_limit_bytes = output_limit_bytes
        configured_runtime = runtime_path or os.getenv("GH0ST_SANDBOX_RUNTIME")
        self.runtime = Path(configured_runtime).resolve(strict=True) if configured_runtime else None
        if self.runtime and (not self.runtime.is_dir() or self.runtime == self.workspace):
            raise ValueError("Sandbox runtime must be a separate, read-only directory")

    def _command(self, shell_command: str) -> list[str]:
        args = [
            self.bwrap, "--die-with-parent", "--cap-drop", "ALL",
            "--unshare-user", "--unshare-pid",
            "--unshare-net", "--unshare-ipc", "--unshare-uts",
            "--clearenv", "--setenv", "PATH", "/runtime/bin:/usr/local/bin:/usr/bin:/bin" if self.runtime else "/usr/local/bin:/usr/bin:/bin",
            "--setenv", "HOME", "/workspace", "--setenv", "TMPDIR", "/tmp",
            "--setenv", "GIT_CONFIG_NOSYSTEM", "1",
            "--setenv", "GIT_CONFIG_GLOBAL", "/dev/null",
        ]
        for system_path in ("/usr", "/bin", "/lib", "/lib64"):
            if Path(system_path).exists():
                args.extend(("--ro-bind", system_path, system_path))
        if self.runtime:
            args.extend(("--ro-bind", str(self.runtime), "/runtime"))
        args.extend((
            "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
            "--bind", str(self.workspace), "/workspace", "--chdir", "/workspace",
            "--", "/bin/sh", "-lc", shell_command,
        ))
        return args

    @staticmethod
    def _limits() -> None:
        import resource
        resource.setrlimit(resource.RLIMIT_CPU, (120, 120))
        resource.setrlimit(resource.RLIMIT_AS, (2 * 1024 * 1024 * 1024, 2 * 1024 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_FSIZE, (64 * 1024 * 1024, 64 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
        process_hard_limit = resource.getrlimit(resource.RLIMIT_NPROC)[1]
        process_limit = (128 if process_hard_limit == resource.RLIM_INFINITY
                         else min(128, process_hard_limit))
        resource.setrlimit(resource.RLIMIT_NPROC, (process_limit, process_limit))

    def run(self, shell_command: str, purpose: str = "other") -> CommandResult:
        if not shell_command.strip() or len(shell_command) > 16384:
            raise ValueError("Command must be nonempty and at most 16384 characters")
        started = time.monotonic()
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            process = subprocess.Popen(
                self._command(shell_command), stdin=subprocess.DEVNULL,
                stdout=out, stderr=err, start_new_session=True,
                preexec_fn=self._limits,
            )
            timed_out = False
            try:
                process.wait(timeout=self.timeout_seconds)
            except subprocess.TimeoutExpired:
                timed_out = True
                os.killpg(process.pid, signal.SIGKILL)
                process.wait()
            out.seek(0)
            err.seek(0)
            stdout = out.read(self.output_limit_bytes + 1)
            stderr = err.read(self.output_limit_bytes + 1)
        truncated = len(stdout) > self.output_limit_bytes or len(stderr) > self.output_limit_bytes
        if len(stdout) > self.output_limit_bytes:
            stdout = stdout[:self.output_limit_bytes] + b"\n[output truncated]"
        if len(stderr) > self.output_limit_bytes:
            stderr = stderr[:self.output_limit_bytes] + b"\n[output truncated]"
        return CommandResult(
            command=shell_command, purpose=purpose,
            exit_code=process.returncode if not timed_out else 124,
            stdout=stdout.decode("utf-8", errors="replace"),
            stderr=stderr.decode("utf-8", errors="replace"),
            duration_seconds=round(time.monotonic() - started, 3),
            timed_out=timed_out,
            truncated=truncated,
        )
