"""ffmpeg helpers. Uses the ffmpeg binary bundled with imageio-ffmpeg unless FFMPEG_BIN is set."""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import imageio_ffmpeg

FFMPEG = os.environ.get("FFMPEG_BIN") or imageio_ffmpeg.get_ffmpeg_exe()
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


class MediaError(RuntimeError):
    pass


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(
            [FFMPEG, "-hide_banner", *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise MediaError(f"ffmpeg timed out after {timeout}s") from e
    except FileNotFoundError as e:
        raise MediaError(f"ffmpeg not found at {FFMPEG}") from e
    return proc


def probe_duration(path: Path) -> float:
    proc = _run(["-i", str(path)], timeout=60)  # exits non-zero without output; that's expected
    m = _DURATION_RE.search(proc.stderr)
    if not m:
        raise MediaError(f"Could not read video duration. ffmpeg said: {proc.stderr[-400:]}")
    h, mnt, s = m.groups()
    return int(h) * 3600 + int(mnt) * 60 + float(s)


def make_proxy(src: Path, dst: Path, max_width: int = 1280) -> None:
    """H.264 MP4 proxy: plays in every browser, smaller upload to the model, audio stripped."""
    scale = f"scale='trunc(min({max_width},iw)/2)*2':-2"
    proc = _run([
        "-y", "-i", str(src), "-vf", scale,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-an", "-movflags", "+faststart", str(dst),
    ], timeout=60 * 60)
    if proc.returncode != 0 or not dst.exists():
        raise MediaError(f"Video conversion failed: {proc.stderr[-600:]}")


def extract_frame(src: Path, t: float, dst: Path, width: int = 960) -> None:
    proc = _run([
        "-y", "-ss", f"{max(t, 0):.3f}", "-i", str(src), "-frames:v", "1",
        "-vf", f"scale={width}:-2", "-q:v", "3", str(dst),
    ], timeout=60)
    if proc.returncode != 0 or not dst.exists():
        raise MediaError(f"Frame extraction failed at {t:.2f}s: {proc.stderr[-400:]}")


def extract_clip(src: Path, start: float, end: float, dst: Path) -> None:
    dur = max(end - start, 0.1)
    proc = _run([
        "-y", "-ss", f"{max(start, 0):.3f}", "-i", str(src), "-t", f"{dur:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p",
        "-an", "-movflags", "+faststart", str(dst),
    ], timeout=300)
    if proc.returncode != 0 or not dst.exists():
        raise MediaError(f"Clip extraction failed for {start:.2f}-{end:.2f}s: {proc.stderr[-400:]}")
