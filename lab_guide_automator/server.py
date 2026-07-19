"""
Lab Guide Automator — MCP Server

Exposes all lab guide operations as MCP tools usable from OpenCode or any MCP client.

Tools:
  Recording:
    - start_recording
    - stop_recording
    - take_screenshot
    - list_sessions

  Ingestion:
    - ingest_video
    - ingest_screenshots

  Viewing / Navigation:
    - get_guide_summary
    - get_section
    - get_step
    - list_guides

  Editing:
    - rewrite_step
    - rewrite_section_overview
    - add_learning_objective
    - add_step
    - rewrite_introduction
    - rewrite_conclusion
    - update_metadata
    - suggest_improvements

  Export:
    - export_markdown
    - export_pdf
    - export_html
    - export_docx
    - export_mkdocs
    - export_narrated_video
"""
from __future__ import annotations
import asyncio
import json
import uuid
from pathlib import Path
from typing import Optional

from fastmcp import FastMCP
from lab_guide_automator.config import Settings
from lab_guide_automator.models import (
    LabGuide, LabMetadata, LearningObjective, LabSection, LabStep,
)
import lab_guide_automator.editor as editor
import lab_guide_automator.ingest as ingest
from export.exporter import (
    export_markdown, export_pdf, export_html, export_docx,
    export_mkdocs, export_narrated_video, push_mkdocs_to_git,
)
from recording.recorder import (
    RecordingSession, start_recording, stop_recording, take_screenshot,
)

# ─────────────────────────────────────────────────────────────
# Globals
# ─────────────────────────────────────────────────────────────

settings = Settings()
mcp = FastMCP("Lab Guide Automator")

# In-memory state (sessions survive as long as the server is running)
_active_sessions: dict[str, RecordingSession] = {}
_loaded_guides: dict[str, LabGuide] = {}   # guide_id → LabGuide


def _data_dir() -> Path:
    d = settings.data_dir
    if not d.is_absolute():
        d = Path(__file__).parent / d
    d.mkdir(parents=True, exist_ok=True)
    return d


def _session_dir(session_id: str) -> Path:
    return _data_dir() / "sessions" / session_id


def _guide_path(guide_id: str) -> Path:
    return _data_dir() / "guides" / f"{guide_id}.json"


def _load_guide(guide_id: str) -> LabGuide:
    if guide_id in _loaded_guides:
        return _loaded_guides[guide_id]
    path = _guide_path(guide_id)
    if not path.exists():
        raise ValueError(f"Guide {guide_id!r} not found at {path}")
    guide = LabGuide.load(path)
    _loaded_guides[guide_id] = guide
    return guide


def _save_guide(guide: LabGuide) -> None:
    _loaded_guides[guide.id] = guide
    guide.save(_guide_path(guide.id))


# ─────────────────────────────────────────────────────────────
# Recording tools
# ─────────────────────────────────────────────────────────────

@mcp.tool()
def start_screen_recording(
    session_id: Optional[str] = None,
    audio: bool = True,
    fps: int = 15,
) -> dict:
    """
    Start recording your screen (macOS only).
    Returns a session_id to use with stop_recording and take_screenshot.

    Args:
        session_id: Optional custom session ID (auto-generated if omitted)
        audio: Whether to capture microphone audio
        fps: Frames per second (default 15 for reasonable file size)
    """
    sid = session_id or str(uuid.uuid4())[:8]
    out_dir = _session_dir(sid) / "recording"
    session = start_recording(sid, out_dir, audio=audio, fps=fps)
    _active_sessions[sid] = session
    return {
        "session_id": sid,
        "status": "recording",
        "audio": audio,
        "output_dir": str(out_dir),
    }


@mcp.tool()
def stop_screen_recording(session_id: str) -> dict:
    """
    Stop an active screen recording.
    Returns the path to the saved .mp4 file.

    Args:
        session_id: Session ID returned by start_screen_recording
    """
    session = _active_sessions.get(session_id)
    if not session:
        raise ValueError(f"No active recording session: {session_id!r}")
    video_path = stop_recording(session)
    del _active_sessions[session_id]
    return {
        "session_id": session_id,
        "status": "stopped",
        "video_path": str(video_path),
        "duration_s": round(session.elapsed, 1),
        "screenshots": len(session.screenshots),
    }


