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


def list_audio_devices() -> list[dict]:
    """Return parsed list of AVFoundation audio input devices.

    Returns a list of dicts: [{"index": 0, "name": "MacBook Pro Microphone"}, ...]
    """
    lines = _avfoundation_devices()
    audio_section = False
    devices: list[dict] = []
    for line in lines:
        if "AVFoundation audio devices" in line:
            audio_section = True
            continue
        if audio_section:
            m = re.search(r'\[(\d+)\]\s+(.+)', line)
            if m:
                devices.append({"index": int(m.group(1)), "name": m.group(2).strip()})
    return devices


# ─────────────────────────────────────────────────────────────
# Recording control
# ─────────────────────────────────────────────────────────────

def start_screenshot_session(
    session_id: str,
    output_dir: Path,
) -> RecordingSession:
    """
    Create a screenshot-only session (no ffmpeg process).
    Screenshots are taken manually via take_screenshot().
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    return RecordingSession(
        session_id=session_id,
        output_dir=output_dir,
        video_path=output_dir / "no_video",   # placeholder, never written
        audio=False,
        _proc=None,
    )


def start_audio_session(
    session_id: str,
    output_dir: Path,
    audio_device_index: int = 0,
) -> RecordingSession:
    """
    Audio-only recording session (no screen video).
    Records mic to an .m4a file alongside screenshots.
    Screenshots are taken manually via take_screenshot().
    """
    _require_macos()
    if not _ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed. Install with: brew install ffmpeg")

    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    audio_path = output_dir / f"audio_{timestamp}.m4a"

    cmd = [
        _ffmpeg_bin(), "-y",
        "-f", "avfoundation",
        "-i", f"none:{audio_device_index}",
        "-c:a", "aac", "-b:a", "128k",
        str(audio_path),
    ]

    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    return RecordingSession(
        session_id=session_id,
        output_dir=output_dir,
        video_path=audio_path,   # reuse field — points to .m4a
        audio=True,
        _proc=proc,
    )


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


def stop_recording(session: RecordingSession) -> Path | None:
    """
    Stop the recording. Returns path to the output .mp4, or None for
    screenshot-only sessions (no ffmpeg process).
    Sends 'q' to ffmpeg stdin to trigger clean shutdown.
    """
    if session._proc is None:
        return None   # screenshot-only session — nothing to stop
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


def _applescript_browser_titles(app: str) -> list[str]:
    """
    Return ordered window titles for a running browser via AppleScript.
    Order matches the Quartz window list (front-to-back).
    Returns [] if the app is not running or AppleScript fails.
    """
    # Each script builds a newline-delimited string of the active-tab title
    # per window.  Using linefeed as a delimiter avoids splitting on commas
    # that appear inside page titles.
    scripts: dict[str, str] = {
        "Google Chrome": (
            'tell application "Google Chrome"\n'
            '  set out to ""\n'
            '  repeat with w in windows\n'
            '    set out to out & (title of active tab of w) & linefeed\n'
            '  end repeat\n'
            '  return out\n'
            'end tell'
        ),
        "Safari": (
            'tell application "Safari"\n'
            '  set out to ""\n'
            '  repeat with w in windows\n'
            '    try\n'
            '      set out to out & (name of current tab of w) & linefeed\n'
            '    end try\n'
            '  end repeat\n'
            '  return out\n'
            'end tell'
        ),
        "Firefox": (
            'tell application "Firefox"\n'
            '  set out to ""\n'
            '  repeat with w in windows\n'
            '    try\n'
            '      set out to out & (name of w) & linefeed\n'
            '    end try\n'
            '  end repeat\n'
            '  return out\n'
            'end tell'
        ),
        "Arc": (
            'tell application "Arc"\n'
            '  set out to ""\n'
            '  repeat with w in windows\n'
            '    try\n'
            '      set out to out & (title of w) & linefeed\n'
            '    end try\n'
            '  end repeat\n'
            '  return out\n'
            'end tell'
        ),
        "Brave Browser": (
            'tell application "Brave Browser"\n'
            '  set out to ""\n'
            '  repeat with w in windows\n'
            '    set out to out & (title of active tab of w) & linefeed\n'
            '  end repeat\n'
            '  return out\n'
            'end tell'
        ),
        "Microsoft Edge": (
            'tell application "Microsoft Edge"\n'
            '  set out to ""\n'
            '  repeat with w in windows\n'
            '    set out to out & (title of active tab of w) & linefeed\n'
            '  end repeat\n'
            '  return out\n'
            'end tell'
        ),
    }
    script = scripts.get(app)
    if not script:
        return []
    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=5,
        )
        raw = result.stdout.strip()
        if not raw:
            return []
        return [t for t in raw.splitlines() if t.strip()]
    except Exception:
        return []


def list_browser_windows() -> list[dict]:
    """
    Return a list of visible browser windows with their tab titles.

    Uses Quartz to enumerate CGWindowIDs (needed for ``screencapture -l``)
    and AppleScript to fetch the actual window titles (Chrome/Safari/Firefox
    don't expose titles to Quartz without Screen Recording permission).

    Each entry: {"id": int, "app": str, "title": str}
    """
    try:
        import Quartz  # type: ignore
    except ImportError:
        return []

    BROWSER_APPS = {
        "Google Chrome", "Safari", "Firefox", "Arc",
        "Brave Browser", "Microsoft Edge",
    }

    # Quartz returns windows front-to-back; layer 0 = normal app windows
    options = (
        Quartz.kCGWindowListOptionOnScreenOnly
        | Quartz.kCGWindowListExcludeDesktopElements
    )
    raw = Quartz.CGWindowListCopyWindowInfo(options, Quartz.kCGNullWindowID) or []

    # Group CGWindowIDs per browser app, preserving front-to-back order
    from collections import defaultdict
    app_windows: dict[str, list[int]] = defaultdict(list)
    for w in raw:
        owner = w.get("kCGWindowOwnerName", "")
        if owner not in BROWSER_APPS:
            continue
        if w.get("kCGWindowLayer", 99) != 0:
            continue
        wid = w.get("kCGWindowNumber")
        if wid is not None:
            app_windows[owner].append(int(wid))

    # Fetch titles per app and zip with window IDs
    result: list[dict] = []
    for app, wids in app_windows.items():
        titles = _applescript_browser_titles(app)
        for i, wid in enumerate(wids):
            if i < len(titles):
                title = titles[i]
            else:
                title = f"Window {i + 1}"
            result.append({"id": wid, "app": app, "title": title})

    return result


def take_screenshot(
    session: RecordingSession,
    label: str = "",
    window_id: int | None = None,
) -> tuple[Path, int]:
    """
    Capture a single screenshot PNG using macOS ``screencapture``.

    If *window_id* is given (a CGWindowID from :func:`list_browser_windows`),
    only that window is captured via ``screencapture -l <id>``.
    Requires Screen Recording permission in System Settings → Privacy.

    If the window capture fails (e.g. permission not yet granted) it
    automatically falls back to a full-screen capture so the session is
    never blocked.  A ``_window_capture_failed`` attribute is set on the
    session so the caller can surface a warning.

    Returns (path, sequence_number).
    """
    _require_macos()
    seq = len(session.screenshots) + 1
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
    suffix = "_" + re.sub(r"[^\w\-]", "_", label)[:30] if label else ""
    path = session.output_dir / f"step-{seq:03d}{suffix}_{ts}.png"

    used_window = False
    if window_id is not None:
        cmd = ["screencapture", "-x", "-o", "-l", str(window_id), str(path)]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0 and path.exists() and path.stat().st_size > 0:
            used_window = True
        else:
            # Permission denied or window gone — fall back to full screen
            session._window_capture_failed = True  # type: ignore[attr-defined]
            path.unlink(missing_ok=True)

    if not used_window:
        result = subprocess.run(["screencapture", "-x", str(path)], capture_output=True)
        if result.returncode != 0 or not path.exists() or path.stat().st_size == 0:
            raise RuntimeError(
                "screencapture failed. Grant Screen Recording permission: "
                "System Settings → Privacy & Security → Screen Recording → enable Terminal (or your app)."
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
