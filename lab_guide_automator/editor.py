"""
Agentic editing tools.
These functions are called by the MCP server when the agent or user
wants to refine a LabGuide.
"""
from __future__ import annotations
import json
from lab_guide_automator.config import Settings
from lab_guide_automator.models import (
    LabGuide, LabSection, LabStep, LearningObjective,
)
from lab_guide_automator import ai_client


# ─────────────────────────────────────────────────────────────
# Section / Step editors
# ─────────────────────────────────────────────────────────────

async def rewrite_step(
    settings: Settings,
    guide: LabGuide,
    step_id: str,
    feedback: str,
) -> LabStep:
    """
    Rewrite a single step's instruction based on user feedback.
    """
    step = guide.get_step(step_id)
    if not step:
        raise ValueError(f"Step {step_id!r} not found in guide.")

    messages = [
        {"role": "system", "content": (
            "You are a technical writer improving a lab guide step. "
            "Return only a JSON object with key: instruction. "
            "Keep the same step title. Be specific, use imperative tense."
        )},
        {"role": "user", "content": (
            f"Current instruction:\n{step.instruction}\n\n"
            f"Feedback / change request:\n{feedback}"
        )},
    ]
    raw = await ai_client.chat(settings, messages)
    raw = raw.strip().lstrip("```json").rstrip("```").strip()
    data = json.loads(raw)
    step.instruction = data.get("instruction", step.instruction)
    guide.touch()
    return step


async def rewrite_section_overview(
    settings: Settings,
    guide: LabGuide,
    section_id: str,
    feedback: str,
) -> LabSection:
    section = guide.get_section(section_id)
    if not section:
        raise ValueError(f"Section {section_id!r} not found.")

    messages = [
        {"role": "system", "content": (
            "You are a technical writer. Rewrite a lab section overview. "
            "Return only the new overview text (plain text, 1-3 sentences)."
        )},
        {"role": "user", "content": (
            f"Section title: {section.title}\n"
            f"Current overview: {section.overview}\n\n"
            f"Feedback: {feedback}"
        )},
    ]
    section.overview = await ai_client.chat(settings, messages)
    guide.touch()
    return section


async def add_learning_objective(
    settings: Settings,
    guide: LabGuide,
    description: str,
) -> LearningObjective:
    """
    Ask the AI to formalize a learning objective from a plain description.
    """
    messages = [
        {"role": "system", "content": (
            "You are a curriculum writer. Convert the user's description into "
            "a single measurable learning objective. "
            "Return JSON: {\"text\": \"...\", \"bloom_level\": \"apply\"}"
        )},
        {"role": "user", "content": description},
    ]
    raw = await ai_client.chat(settings, messages)
    raw = raw.strip().lstrip("```json").rstrip("```").strip()
    data = json.loads(raw)
    obj = LearningObjective(
        text=data.get("text", description),
        bloom_level=data.get("bloom_level", "apply"),
    )
    guide.learning_objectives.append(obj)
    guide.touch()
    return obj


async def rewrite_introduction(
    settings: Settings,
    guide: LabGuide,
    feedback: str,
) -> str:
    messages = [
        {"role": "system", "content": (
            "You are a technical writer. Rewrite the lab introduction. "
            "Return only the new introduction text (2-3 paragraphs, no markdown headers)."
        )},
        {"role": "user", "content": (
            f"Lab title: {guide.metadata.title}\n"
            f"Current introduction:\n{guide.introduction}\n\n"
            f"Feedback: {feedback}"
        )},
    ]
    guide.introduction = await ai_client.chat(settings, messages)
    guide.touch()
    return guide.introduction


async def rewrite_conclusion(
    settings: Settings,
    guide: LabGuide,
    feedback: str,
) -> str:
    messages = [
        {"role": "system", "content": (
            "You are a technical writer. Rewrite the lab conclusion. "
            "Summarize what was accomplished and suggest next steps. "
            "Return only the new conclusion text (1-2 paragraphs)."
        )},
        {"role": "user", "content": (
            f"Lab title: {guide.metadata.title}\n"
            f"Steps covered: {', '.join(s.title for sec in guide.sections for s in sec.steps)}\n"
            f"Current conclusion:\n{guide.conclusion}\n\n"
            f"Feedback: {feedback}"
        )},
    ]
    guide.conclusion = await ai_client.chat(settings, messages)
    guide.touch()
    return guide.conclusion