@mcp.tool()
def capture_screenshot(
    session_id: str,
    label: str = "",
) -> dict:
    """
    Capture a screenshot during an active recording session.
    Screenshots are automatically attached to lab steps during ingestion.

    Args:
        session_id: Active recording session ID
        label: Optional descriptive label for the screenshot
    """
    session = _active_sessions.get(session_id)
    if not session:
        raise ValueError(f"No active recording session: {session_id!r}")
    path = take_screenshot(session, label)
    return {
        "path": str(path),
        "label": label,
        "elapsed_s": round(session.elapsed, 1),
        "total_screenshots": len(session.screenshots),
    }


@mcp.tool()
def list_recording_sessions() -> list[dict]:
    """List all active (in-progress) recording sessions."""
    return [
        {
            "session_id": sid,
            "elapsed_s": round(s.elapsed, 1),
            "screenshots": len(s.screenshots),
            "is_running": s.is_running(),
        }
        for sid, s in _active_sessions.items()
    ]


# ─────────────────────────────────────────────────────────────
# Ingestion tools
# ─────────────────────────────────────────────────────────────

@mcp.tool()
async def ingest_video(
    video_path: str,
    lab_title: str,
    session_id: Optional[str] = None,
    frame_interval_seconds: float = 5.0,
) -> dict:
    """
    Process a screen recording video into a draft LabGuide using AI vision.
    Extracts frames, describes each with vision AI, clusters into steps,
    generates sections, learning objectives, and introduction automatically.

    Args:
        video_path: Absolute path to the .mp4 recording
        lab_title: Title for the lab guide
        session_id: Optional session ID for organizing output files
        frame_interval_seconds: How often to extract a frame (lower = more detail, more AI calls)
    """
    vp = Path(video_path)
    if not vp.exists():
        raise FileNotFoundError(f"Video not found: {video_path}")

    sid = session_id or str(uuid.uuid4())[:8]
    session_dir = _session_dir(sid)

    guide = await ingest.ingest_recording(
        settings, vp, lab_title, session_dir,
        frame_interval_s=frame_interval_seconds,
    )
    _save_guide(guide)

    return {
        "guide_id": guide.id,
        "session_id": sid,
        "title": guide.metadata.title,
        "sections": len(guide.sections),
        "steps": guide.step_count(),
        "objectives": len(guide.learning_objectives),
        "guide_path": str(_guide_path(guide.id)),
        "summary": guide.summary(),
    }


@mcp.tool()
async def ingest_screenshot_folder(
    folder_path: str,
    lab_title: str,
    session_id: Optional[str] = None,
) -> dict:
    """
    Process a folder of screenshots into a draft LabGuide.
    Useful when you have annotated screenshots instead of a video.

    Args:
        folder_path: Path to folder containing .png or .jpg screenshots
        lab_title: Title for the lab guide
        session_id: Optional session ID
    """
    folder = Path(folder_path)
    if not folder.is_dir():
        raise NotADirectoryError(f"Not a directory: {folder_path}")

    screenshots = sorted(
        list(folder.glob("*.png")) + list(folder.glob("*.jpg")) + list(folder.glob("*.jpeg"))
    )
    if not screenshots:
        raise ValueError(f"No screenshots found in {folder_path}")

    sid = session_id or str(uuid.uuid4())[:8]
    session_dir = _session_dir(sid)

    guide = await ingest.ingest_screenshots(settings, screenshots, lab_title, session_dir)
    _save_guide(guide)

    return {
        "guide_id": guide.id,
        "session_id": sid,
        "title": guide.metadata.title,
        "sections": len(guide.sections),
        "steps": guide.step_count(),
        "screenshots_processed": len(screenshots),
    }


# ─────────────────────────────────────────────────────────────
# Guide management / navigation tools
# ─────────────────────────────────────────────────────────────

