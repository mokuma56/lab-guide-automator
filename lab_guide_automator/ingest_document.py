"""
Document ingestion — parses existing lab guides in various formats into LabGuide.

Supported input formats:
  - Markdown (.md)
  - PDF (.pdf)      via pypdf
  - Word (.docx)    via python-docx
  - HTML (.html)    via BeautifulSoup

Pipeline:
  1. Extract raw text from document
  2. Send to AI with a structured extraction prompt
  3. AI returns JSON matching the LabGuide schema
  4. Build and save a LabGuide object
"""
from __future__ import annotations
import json
import re
from pathlib import Path

from lab_guide_automator.config import Settings
from lab_guide_automator.models import (
    LabGuide, LabMetadata, LabSection, LabStep, LearningObjective,
)
from lab_guide_automator import ai_client


# ─────────────────────────────────────────────────────────────
# Text extractors
# ─────────────────────────────────────────────────────────────

def extract_text_from_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError("pypdf is required: uv add pypdf")
    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages.append(text.strip())
    return "\n\n".join(pages)


def extract_text_from_docx(path: Path) -> str:
    try:
        from docx import Document
    except ImportError:
        raise RuntimeError("python-docx is required: uv add python-docx")
    doc = Document(str(path))
    parts = []
    for para in doc.paragraphs:
        if para.text.strip():
            # Preserve heading markers so the AI can detect structure
            if para.style.name.startswith("Heading"):
                level = re.search(r"\d+", para.style.name)
                prefix = "#" * (int(level.group()) if level else 2) + " "
                parts.append(prefix + para.text.strip())
            else:
                parts.append(para.text.strip())
    return "\n\n".join(parts)


def extract_text_from_html(path: Path) -> str:
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        raise RuntimeError("beautifulsoup4 is required: uv add beautifulsoup4")
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="replace"), "html.parser")
    # Remove scripts and styles
    for tag in soup(["script", "style", "nav", "footer", "head"]):
        tag.decompose()
    # Add heading markers
    for tag in soup.find_all(re.compile(r"^h[1-6]$")):
        level = int(tag.name[1])
        tag.insert_before("#" * level + " ")
        tag.insert_after("\n")
    return soup.get_text(separator="\n").strip()


