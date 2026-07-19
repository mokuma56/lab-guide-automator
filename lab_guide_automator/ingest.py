"""
Ingestion pipeline.

Takes a video (or folder of screenshots) and produces a draft LabGuide:
  1. Extract frames from video at N-second intervals
  2. Describe each frame with vision AI
  3. Cluster frames into logical steps using LLM
  4. Draft section titles, step instructions, and objectives
"""
from __future__ import annotations
import json
from pathlib import Path

from lab_guide_automator.config import Settings
from lab_guide_automator.models import (
    LabGuide, LabMetadata, LabSection, LabStep, LearningObjective, StepScreenshot,
)
from lab_guide_automator import ai_client
from recording.recorder import extract_frames


# ─────────────────────────────────────────────────────────────
# Frame description
# ─────────────────────────────────────────────────────────────

async def describe_frames(
    settings: Settings,
    frames: list[Path],
    lab_context: str = "",
    progress_callback=None,
) -> list[dict]:
    """
    Run vision AI on each frame. Returns list of
    {"frame": Path, "description": str, "timestamp_s": float}
    """
    results = []
    total = len(frames)
    for i, frame in enumerate(frames):
        if progress_callback:
            progress_callback(f"Describing screenshot {i + 1} of {total}: {frame.name}")
        description = await ai_client.describe_screenshot(settings, frame, lab_context)
        # Infer timestamp from filename: frame_0001.jpg → 0, frame_0002.jpg → N, etc.
        try:
            idx = int(frame.stem.split("_")[-1]) - 1
        except ValueError:
            idx = i
        results.append({
            "frame": frame,
            "description": description,
            "index": idx,
        })
    return results


# ─────────────────────────────────────────────────────────────
# Step clustering
# ─────────────────────────────────────────────────────────────

_CLUSTER_SYSTEM = """\
You are a technical writer helping create a lab guide from a screen recording.
You will receive a sequence of frame descriptions from the recording.
Group them into logical lab steps. Each step should represent a meaningful action.

Return a JSON array of steps:
[
  {
    "title": "Short step title",
    "instruction": "Clear instruction text in second person (e.g. 'Navigate to...')",
    "expected_result": "What the learner should see when done",
    "frame_indices": [0, 1, 2]
  }
]

Rules:
- Merge redundant frames that show the same state
- Prefer 5-15 steps per section
- Instructions should be actionable, specific, and include exact values visible in frames
- Use imperative present tense ("Click", "Enter", "Navigate", "Verify")
"""

async def cluster_into_steps(
    settings: Settings,
    frame_descriptions: list[dict],
    section_title: str = "",
) -> list[dict]:
    """Ask the LLM to cluster frame descriptions into lab steps."""
    lines = []
    for d in frame_descriptions:
        lines.append(f"Frame {d['index']}: {d['description']}")
    frames_text = "\n".join(lines)

    context = f"Section: {section_title}\n\n" if section_title else ""
    messages = [
        {"role": "system", "content": _CLUSTER_SYSTEM},
        {"role": "user", "content": context + frames_text},
    ]

    raw = await ai_client.chat(settings, messages)
    # Extract JSON from response (may have markdown fences)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)


# ─────────────────────────────────────────────────────────────
# Section detection
# ─────────────────────────────────────────────────────────────

_SECTION_SYSTEM = """\
You are a technical writer. Given a lab title and list of step titles, propose
logical section groupings. Return JSON:
[
  {"title": "Section Title", "step_indices": [0, 1, 2], "overview": "One sentence overview"},
  ...
]
Keep sections at 3-8 steps each. Prefer 2-5 sections total.
"""

async def detect_sections(
    settings: Settings,
    lab_title: str,
    step_titles: list[str],
) -> list[dict]:
    messages = [
        {"role": "system", "content": _SECTION_SYSTEM},
        {"role": "user", "content": f"Lab: {lab_title}\n\nSteps:\n" + "\n".join(
            f"{i}. {t}" for i, t in enumerate(step_titles)
        )},
    ]
    raw = await ai_client.chat(settings, messages)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)


# ─────────────────────────────────────────────────────────────
# Objectives + intro generation
# ─────────────────────────────────────────────────────────────

_OBJECTIVES_SYSTEM = """\
You are a technical curriculum writer. Given a lab title and its step summaries,
write 3-6 measurable learning objectives using Bloom's taxonomy.
Return JSON:
[{"text": "...", "bloom_level": "apply"}, ...]
Bloom levels: remember | understand | apply | analyze | evaluate | create
"""

_INTRO_SYSTEM = """\
You are a technical writer. Write a professional lab introduction (2-3 paragraphs)
that covers: what the lab is about, the technology or platform being used, and
why it matters. Be engaging but concise. Plain text, no headers.
"""

async def generate_objectives(
    settings: Settings,
    lab_title: str,
    step_summaries: list[str],
) -> list[dict]:
    messages = [
        {"role": "system", "content": _OBJECTIVES_SYSTEM},
        {"role": "user", "content": f"Lab: {lab_title}\n\nSteps:\n" + "\n".join(f"- {s}" for s in step_summaries)},
    ]
    raw = await ai_client.chat(settings, messages)
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw)