@mcp.tool()
def list_guides() -> list[dict]:
    """List all saved lab guides."""
    guides_dir = _data_dir() / "guides"
    guides_dir.mkdir(exist_ok=True)
    result = []
    for p in sorted(guides_dir.glob("*.json")):
        try:
            g = LabGuide.load(p)
            result.append({
                "guide_id": g.id,
                "title": g.metadata.title,
                "version": g.metadata.version,
                "sections": len(g.sections),
                "steps": g.step_count(),
                "updated_at": g.updated_at,
            })
        except Exception:
            pass
    return result


@mcp.tool()
def get_guide_summary(guide_id: str) -> dict:
    """
    Get a full structural summary of a LabGuide.

    Args:
        guide_id: The guide ID from list_guides or ingest_video
    """
    guide = _load_guide(guide_id)
    return {
        "guide_id": guide.id,
        "metadata": guide.metadata.model_dump(),
        "introduction_preview": guide.introduction[:300] + "..." if len(guide.introduction) > 300 else guide.introduction,
        "learning_objectives": [{"id": o.id, "text": o.text, "bloom_level": o.bloom_level} for o in guide.learning_objectives],
        "sections": [
            {
                "id": sec.id,
                "title": sec.title,
                "overview": sec.overview,
                "steps": [
                    {"id": s.id, "order": s.order, "title": s.title}
                    for s in sec.steps
                ],
            }
            for sec in guide.sections
        ],
        "conclusion_preview": guide.conclusion[:200] if guide.conclusion else "",
        "updated_at": guide.updated_at,
    }


@mcp.tool()
def get_step_detail(guide_id: str, step_id: str) -> dict:
    """
    Get the full content of a specific step.

    Args:
        guide_id: Guide ID
        step_id: Step ID from get_guide_summary
    """
    guide = _load_guide(guide_id)
    step = guide.get_step(step_id)
    if not step:
        raise ValueError(f"Step {step_id!r} not found.")
    return step.model_dump()


# ─────────────────────────────────────────────────────────────
# Editing tools
# ─────────────────────────────────────────────────────────────

@mcp.tool()
async def rewrite_step_instruction(
    guide_id: str,
    step_id: str,
    feedback: str,
) -> dict:
    """
    Rewrite a lab step's instruction and expected result based on your feedback.
    The AI will improve the step while preserving its title and screenshots.

    Args:
        guide_id: Guide ID
        step_id: Step ID to rewrite
        feedback: What to change (e.g. "Make it more specific, include the exact CLI command shown")
    """
    guide = _load_guide(guide_id)
    step = await editor.rewrite_step(settings, guide, step_id, feedback)
    _save_guide(guide)
    return {
        "step_id": step.id,
        "title": step.title,
        "instruction": step.instruction,
        "expected_result": step.expected_result,
    }


@mcp.tool()
async def rewrite_section_overview_text(
    guide_id: str,
    section_id: str,
    feedback: str,
) -> dict:
    """
    Rewrite a section's overview paragraph.

    Args:
        guide_id: Guide ID
        section_id: Section ID
        feedback: Change instructions
    """
    guide = _load_guide(guide_id)
    sec = await editor.rewrite_section_overview(settings, guide, section_id, feedback)
    _save_guide(guide)
    return {"section_id": sec.id, "title": sec.title, "overview": sec.overview}


@mcp.tool()
async def add_learning_objective(
    guide_id: str,
    description: str,
) -> dict:
    """
    Add a new learning objective. Describe it in plain English and the AI will
    formalize it using Bloom's taxonomy.

    Args:
        guide_id: Guide ID
        description: Plain English description (e.g. "Students should be able to configure OSPF")
    """
    guide = _load_guide(guide_id)
    obj = await editor.add_learning_objective(settings, guide, description)
    _save_guide(guide)
    return {"id": obj.id, "text": obj.text, "bloom_level": obj.bloom_level}