def extract_text_from_markdown(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def extract_text(path: Path) -> str:
    """Auto-detect format and extract text."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return extract_text_from_pdf(path)
    elif suffix == ".docx":
        return extract_text_from_docx(path)
    elif suffix in (".html", ".htm"):
        return extract_text_from_html(path)
    elif suffix in (".md", ".markdown", ".txt"):
        return extract_text_from_markdown(path)
    else:
        # Try plain text fallback
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            raise ValueError(f"Unsupported file format: {suffix}")


# ─────────────────────────────────────────────────────────────
# AI extraction prompt
# ─────────────────────────────────────────────────────────────

_EXTRACT_SYSTEM = """\
You are a technical writer parsing an existing lab guide document.
Extract all structured content and return it as a single JSON object matching this exact schema:

{
  "title": "string",
  "subtitle": "string (or empty)",
  "author": "string (or empty)",
  "version": "string (default '1.0')",
  "difficulty": "beginner|intermediate|advanced",
  "lab_duration_minutes": number,
  "tags": ["tag1", "tag2"],
  "prerequisites": ["prereq1"],
  "introduction": "full introduction text (plain text, no markdown headers)",
  "learning_objectives": [
    {"text": "objective text", "bloom_level": "apply"}
  ],
  "sections": [
    {
      "title": "Section Title",
      "overview": "one-sentence overview",
      "steps": [
        {
          "order": 1,
          "title": "Step title",
          "instruction": "full step instruction text",
          "expected_result": "what the learner should see",
          "notes": "instructor notes if any",
          "code_blocks": ["any CLI commands or code exactly as written"]
        }
      ]
    }
  ],
  "conclusion": "conclusion text (plain text)"
}

Rules:
- Preserve all technical details exactly: IP addresses, hostnames, CLI commands, config values
- If the document has numbered steps, preserve the numbering in 'order'
- If the document has code blocks, CLI commands, or config snippets, put them in code_blocks (one item per block)
- Infer difficulty from language and content complexity if not stated
- Infer duration if not stated (count steps × 3-5 minutes)
- If there are no sections, create one section called "Lab Steps" containing all steps
- Bloom levels: remember | understand | apply | analyze | evaluate | create
- Return ONLY valid JSON, no markdown fences, no explanation
"""


async def extract_structure_with_ai(
    settings: Settings,
    text: str,
    filename: str = "",
    progress_callback=None,
) -> dict:
    """
    Send extracted text to AI and get back structured LabGuide JSON.
    Chunks large documents if needed.
    """
    # Truncate very large documents to fit context (keep first ~60k chars)
    MAX_CHARS = 60_000
    if len(text) > MAX_CHARS:
        if progress_callback:
            progress_callback(f"Document is large ({len(text):,} chars), truncating to first {MAX_CHARS:,} chars for AI processing...")
        text = text[:MAX_CHARS] + "\n\n[... document truncated for processing ...]"

    if progress_callback:
        progress_callback("Sending document to AI for structure extraction...")

    messages = [
        {"role": "system", "content": _EXTRACT_SYSTEM},
        {"role": "user", "content": f"Document filename: {filename}\n\n---\n\n{text}"},
    ]

    raw = await ai_client.chat(settings, messages)

    # Strip any accidental markdown fences
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-z]*\n?", "", raw)
        raw = re.sub(r"\n?```$", "", raw)
    raw = raw.strip()

    return json.loads(raw)


# ─────────────────────────────────────────────────────────────
# Build LabGuide from extracted dict
# ─────────────────────────────────────────────────────────────

def _bloom(level: str) -> str:
    valid = {"remember", "understand", "apply", "analyze", "evaluate", "create"}
    return level if level in valid else "apply"


def build_guide_from_dict(data: dict) -> LabGuide:
    """Convert the AI-extracted dict into a LabGuide object."""
    metadata = LabMetadata(
        title=data.get("title", "Untitled Lab"),
        subtitle=data.get("subtitle", ""),
        author=data.get("author", ""),
        version=str(data.get("version", "1.0")),
        difficulty=data.get("difficulty", "intermediate"),
        lab_duration_minutes=int(data.get("lab_duration_minutes", 60)),
        tags=data.get("tags", []),
        prerequisites=data.get("prerequisites", []),
    )

    objectives = [
        LearningObjective(
            text=o.get("text", ""),
            bloom_level=_bloom(o.get("bloom_level", "apply")),
        )
        for o in data.get("learning_objectives", [])
        if o.get("text")
    ]

    sections = []
    for raw_sec in data.get("sections", []):
        steps = []
        for raw_step in raw_sec.get("steps", []):
            steps.append(LabStep(
                order=int(raw_step.get("order", len(steps) + 1)),
                title=raw_step.get("title", f"Step {len(steps)+1}"),
                instruction=raw_step.get("instruction", ""),
                expected_result=raw_step.get("expected_result", ""),
                notes=raw_step.get("notes", ""),
                code_blocks=raw_step.get("code_blocks", []),
            ))
        sections.append(LabSection(
            title=raw_sec.get("title", "Section"),
            overview=raw_sec.get("overview", ""),
            steps=steps,
        ))

    return LabGuide(
        metadata=metadata,
        introduction=data.get("introduction", ""),
        learning_objectives=objectives,
        sections=sections,
        conclusion=data.get("conclusion", ""),
    )


# ─────────────────────────────────────────────────────────────
# Main entry point
# ─────────────────────────────────────────────────────────────

async def ingest_document(
    settings: Settings,
    document_path: Path,
    session_dir: Path,
    progress_callback=None,
) -> LabGuide:
    """
    Full pipeline: document file → LabGuide.

    Args:
        settings: App settings
        document_path: Path to the existing lab guide document
        session_dir: Where to save the guide JSON
        progress_callback: Optional fn(message) for progress reporting
    """
    if not document_path.exists():
        raise FileNotFoundError(f"Document not found: {document_path}")

    suffix = document_path.suffix.lower()
    fmt_names = {".pdf": "PDF", ".docx": "Word", ".md": "Markdown",
                 ".html": "HTML", ".htm": "HTML", ".txt": "Text"}
    fmt = fmt_names.get(suffix, suffix.upper())

    if progress_callback:
        progress_callback(f"Extracting text from {fmt} document...")

    text = extract_text(document_path)

    if not text.strip():
        raise ValueError("No text could be extracted from the document.")

    if progress_callback:
        progress_callback(f"Extracted {len(text):,} characters of text.")

    data = await extract_structure_with_ai(
        settings, text, document_path.name, progress_callback
    )

    if progress_callback:
        progress_callback("Building LabGuide from extracted structure...")

    guide = build_guide_from_dict(data)

    session_dir.mkdir(parents=True, exist_ok=True)
    guide.save(session_dir / "guide.json")

    if progress_callback:
        progress_callback(
            f"Done — {len(guide.sections)} sections, "
            f"{guide.step_count()} steps, "
            f"{len(guide.learning_objectives)} objectives."
        )

    return guide
