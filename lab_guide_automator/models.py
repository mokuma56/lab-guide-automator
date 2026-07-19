"""
Lab Guide data model.
A LabGuide is the central document object that flows through every stage:
  record → ingest → draft → edit → export
"""
from __future__ import annotations
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal, Optional
from pydantic import BaseModel, Field


# ─────────────────────────────────────────────────────────────
# Sub-models
# ─────────────────────────────────────────────────────────────

class LearningObjective(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    text: str
    bloom_level: Literal[
        "remember", "understand", "apply", "analyze", "evaluate", "create"
    ] = "apply"


class StepScreenshot(BaseModel):
    """Reference to a screenshot file captured during recording."""
    path: str          # relative to session data dir
    timestamp_s: float = 0.0
    caption: str = ""


class LabStep(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    order: int
    title: str
    instruction: str       # full markdown instruction text
    expected_result: str = ""
    notes: str = ""        # instructor / proctor notes
    screenshots: list[StepScreenshot] = []
    code_blocks: list[str] = []
    verified: bool = False


class ContentBlock(BaseModel):
    """An ordered content block inside a section — either text or a screenshot."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    type: Literal["text", "screenshot"] = "text"
    content: str = ""    # markdown text (type=text)
    path: str = ""       # e.g. screenshots/foo.png (type=screenshot)
    caption: str = ""    # alt/caption (type=screenshot)


class LabSection(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    title: str
    overview: str = ""
    screenshots: list[StepScreenshot] = []   # legacy — superseded by blocks
    blocks: list[ContentBlock] = []           # ordered text+screenshot blocks
    steps: list[LabStep] = []


class LabMetadata(BaseModel):
    title: str
    subtitle: str = ""
    version: str = "1.0"
    author: str = ""
    date: str = Field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))
    lab_duration_minutes: int = 60
    difficulty: Literal["beginner", "intermediate", "advanced"] = "intermediate"
    tags: list[str] = []
    prerequisites: list[str] = []


# ─────────────────────────────────────────────────────────────
# Root model
# ─────────────────────────────────────────────────────────────

class LabGuide(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    metadata: LabMetadata
    introduction: str = ""
    learning_objectives: list[LearningObjective] = []
    sections: list[LabSection] = []
    conclusion: str = ""
    topology_image: Optional[str] = None   # relative path
    recording_path: Optional[str] = None   # original .mp4
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat())

    # ── GitHub publishing (per-guide) ─────────────────────────
    github_repo: str = ""      # e.g. https://github.com/ciscodocs/tedcn24-01-fy27.git
    github_branch: str = "main"
    last_published: Optional[str] = None   # ISO timestamp of last successful push

    # ── Persistence helpers ──────────────────────────────────

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.model_dump_json(indent=2))
        self.updated_at = datetime.utcnow().isoformat()

    @classmethod
    def load(cls, path: Path) -> "LabGuide":
        return cls.model_validate_json(path.read_text())

    # ── Convenience mutators ─────────────────────────────────

    def touch(self) -> None:
        self.updated_at = datetime.utcnow().isoformat()

    def get_section(self, section_id: str) -> Optional[LabSection]:
        return next((s for s in self.sections if s.id == section_id), None)

    def get_step(self, step_id: str) -> Optional[LabStep]:
        for sec in self.sections:
            for step in sec.steps:
                if step.id == step_id:
                    return step
        return None

    def all_steps(self) -> list[LabStep]:
        return [step for sec in self.sections for step in sec.steps]

    def step_count(self) -> int:
        return sum(len(s.steps) for s in self.sections)

    def summary(self) -> str:
        return (
            f"**{self.metadata.title}** v{self.metadata.version}\n"
            f"{len(self.sections)} sections · {self.step_count()} steps · "
            f"{len(self.learning_objectives)} objectives\n"
            f"Last updated: {self.updated_at}"
        )