@mcp.tool()
async def rewrite_introduction_text(
    guide_id: str,
    feedback: str,
) -> dict:
    """
    Rewrite the lab introduction.

    Args:
        guide_id: Guide ID
        feedback: Change instructions
    """
    guide = _load_guide(guide_id)
    text = await editor.rewrite_introduction(settings, guide, feedback)
    _save_guide(guide)
    return {"introduction": text}


@mcp.tool()
async def rewrite_conclusion_text(
    guide_id: str,
    feedback: str,
) -> dict:
    """
    Rewrite the lab conclusion.

    Args:
        guide_id: Guide ID
        feedback: Change instructions
    """
    guide = _load_guide(guide_id)
    text = await editor.rewrite_conclusion(settings, guide, feedback)
    _save_guide(guide)
    return {"conclusion": text}


@mcp.tool()
async def add_step_to_section(
    guide_id: str,
    section_id: str,
    step_title: str,
    step_description: str,
    insert_after_step_id: Optional[str] = None,
) -> dict:
    """
    Add a new step to a section. The AI drafts the instruction and expected result.

    Args:
        guide_id: Guide ID
        section_id: Section to add the step to
        step_title: Short title for the new step
        step_description: What the step should cover (plain description)
        insert_after_step_id: Optional — insert after this step ID (appends if omitted)
    """
    guide = _load_guide(guide_id)
    step = await editor.add_step(
        settings, guide, section_id, step_title, step_description, insert_after_step_id
    )
    _save_guide(guide)
    return step.model_dump()


@mcp.tool()
def update_guide_metadata(
    guide_id: str,
    title: Optional[str] = None,
    subtitle: Optional[str] = None,
    version: Optional[str] = None,
    author: Optional[str] = None,
    lab_duration_minutes: Optional[int] = None,
    difficulty: Optional[str] = None,
    tags: Optional[list[str]] = None,
    prerequisites: Optional[list[str]] = None,
) -> dict:
    """
    Update lab guide metadata fields.

    Args:
        guide_id: Guide ID
        title: New title (optional)
        author: Author name
        version: Version string e.g. "1.1"
        difficulty: beginner | intermediate | advanced
        lab_duration_minutes: Estimated duration
        tags: List of topic tags
        prerequisites: List of prerequisite knowledge items
    """
    guide = _load_guide(guide_id)
    m = guide.metadata
    if title is not None: m.title = title
    if subtitle is not None: m.subtitle = subtitle
    if version is not None: m.version = version
    if author is not None: m.author = author
    if lab_duration_minutes is not None: m.lab_duration_minutes = lab_duration_minutes
    if difficulty is not None: m.difficulty = difficulty  # type: ignore
    if tags is not None: m.tags = tags
    if prerequisites is not None: m.prerequisites = prerequisites
    guide.touch()
    _save_guide(guide)
    return m.model_dump()


@mcp.tool()
async def suggest_guide_improvements(guide_id: str) -> list[str]:
    """
    Ask the AI to review the entire guide and suggest improvements.
    Returns a list of specific, actionable suggestions.

    Args:
        guide_id: Guide ID
    """
    guide = _load_guide(guide_id)
    return await editor.suggest_improvements(settings, guide)


# ─────────────────────────────────────────────────────────────
# Export tools
# ─────────────────────────────────────────────────────────────

@mcp.tool()
def export_guide_markdown(guide_id: str, output_path: Optional[str] = None) -> dict:
    """
    Export the lab guide as a Markdown file.

    Args:
        guide_id: Guide ID
        output_path: Where to save (defaults to data/exports/<guide_id>.md)
    """
    guide = _load_guide(guide_id)
    out = Path(output_path) if output_path else _data_dir() / "exports" / f"{guide_id}.md"
    export_markdown(guide, out)
    return {"path": str(out), "format": "markdown"}


@mcp.tool()
def export_guide_pdf(guide_id: str, output_path: Optional[str] = None) -> dict:
    """
    Export the lab guide as a PDF (with embedded screenshots).

    Args:
        guide_id: Guide ID
        output_path: Where to save (defaults to data/exports/<guide_id>.pdf)
    """
    guide = _load_guide(guide_id)
    out = Path(output_path) if output_path else _data_dir() / "exports" / f"{guide_id}.pdf"
    export_pdf(guide, out)
    return {"path": str(out), "format": "pdf"}


