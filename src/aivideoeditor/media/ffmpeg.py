"""Thin wrappers around the ffmpeg / ffprobe executables."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence


class FFmpegError(RuntimeError):
    """Raised when an ffmpeg/ffprobe invocation fails."""

    def __init__(self, cmd: Sequence[str], returncode: int, stderr: str):
        self.cmd = list(cmd)
        self.returncode = returncode
        self.stderr = stderr
        tail = "\n".join(stderr.strip().splitlines()[-15:])
        super().__init__(f"{os.path.basename(cmd[0])} exited with {returncode}:\n{tail}")


def _find(name: str, env_var: str) -> str:
    path = os.environ.get(env_var) or shutil.which(name)
    if not path:
        raise FileNotFoundError(
            f"'{name}' was not found. Install FFmpeg and make sure it is on PATH, "
            f"or set {env_var} to the executable."
        )
    return path


def ffmpeg_bin() -> str:
    return _find("ffmpeg", "AVE_FFMPEG")


def ffprobe_bin() -> str:
    return _find("ffprobe", "AVE_FFPROBE")


def run_ffmpeg(args: Sequence[str], *, capture_stdout: bool = False) -> subprocess.CompletedProcess:
    """Run ffmpeg with the given arguments (without the binary name).

    stderr is always captured so errors can be reported; stdout is captured
    as bytes when ``capture_stdout`` is set (used for raw PCM/frame pipes).
    """
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostdin", "-loglevel", "error", *args]
    proc = subprocess.run(
        cmd,
        stdout=subprocess.PIPE if capture_stdout else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    if proc.returncode != 0:
        raise FFmpegError(cmd, proc.returncode, proc.stderr.decode("utf-8", "replace"))
    return proc