async def add_step(
    settings: Settings,
    guide: LabGuide,
    section_id: str,
    title: str,
    description: str,
    insert_after_step_id: str | None = None,
) -> LabStep:
    """
    Ask AI to draft a new step and insert it into the specified section.
    """
    section = guide.get_section(section_id)
    if not section:
        raise ValueError(f"Section {section_id!r} not found.")

    messages = [
        {"role": "system", "content": (
            "You are a technical writer. Draft a lab step. "
            "Return JSON: {\"instruction\": \"...\"}"
        )},
        {"role": "user", "content": (
            f"Step title: {title}\n"
            f"Description of what should happen: {description}\n"
            f"Lab context: {guide.metadata.title}"
        )},
    ]
    raw = await ai_client.chat(settings, messages)
    raw = raw.strip().lstrip("```json").rstrip("```").strip()
    data = json.loads(raw)

    new_step = LabStep(
        order=len(section.steps) + 1,
        title=title,
        instruction=data.get("instruction", description),
        expected_result="",
    )

    if insert_after_step_id:
        idx = next((i for i, s in enumerate(section.steps) if s.id == insert_after_step_id), None)
        if idx is not None:
            section.steps.insert(idx + 1, new_step)
            # Renumber
            for i, s in enumerate(section.steps):
                s.order = i + 1
        else:
            section.steps.append(new_step)
    else:
        section.steps.append(new_step)

    guide.touch()
    return new_step


async def apply_suggestion(
    settings: Settings,
    guide: LabGuide,
    suggestion: str,
) -> LabGuide:
    """
    Apply a single improvement suggestion to the guide using AI.

    The AI receives the full guide as JSON and the suggestion text,
    and returns an updated guide JSON.  We merge changes back into
    the existing LabGuide object so all IDs / metadata are preserved.
    """
    import copy

    guide_json = guide.model_dump_json(indent=2)

    messages = [
        {"role": "system", "content": (
            "You are a senior technical curriculum editor. "
            "You will receive a lab guide in JSON format and ONE improvement suggestion. "
            "Apply the suggestion to the guide — rewrite steps, sections, intro, conclusion, "
            "or learning objectives as needed. "
            "Return the COMPLETE updated guide JSON with the same schema. "
            "Do NOT change IDs, metadata.id, or metadata.created_at. "
            "Preserve any fields you are not modifying. "
            "Return only valid JSON — no markdown fences, no explanation."
        )},
        {"role": "user", "content": (
            f"Suggestion to apply:\n{suggestion}\n\n"
            f"Current guide JSON:\n{guide_json}"
        )},
    ]

    raw = await ai_client.chat(settings, messages)
    raw = raw.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
    updated = LabGuide.model_validate_json(raw)
    # Preserve immutable identity fields (they live on LabGuide, not LabMetadata)
    updated.id = guide.id
    return updated


async def suggest_improvements(
    settings: Settings,
    guide: LabGuide,
) -> list[str]:
    """
    Ask AI to review the whole guide and suggest improvements.
    Returns a list of suggestion strings.
    """
    step_text = "\n".join(
        f"- [{sec.title}] {step.title}: {step.instruction[:80]}"
        for sec in guide.sections
        for step in sec.steps
    )
    obj_text = "\n".join(f"- {o.text}" for o in guide.learning_objectives)

    messages = [
        {"role": "system", "content": (
            "You are a senior technical curriculum reviewer. "
            "Review the lab guide outline and return a JSON array of improvement suggestions. "
            "Each item should be a specific, actionable suggestion string. "
            "Focus on: missing steps, unclear instructions, alignment with objectives, "
            "format consistency, and learner experience. "
            "Return: [\"suggestion 1\", \"suggestion 2\", ...]"
        )},
        {"role": "user", "content": (
            f"Lab: {guide.metadata.title}\n\n"
            f"Objectives:\n{obj_text}\n\n"
            f"Steps:\n{step_text}"
        )},
    ]
    raw = await ai_client.chat(settings, messages)
    raw = raw.strip().lstrip("```json").rstrip("```").strip()
    return json.loads(raw)
