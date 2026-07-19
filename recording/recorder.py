"""
Recording module — macOS screen capture via ffmpeg / screencapture.

Provides:
  start_recording(session_id, audio=True)  → RecordingSession
  stop_recording(session)                  → path to .mp4
  take_screenshot(session, label)          → path to .png
"""
from __future__ import annotations
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime
import platform


# ─────────────────────────────────────────────────────────────
# Data classes
# ─────────────────────────────────────────────────────────────

@dataclass
class RecordingSession:
    session_id: str
    output_dir: Path
    video_path: Path
    audio: bool
    start_time: float = field(default_factory=time.time)
    _proc: subprocess.Popen | None = None
    screenshots: list[Path] = field(default_factory=list)
    screenshot_times: list[float] = field(default_factory=list)

    @property
    def elapsed(self) -> float:
        return time.time() - self.start_time

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None


# ─────────────────────────────────────────────────────────────
# Platform check
# ─────────────────────────────────────────────────────────────

def _require_macos() -> None:
    if platform.system() != "Darwin":
        raise RuntimeError("Screen recording is only supported on macOS.")

def _ffmpeg_bin() -> str:
    """Return the ffmpeg binary path, checking common install locations."""
    candidates = [
        "ffmpeg",                               # already in PATH
        "/opt/homebrew/bin/ffmpeg",             # Homebrew Apple Silicon
        "/usr/local/bin/ffmpeg",                # Homebrew Intel
        "/usr/bin/ffmpeg",
    ]
    for candidate in candidates:
        try:
            subprocess.run([candidate, "-version"], capture_output=True, check=True)
            return candidate
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    return "ffmpeg"   # will fail with a clear error at call time


def _ffmpeg_available() -> bool:
    try:
        subprocess.run([_ffmpeg_bin(), "-version"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False

def _avfoundation_devices() -> list[str]:
    """Return list of AVFoundation device lines (for debugging)."""
    result = subprocess.run(
        [_ffmpeg_bin(), "-f", "avfoundation", "-list_devices", "true", "-i", "dummy"],
        capture_output=True, text=True
    )
    return (result.stdout + result.stderr).splitlines()


# ─────────────────────────────────────────────────────────────
# Recording control
# ─────────────────────────────────────────────────────────────

def start_recording(
    session_id: str,
    output_dir: Path,
    audio: bool = True,
    fps: int = 15,
    display_index: int = 1,
    audio_device_index: int = 0,
) -> RecordingSession:
    """
    Start a screen recording using ffmpeg + AVFoundation (macOS).
    Returns a RecordingSession with the running subprocess.
    """
    _require_macos()
    if not _ffmpeg_available():
        raise RuntimeError(
            "ffmpeg is not installed. Install with: brew install ffmpeg"
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    video_path = output_dir / f"recording_{timestamp}.mp4"

    cmd: list[str] = [_ffmpeg_bin(), "-y"]

    if audio:
        cmd += [
            "-f", "avfoundation",
            "-framerate", str(fps),
            "-capture_cursor", "1",
            "-i", f"{display_index}:{audio_device_index}",
        ]
    else:
        cmd += [
            "-f", "avfoundation",
            "-framerate", str(fps),
            "-capture_cursor", "1",
            "-i", f"{display_index}",
        ]

    cmd += [
        "-vcodec", "libx264",
        "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        "-crf", "23",
        str(video_path),
    ]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    session = RecordingSession(
        session_id=session_id,
        output_dir=output_dir,
        video_path=video_path,
        audio=audio,
        _proc=proc,
    )
    return session


def stop_recording(session: RecordingSession) -> Path:
    """
    Stop the recording. Returns path to the output .mp4.
    Sends 'q' to ffmpeg stdin to trigger clean shutdown.
    """
    if session._proc is None:
        raise RuntimeError("No recording process attached to this session.")
    if session._proc.poll() is not None:
        # already stopped
        return session.video_path

    try:
        session._proc.stdin.write(b"q\n")
        session._proc.stdin.flush()
        session._proc.wait(timeout=10)
    except Exception:
        session._proc.terminate()
        try:
            session._proc.wait(timeout=5)
        except Exception:
            session._proc.kill()

    return session.video_path


def take_screenshot(
    session: RecordingSession,
    label: str = "",
) -> tuple[Path, int]:
    """
    Capture a single screenshot PNG using macOS `screencapture`.

    The file is named  step-NNN_<timestamp>.png  where NNN is the 1-based
    sequence number of this screenshot within the session.  This lets the
    ingestion pipeline attach screenshots to steps in the correct order
    without any manual labelling.

    Returns (path, sequence_number).
    """
    _require_macos()
    seq = len(session.screenshots) + 1          # 1-based before appending
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    # Build filename:  step-001_20260718_143022_123.png
    # If a custom label is given it is appended for human readability.
    suffix = "_" + re.sub(r"[^\w\-]", "_", label)[:30] if label else ""
    path = session.output_dir / f"step-{seq:03d}{suffix}_{ts}.png"

    subprocess.run(
        ["screencapture", "-x", str(path)],
        check=True,
    )

    session.screenshots.append(path)
    session.screenshot_times.append(session.elapsed)
    return path, seq


# ─────────────────────────────────────────────────────────────
# Frame extraction (for processing existing recordings)
# ─────────────────────────────────────────────────────────────

def extract_frames(
    video_path: Path,
    output_dir: Path,
    every_n_seconds: float = 5.0,
) -> list[Path]:
    """
    Extract one frame every N seconds from a video using ffmpeg.
    Returns list of extracted frame paths.
    """
    if not _ffmpeg_available():
        raise RuntimeError("ffmpeg is required. Install with: brew install ffmpeg")

    output_dir.mkdir(parents=True, exist_ok=True)
    output_pattern = str(output_dir / "frame_%04d.jpg")

    subprocess.run(
        [
            _ffmpeg_bin(), "-y",
            "-i", str(video_path),
            "-vf", f"fps=1/{every_n_seconds}",
            "-q:v", "2",
            output_pattern,
        ],
        check=True,
        capture_output=True,
    )

    frames = sorted(output_dir.glob("frame_*.jpg"))
    return frames


def _ffprobe_bin() -> str:
    """Return ffprobe binary path."""
    for candidate in [
        "ffprobe",
        "/opt/homebrew/bin/ffprobe",
        "/usr/local/bin/ffprobe",
    ]:
        try:
            subprocess.run([candidate, "-version"], capture_output=True, check=True)
            return candidate
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    return "ffprobe"


def get_video_duration(video_path: Path) -> float:
    """Return video duration in seconds using ffprobe."""
    result = subprocess.run(
        [
            _ffprobe_bin(), "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())