@mcp.tool()
def export_guide_html(
    guide_id: str,
    output_path: Optional[str] = None,
    embed_images: bool = True,
) -> dict:
    """
    Export the lab guide as HTML (Moodle-compatible, self-contained).

    Args:
        guide_id: Guide ID
        output_path: Where to save
        embed_images: Base64-embed screenshots so the file is fully self-contained
    """
    guide = _load_guide(guide_id)
    out = Path(output_path) if output_path else _data_dir() / "exports" / f"{guide_id}.html"
    export_html(guide, out, embed_screenshots=embed_images)
    return {"path": str(out), "format": "html", "embedded_images": embed_images}


@mcp.tool()
def export_guide_docx(guide_id: str, output_path: Optional[str] = None) -> dict:
    """
    Export the lab guide as a Microsoft Word (.docx) document.

    Args:
        guide_id: Guide ID
        output_path: Where to save
    """
    guide = _load_guide(guide_id)
    out = Path(output_path) if output_path else _data_dir() / "exports" / f"{guide_id}.docx"
    export_docx(guide, out)
    return {"path": str(out), "format": "docx"}


@mcp.tool()
def export_guide_mkdocs(
    guide_id: str,
    output_dir: Optional[str] = None,
    push_to_git: bool = False,
) -> dict:
    """
    Generate a complete MkDocs Material site from the lab guide.
    Optionally push to git remote configured in MKDOCS_REPO_URL env var.

    Args:
        guide_id: Guide ID
        output_dir: Where to generate the site (defaults to data/exports/<guide_id>-mkdocs)
        push_to_git: Whether to git commit and push to MKDOCS_REPO_URL
    """
    guide = _load_guide(guide_id)
    out = Path(output_dir) if output_dir else _data_dir() / "exports" / f"{guide_id}-mkdocs"
    export_mkdocs(guide, out)

    result = {"path": str(out), "format": "mkdocs", "pushed": False}

    if push_to_git and settings.mkdocs_repo_url:
        msg = push_mkdocs_to_git(out, settings.mkdocs_repo_url, settings.mkdocs_branch)
        result["pushed"] = True
        result["git_message"] = msg

    return result


@mcp.tool()
async def export_guide_narrated_video(
    guide_id: str,
    recording_path: Optional[str] = None,
    output_path: Optional[str] = None,
) -> dict:
    """
    Generate a narrated video by combining the original recording with
    AI-generated text-to-speech narration of the lab steps.

    Args:
        guide_id: Guide ID
        recording_path: Path to source .mp4 (uses guide's recording_path if omitted)
        output_path: Where to save the output .mp4
    """
    guide = _load_guide(guide_id)
    session_dir = _data_dir() / "sessions" / guide_id

    if recording_path:
        rec = Path(recording_path)
    elif guide.recording_path:
        rec = session_dir / guide.recording_path
    else:
        raise ValueError("No recording path available. Provide recording_path argument.")

    out = Path(output_path) if output_path else _data_dir() / "exports" / f"{guide_id}_narrated.mp4"
    result_path = await export_narrated_video(settings, guide, rec, out, session_dir)
    return {"path": str(result_path), "format": "narrated_video"}


# ─────────────────────────────────────────────────────────────
# Utility
# ─────────────────────────────────────────────────────────────

@mcp.tool()
def create_blank_guide(
    title: str,
    author: str = "",
    difficulty: str = "intermediate",
    duration_minutes: int = 60,
) -> dict:
    """
    Create a new blank LabGuide that you can populate manually with add_step and editing tools.

    Args:
        title: Lab guide title
        author: Author name
        difficulty: beginner | intermediate | advanced
        duration_minutes: Estimated duration
    """
    guide = LabGuide(
        metadata=LabMetadata(
            title=title,
            author=author,
            difficulty=difficulty,  # type: ignore
            lab_duration_minutes=duration_minutes,
        )
    )
    _save_guide(guide)
    return {"guide_id": guide.id, "title": title}


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