async def generate_introduction(
    settings: Settings,
    lab_title: str,
    step_summaries: list[str],
) -> str:
    messages = [
        {"role": "system", "content": _INTRO_SYSTEM},
        {"role": "user", "content": f"Lab title: {lab_title}\nSteps: " + "; ".join(step_summaries[:10])},
    ]
    return await ai_client.chat(settings, messages)


# ─────────────────────────────────────────────────────────────
# Main pipeline
# ─────────────────────────────────────────────────────────────

async def ingest_recording(
    settings: Settings,
    video_path: Path,
    lab_title: str,
    session_dir: Path,
    frame_interval_s: float = 5.0,
    progress_callback=None,
) -> LabGuide:
    """
    Full ingestion pipeline: video → LabGuide draft.
    """
    frames_dir = session_dir / "frames"

    # 1. Extract frames
    if progress_callback:
        progress_callback("Extracting frames from video...")
    frames = extract_frames(video_path, frames_dir, every_n_seconds=frame_interval_s)

    # 2. Describe frames
    if progress_callback:
        progress_callback(f"Describing {len(frames)} frames with vision AI...")
    frame_descs = await describe_frames(settings, frames, lab_title, progress_callback)

    # 3. Cluster into steps
    if progress_callback:
        progress_callback("Clustering frames into steps...")
    raw_steps = await cluster_into_steps(settings, frame_descs, lab_title)

    # 4. Detect sections
    if progress_callback:
        progress_callback("Organizing steps into sections...")
    step_titles = [s["title"] for s in raw_steps]
    raw_sections = await detect_sections(settings, lab_title, step_titles)

    # 5. Build LabSection / LabStep objects
    sections: list[LabSection] = []
    for sec_order, raw_sec in enumerate(raw_sections):
        steps: list[LabStep] = []
        for step_order, si in enumerate(raw_sec.get("step_indices", [])):
            if si >= len(raw_steps):
                continue
            rs = raw_steps[si]
            # Attach screenshots for frames in this step
            screenshots = []
            for fi in rs.get("frame_indices", []):
                if fi < len(frame_descs):
                    fd = frame_descs[fi]
                    screenshots.append(StepScreenshot(
                        path=str(fd["frame"].relative_to(session_dir)),
                        caption=fd["description"][:120],
                    ))
            steps.append(LabStep(
                order=step_order + 1,
                title=rs["title"],
                instruction=rs["instruction"],
                expected_result=rs.get("expected_result", ""),
                screenshots=screenshots,
            ))
        sections.append(LabSection(
            title=raw_sec["title"],
            overview=raw_sec.get("overview", ""),
            steps=steps,
        ))

    # 6. Learning objectives
    if progress_callback:
        progress_callback("Generating learning objectives...")
    raw_objs = await generate_objectives(settings, lab_title, step_titles)
    objectives = [
        LearningObjective(text=o["text"], bloom_level=o.get("bloom_level", "apply"))
        for o in raw_objs
    ]

    # 7. Introduction
    if progress_callback:
        progress_callback("Writing introduction...")
    introduction = await generate_introduction(settings, lab_title, step_titles)

    guide = LabGuide(
        metadata=LabMetadata(title=lab_title),
        introduction=introduction,
        learning_objectives=objectives,
        sections=sections,
        recording_path=str(video_path.relative_to(session_dir.parent)),
    )
    guide.save(session_dir / "guide.json")
    return guide


async def ingest_screenshots(
    settings: Settings,
    screenshots: list[Path],
    lab_title: str,
    session_dir: Path,
    progress_callback=None,
) -> LabGuide:
    """
    Ingestion from a folder of screenshots instead of a video.
    """
    frame_descs = await describe_frames(settings, screenshots, lab_title, progress_callback)
    raw_steps = await cluster_into_steps(settings, frame_descs, lab_title)
    step_titles = [s["title"] for s in raw_steps]
    raw_sections = await detect_sections(settings, lab_title, step_titles)

    sections: list[LabSection] = []
    for raw_sec in raw_sections:
        steps = []
        for step_order, si in enumerate(raw_sec.get("step_indices", [])):
            if si >= len(raw_steps):
                continue
            rs = raw_steps[si]
            screenshots_for_step = []
            for fi in rs.get("frame_indices", []):
                if fi < len(frame_descs):
                    fd = frame_descs[fi]
                    screenshots_for_step.append(StepScreenshot(
                        path=str(fd["frame"].relative_to(session_dir)),
                        caption=fd["description"][:120],
                    ))
            steps.append(LabStep(
                order=step_order + 1,
                title=rs["title"],
                instruction=rs["instruction"],
                expected_result=rs.get("expected_result", ""),
                screenshots=screenshots_for_step,
            ))
        sections.append(LabSection(
            title=raw_sec["title"],
            overview=raw_sec.get("overview", ""),
            steps=steps,
        ))

    raw_objs = await generate_objectives(settings, lab_title, step_titles)
    objectives = [
        LearningObjective(text=o["text"], bloom_level=o.get("bloom_level", "apply"))
        for o in raw_objs
    ]
    introduction = await generate_introduction(settings, lab_title, step_titles)

    guide = LabGuide(
        metadata=LabMetadata(title=lab_title),
        introduction=introduction,
        learning_objectives=objectives,
        sections=sections,
    )
    guide.save(session_dir / "guide.json")
    return guide
