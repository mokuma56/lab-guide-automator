"""
Export pipeline — renders a LabGuide to multiple output formats.

Supported:
  - Markdown (.md)
  - PDF (via weasyprint)
  - DOCX (via python-docx)
  - HTML (Moodle-compatible)
  - MkDocs site (generate docs/ tree + mkdocs.yml, optional git push)
  - Narrated video (TTS + ffmpeg overlay)
"""
from __future__ import annotations
import subprocess
from pathlib import Path
from datetime import datetime

from lab_guide_automator.models import LabGuide, LabSection, LabStep


def _ffmpeg_bin() -> str:
    for candidate in ["ffmpeg", "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]:
        try:
            subprocess.run([candidate, "-version"], capture_output=True, check=True)
            return candidate
        except (FileNotFoundError, subprocess.CalledProcessError):
            continue
    return "ffmpeg"


# ─────────────────────────────────────────────────────────────
# Markdown renderer (shared base for all text exports)
# ─────────────────────────────────────────────────────────────

def render_markdown(guide: LabGuide, include_screenshots: bool = True) -> str:
    lines: list[str] = []
    m = guide.metadata

    lines += [
        f"# {m.title}",
        "",
        f"**Version:** {m.version}  |  "
        f"**Author:** {m.author or 'Unknown'}  |  "
        f"**Date:** {m.date}  |  "
        f"**Difficulty:** {m.difficulty.capitalize()}  |  "
        f"**Duration:** {m.lab_duration_minutes} min",
        "",
    ]

    if m.tags:
        lines += [f"**Tags:** {', '.join(m.tags)}", ""]
    if m.prerequisites:
        lines += ["## Prerequisites", ""]
        for p in m.prerequisites:
            lines += [f"- {p}"]
        lines += [""]

    if guide.introduction:
        lines += ["## Introduction", "", guide.introduction, ""]

    if guide.learning_objectives:
        lines += ["## Learning Objectives", ""]
        for obj in guide.learning_objectives:
            lines += [f"- {obj.text}"]
        lines += [""]

    for sec in guide.sections:
        lines += [f"## {sec.title}", ""]
        if sec.overview:
            lines += [sec.overview, ""]
        for step in sec.steps:
            lines += [f"### Step {step.order}: {step.title}", ""]
            lines += [step.instruction, ""]
            if step.code_blocks:
                for cb in step.code_blocks:
                    lines += ["```", cb, "```", ""]
            if include_screenshots and step.screenshots:
                for ss in step.screenshots:
                    caption = ss.caption or step.title
                    lines += [f"![{caption}]({ss.path})", ""]
            if step.expected_result:
                lines += [f"> **Expected Result:** {step.expected_result}", ""]
            if step.notes:
                lines += [f"!!! note", f"    {step.notes}", ""]

    if guide.conclusion:
        lines += ["## Conclusion", "", guide.conclusion, ""]

    return "\n".join(lines)


def export_markdown(guide: LabGuide, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_markdown(guide))
    return output_path


# ─────────────────────────────────────────────────────────────
# HTML export (Moodle-compatible)
# ─────────────────────────────────────────────────────────────

_HTML_CSS = """
body { font-family: 'Segoe UI', Arial, sans-serif; max-width: 900px; margin: 0 auto; padding: 2rem; color: #1a1a1a; }
h1 { color: #005073; border-bottom: 3px solid #00bceb; padding-bottom: .5rem; }
h2 { color: #005073; margin-top: 2rem; }
h3 { color: #1f7a8c; }
.meta { color: #555; font-size: .9rem; margin-bottom: 1.5rem; }
.tag { background: #e8f7fc; color: #005073; border-radius: 4px; padding: 2px 8px; font-size: .8rem; }
.objective li { margin: .4rem 0; }
.step { border-left: 4px solid #00bceb; padding: 0 1rem; margin: 1rem 0; }
.expected { background: #f0faf0; border-left: 4px solid #4caf50; padding: .5rem 1rem; border-radius: 4px; }
.note { background: #fff8e1; border-left: 4px solid #ffc107; padding: .5rem 1rem; border-radius: 4px; }
code, pre { background: #f4f4f4; border-radius: 4px; padding: .2rem .4rem; font-family: monospace; }
pre { padding: 1rem; overflow-x: auto; }
img { max-width: 100%; border: 1px solid #ddd; border-radius: 4px; margin: .5rem 0; }
"""

def render_html(guide: LabGuide, embed_screenshots: bool = False) -> str:
    import base64
    m = guide.metadata
    tags_html = " ".join(f'<span class="tag">{t}</span>' for t in m.tags)

    parts = [f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{m.title}</title>
<style>{_HTML_CSS}</style>
</head>
<body>
<h1>{m.title}</h1>
<div class="meta">
  Version {m.version} &nbsp;|&nbsp; {m.author or 'Unknown'} &nbsp;|&nbsp; {m.date}
  &nbsp;|&nbsp; {m.difficulty.capitalize()} &nbsp;|&nbsp; {m.lab_duration_minutes} min
  <br>{tags_html}
</div>"""]

    if m.prerequisites:
        items = "".join(f"<li>{p}</li>" for p in m.prerequisites)
        parts.append(f"<h2>Prerequisites</h2><ul>{items}</ul>")

    if guide.introduction:
        intro_html = guide.introduction.replace("\n\n", "</p><p>")
        parts.append(f"<h2>Introduction</h2><p>{intro_html}</p>")

    if guide.learning_objectives:
        items = "".join(f"<li>{o.text}</li>" for o in guide.learning_objectives)
        parts.append(f'<h2>Learning Objectives</h2><ul class="objective">{items}</ul>')

    for sec in guide.sections:
        parts.append(f"<h2>{sec.title}</h2>")
        if sec.overview:
            parts.append(f"<p>{sec.overview}</p>")
        for step in sec.steps:
            parts.append(f'<div class="step">')
            parts.append(f"<h3>Step {step.order}: {step.title}</h3>")
            instr_html = step.instruction.replace("\n\n", "</p><p>").replace("\n", "<br>")
            parts.append(f"<p>{instr_html}</p>")
            for cb in step.code_blocks:
                parts.append(f"<pre><code>{cb}</code></pre>")
            for ss in step.screenshots:
                if embed_screenshots:
                    try:
                        img_path = Path(ss.path)
                        if img_path.exists():
                            b64 = base64.b64encode(img_path.read_bytes()).decode()
                            ext = img_path.suffix.lstrip(".")
                            src = f"data:image/{ext};base64,{b64}"
                        else:
                            src = ss.path
                    except Exception:
                        src = ss.path
                else:
                    src = ss.path
                parts.append(f'<img src="{src}" alt="{ss.caption}">')
            if step.expected_result:
                parts.append(f'<div class="expected"><strong>Expected Result:</strong> {step.expected_result}</div>')
            if step.notes:
                parts.append(f'<div class="note"><strong>Note:</strong> {step.notes}</div>')
            parts.append("</div>")

    if guide.conclusion:
        conc_html = guide.conclusion.replace("\n\n", "</p><p>")
        parts.append(f"<h2>Conclusion</h2><p>{conc_html}</p>")

    parts.append("</body></html>")
    return "\n".join(parts)


def export_html(guide: LabGuide, output_path: Path, embed_screenshots: bool = False) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(render_html(guide, embed_screenshots=embed_screenshots))
    return output_path


# ─────────────────────────────────────────────────────────────
# PDF export (weasyprint)
# ─────────────────────────────────────────────────────────────

def export_pdf(guide: LabGuide, output_path: Path) -> Path:
    try:
        from weasyprint import HTML
    except ImportError:
        raise RuntimeError("weasyprint is required: pip install weasyprint")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    html_content = render_html(guide, embed_screenshots=True)
    HTML(string=html_content).write_pdf(str(output_path))
    return output_path


# ─────────────────────────────────────────────────────────────
# DOCX export (python-docx)
# ─────────────────────────────────────────────────────────────

def export_docx(guide: LabGuide, output_path: Path) -> Path:
    try:
        from docx import Document
        from docx.shared import Pt, RGBColor, Inches
        from docx.enum.text import WD_ALIGN_PARAGRAPH
    except ImportError:
        raise RuntimeError("python-docx is required: pip install python-docx")

    doc = Document()
    m = guide.metadata

    # Title
    title_para = doc.add_heading(m.title, 0)
    meta_para = doc.add_paragraph(
        f"Version {m.version}  |  {m.author or 'Unknown'}  |  {m.date}  |  "
        f"{m.difficulty.capitalize()}  |  {m.lab_duration_minutes} min"
    )
    meta_para.style = doc.styles["Caption"]

    if m.prerequisites:
        doc.add_heading("Prerequisites", 2)
        for p in m.prerequisites:
            doc.add_paragraph(p, style="List Bullet")

    if guide.introduction:
        doc.add_heading("Introduction", 1)
        doc.add_paragraph(guide.introduction)

    if guide.learning_objectives:
        doc.add_heading("Learning Objectives", 1)
        for obj in guide.learning_objectives:
            doc.add_paragraph(obj.text, style="List Bullet")

    for sec in guide.sections:
        doc.add_heading(sec.title, 1)
        if sec.overview:
            doc.add_paragraph(sec.overview)
        for step in sec.steps:
            doc.add_heading(f"Step {step.order}: {step.title}", 2)
            doc.add_paragraph(step.instruction)
            for cb in step.code_blocks:
                p = doc.add_paragraph(cb)
                p.style = doc.styles.get("Code") or doc.styles["Normal"]
            for ss in step.screenshots:
                img_path = Path(ss.path)
                if img_path.exists():
                    try:
                        doc.add_picture(str(img_path), width=Inches(5.5))
                    except Exception:
                        pass
            if step.expected_result:
                p = doc.add_paragraph()
                p.add_run("Expected Result: ").bold = True
                p.add_run(step.expected_result)
            if step.notes:
                p = doc.add_paragraph()
                p.add_run("Note: ").bold = True
                p.add_run(step.notes)

    if guide.conclusion:
        doc.add_heading("Conclusion", 1)
        doc.add_paragraph(guide.conclusion)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(output_path))
    return output_path


# ─────────────────────────────────────────────────────────────
# MkDocs site
# ─────────────────────────────────────────────────────────────

_MKDOCS_YML_TEMPLATE = """\
---
dev_addr: "0.0.0.0:8000"
site_name: "{title}"
site_description: "{title}"
site_author: "Cisco"
copyright: Copyright &copy; 2024 Cisco
theme:
  name: material
  features:
    - navigation.tabs
    - navigation.tabs.sticky
    - navigation.indexes
    - navigation.instant
    - navigation.top
    - search.suggest
    - content.code.copy
    - toc.integrate
  custom_dir: docs/overrides
  palette:
    - media: "(prefers-color-scheme: light)"
      scheme: default
      primary: custom
      toggle:
        icon: material/brightness-7
        name: Switch to dark mode
    - media: "(prefers-color-scheme: dark)"
      scheme: slate
      primary: custom
      toggle:
        icon: material/brightness-4
        name: Switch to light mode
  logo: template_assets/cisco_logo.png
  favicon: template_assets/cisco_logo.png
extra:
  generator: false
extra_css:
  - stylesheets/extra.css
plugins:
  - search
  - glightbox:
      touchNavigation: true
      loop: false
      effect: fade
      slide_effect: slide
      width: 100%
      height: auto
      zoomable: true
      draggable: false
      auto_caption: true
      caption_position: top
markdown_extensions:
  - abbr
  - admonition
  - attr_list
  - def_list
  - footnotes
  - meta
  - md_in_html
  - tables
  - toc:
      toc_depth: 2
  - pymdownx.details
  - pymdownx.highlight
  - pymdownx.superfences:
      custom_fences:
        - name: mermaid
          class: mermaid
          format: !!python/name:pymdownx.superfences.fence_code_format
nav:
  - Home: index.md
  - Lab:
{nav_sections}
"""

_EXTRA_CSS = """\
.md-typeset__table {
    min-width: 100%;
}

.md-typeset table:not([class]) {
    display: table;
}

.md-grid {
    max-width: 1440px;
}

:root {
    --md-primary-fg-color: rgb(11, 23, 44);
    --md-accent-fg-color: #00bdeb;
}

[data-md-color-scheme="default"] {
    --md-typeset-a-color: #3a7fff;
    --md-accent-fg-color: #00bdeb;
}

[data-md-color-scheme="slate"] {
    --md-typeset-a-color: #3a7fff;
    --md-accent-fg-color: #00bdeb;
}
"""

_HOME_HTML = """\
{% extends "main.html" %}
{% block tabs %}
{{ super() }}
<style>
    .md-container { height: 100%; background: var(--md-primary-fg-color) }
    .md-main { flex-grow: 0; height: 0px; background: var(--md-primary-fg-color) }
    .md-main__inner { display: flex; height: 100%; }
    .tx-container { padding-top: .0rem; background: var(--md-primary-fg-color) }
    .tx-hero { margin: 32px 2.8rem; color: var(--md-primary-bg-color); justify-content: center; }
    .tx-hero h1 { margin-bottom: 1rem; color: currentColor; font-weight: 700 }
    :root { .tx-hero__content { margin: 0; } }
    .tx-hero__content { padding-bottom: 1rem; margin: 0 auto; }
    .tx-hero__logo { scale: 1; width: 100%; padding-bottom: 50px; }
    .tx-hero__image { width: 850px; height: 1050px; order: 1; padding-right: 2.5rem; }
    .tx-hero .md-button { margin-top: .5rem; margin-right: .5rem; color: var(--md-primary-bg-color) }
    .tx-hero .md-button--primary { background-color: var(--md-primary-bg-color); color: rgb(0,0,0); border-color: var(--md-primary-bg-color) }
    .tx-hero .md-button:focus, .tx-hero .md-button:hover { background-color: var(--md-accent-fg-color); color: var(--md-default-bg-color); border-color: var(--md-accent-fg-color) }
    @media screen and (min-width:60em) {
        .md-sidebar--secondary { display: none }
        .tx-hero { display: flex; align-items: center; justify-content: center; }
        .tx-hero__content { max-width: 22rem; margin-top: 3.5rem; margin-bottom: 3.5rem; margin-left: 1.0rem; margin-right: 4.0rem; align-items: center; }
    }
    @media screen and (min-width:76.25em) {
        .md-sidebar--primary { display: none }
    }
</style>
<section class="tx-container">
    <div class="md-grid md-typeset">
        <div class="tx-hero">
            <div class="tx-hero__image">
                <img src="template_assets/CLAMER2025_Static_Midnight_Generic.png" draggable="false">
            </div>
            <div class="tx-hero__content">
                <div class="tx-hero__logo">
                    <img src="template_assets/TE_white_Logo_300dpi.png" draggable="false">
                </div>
                <h1>
                    <script>
                        var title = document.querySelector('meta[name="description"]').content
                        const lab = title.split(" ")[0]
                        document.write(lab)
                    </script>
                </h1>
                <p>
                    <script>
                        const descr = title.substring(title.indexOf(" ") + 1)
                        document.write(descr)
                    </script>
                </p>
                <a href="{{ nav.items[1].children[0].url }}" class="md-button md-button--primary">
                    Get started
                </a>
            </div>
        </div>
    </div>
</section>
{% endblock %}
{% block content %} {% endblock %}
{% block footer %} {% endblock %}
"""

_GITHUB_ACTIONS_WORKFLOW = """\
name: Deploy Lab Guide to GitHub Pages

on:
  push:
    branches:
      - main

permissions:
  contents: write

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Install MkDocs dependencies
        run: pip install mkdocs-material mkdocs-glightbox

      - name: Deploy to GitHub Pages
        run: mkdocs gh-deploy --force
"""

# Reference assets bundled from ciscodocs/ltrxar-3783-tech-elevate-fy26
_REFERENCE_ASSETS_REPO = "https://github.com/ciscodocs/ltrxar-3783-tech-elevate-fy26.git"

def export_mkdocs(guide: LabGuide, output_dir: Path) -> Path:
    """
    Generate a full MkDocs docs/ directory structure.
    output_dir/
      mkdocs.yml
      docs/
        index.md
        <section-slug>/
          index.md
    """
    docs_dir = output_dir / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)

    def slugify(text: str) -> str:
        import re
        return re.sub(r"[^\w]+", "-", text.lower()).strip("-")

    # index.md
    intro_md = render_markdown(guide, include_screenshots=False)
    (docs_dir / "index.md").write_text(intro_md)

    # Copy screenshots into docs/screenshots/ so MkDocs can serve them
    _copy_screenshots(guide, docs_dir)

    # Per-section pages
    nav_sections = ""
    for sec in guide.sections:
        slug = slugify(sec.title)
        sec_dir = docs_dir / slug
        sec_dir.mkdir(exist_ok=True)

        lines = [f"# {sec.title}", ""]
        if sec.overview:
            import re as _re
            fixed_overview = _re.sub(
                r'!\[([^\]]*)\]\(screenshots/([^)]+)\)',
                r'![\1](../screenshots/\2)',
                sec.overview
            )
            lines += [fixed_overview, ""]
        # Content blocks (text + screenshots interleaved, in order)
        for blk in getattr(sec, "blocks", []):
            if blk.type == "text" and blk.content:
                lines += [blk.content, ""]
            elif blk.type == "screenshot" and blk.path:
                fname = Path(blk.path).name
                cap = blk.caption or fname
                lines += [f"![{cap}](../screenshots/{fname})", ""]
        # Legacy section-level screenshots (shown after blocks if blocks absent)
        if not getattr(sec, "blocks", []):
            for ss in getattr(sec, "screenshots", []):
                fname = Path(ss.path).name
                lines += [f"![{ss.caption}](../screenshots/{fname})", ""]
        for step in sec.steps:
            lines += [f"## Step {step.order}: {step.title}", ""]
            lines += [step.instruction, ""]
            for cb in step.code_blocks:
                lines += ["```", cb, "```", ""]
            for ss in step.screenshots:
                # path stored as "screenshots/filename" — resolve relative to section subdir
                fname = Path(ss.path).name
                lines += [f"![{ss.caption}](../screenshots/{fname})", ""]
            if step.expected_result:
                lines += [f"!!! success \"Expected Result\"", f"    {step.expected_result}", ""]
            if step.notes:
                lines += [f"!!! note", f"    {step.notes}", ""]

        (sec_dir / "index.md").write_text("\n".join(lines))
        # Quote the label if it contains a colon (would break YAML otherwise)
        label = f'"{sec.title}"' if ":" in sec.title else sec.title
        nav_sections += f"    - {label}: {slug}/index.md\n"

    # mkdocs.yml
    yml = _MKDOCS_YML_TEMPLATE.format(
        title=guide.metadata.title,
        nav_sections=nav_sections,
    )
    (output_dir / "mkdocs.yml").write_text(yml)

    # ── Cisco brand assets ──────────────────────────────────────
    _write_brand_assets(docs_dir)

    # ── GitHub Actions workflow ─────────────────────────────────
    workflow_dir = output_dir / ".github" / "workflows"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    (workflow_dir / "deploy.yml").write_text(_GITHUB_ACTIONS_WORKFLOW)

    return output_dir


def _copy_screenshots(guide: LabGuide, docs_dir: Path) -> None:
    """Copy all referenced screenshots into docs_dir/screenshots/."""
    import shutil as _shutil
    ss_dst = docs_dir / "screenshots"
    ss_dst.mkdir(exist_ok=True)
    data_dir = Path(__file__).parent.parent / "data"
    for sec in guide.sections:
        # Block-level screenshots
        for blk in getattr(sec, "blocks", []):
            if blk.type == "screenshot" and blk.path:
                src = data_dir / blk.path
                if src.exists():
                    _shutil.copy2(src, ss_dst / src.name)
        # Legacy section-level screenshots
        for ss in getattr(sec, "screenshots", []):
            src = data_dir / ss.path
            if src.exists():
                _shutil.copy2(src, ss_dst / src.name)
        # Step-level screenshots
        for step in sec.steps:
            for ss in step.screenshots:
                src = data_dir / ss.path
                if src.exists():
                    _shutil.copy2(src, ss_dst / src.name)


def _write_brand_assets(docs_dir: Path) -> None:
    """Write Cisco brand assets (CSS, overrides, images) into docs_dir."""
    import shutil, base64

    # Stylesheets
    (docs_dir / "stylesheets").mkdir(exist_ok=True)
    (docs_dir / "stylesheets" / "extra.css").write_text(_EXTRA_CSS)

    # Overrides
    (docs_dir / "overrides").mkdir(exist_ok=True)
    (docs_dir / "overrides" / "home.html").write_text(_HOME_HTML)

    # Template assets — copy from cached reference clone if available,
    # otherwise copy from the local export that already has them
    asset_src = Path("/tmp/ltrxar-3783/docs/template_assets")
    asset_dst = docs_dir / "template_assets"
    asset_dst.mkdir(exist_ok=True)
    needed = [
        "cisco_logo.png",
        "TE_white_Logo_300dpi.png",
        "CLAMER2025_Static_Midnight_Generic.png",
    ]
    for fname in needed:
        src = asset_src / fname
        dst = asset_dst / fname
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)


def push_mkdocs_to_git(output_dir: Path, repo_url: str, branch: str = "main") -> str:
    """
    Build/update a git repo in output_dir and push to repo_url.

    Injects the mokuma56 PAT into the HTTPS URL so the push bypasses
    the macOS keychain (which returns the EMU account instead).
    """
    import re as _re

    # Inject credentials into HTTPS URL: https://user:token@github.com/...
    _TOKEN = "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"
    _USER  = "mokuma56"
    if repo_url.startswith("https://github.com/"):
        auth_url = repo_url.replace("https://", f"https://{_USER}:{_TOKEN}@")
    else:
        auth_url = repo_url   # SSH or custom — use as-is

    def _git(*args, check=True):
        return subprocess.run(
            ["git", *args],
            cwd=output_dir,
            check=check,
            capture_output=True,
            text=True,
        )

    git_dir = output_dir / ".git"
    is_fresh = not git_dir.exists()

    # ── Init if needed ──────────────────────────────────────────
    if is_fresh:
        _git("init")
        _git("remote", "add", "origin", auth_url)
        # Fetch so --force-with-lease has ref tracking info
        _git("fetch", "origin", check=False)
    else:
        result = _git("remote", "get-url", "origin", check=False)
        if result.returncode != 0:
            _git("remote", "add", "origin", auth_url)
        else:
            _git("remote", "set-url", "origin", auth_url)

    # ── Ensure a git identity exists (local fallback) ───────────
    for key, val in [("user.email", "lab-guide@localhost"), ("user.name", "Lab Guide Automator")]:
        r = _git("config", key, check=False)
        if not r.stdout.strip():
            _git("config", key, val)

    # ── Stage + commit if anything changed ──────────────────────
    _git("add", "-A")
    status = _git("status", "--porcelain")
    if status.stdout.strip():
        _git("commit", "-m", f"Update lab guide — {datetime.now().strftime('%Y-%m-%d %H:%M')}")

    # ── Push ────────────────────────────────────────────────────
    _git("push", "-u", "--force-with-lease", "origin", branch)
    return f"Pushed to {repo_url} ({branch})"


# ─────────────────────────────────────────────────────────────
# Narrated video (TTS + ffmpeg)
# ─────────────────────────────────────────────────────────────

def _build_narration_script(guide: LabGuide) -> str:
    """Build a narration script from the guide (one sentence per step)."""
    lines = [f"Welcome to the lab: {guide.metadata.title}. "]
    if guide.introduction:
        lines.append(guide.introduction.split("\n")[0])
    for sec in guide.sections:
        lines.append(f"Section: {sec.title}. ")
        for step in sec.steps:
            lines.append(f"Step {step.order}: {step.title}. {step.instruction.split('.')[0]}. ")
    if guide.conclusion:
        lines.append(guide.conclusion.split("\n")[0])
    return " ".join(lines)


async def generate_narration_audio(
    settings,
    guide: LabGuide,
    output_path: Path,
) -> Path:
    """Generate narration audio using the configured TTS provider."""
    script = _build_narration_script(guide)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if settings.tts_provider == "openai":
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=settings.openai_api_key)
        response = await client.audio.speech.create(
            model="tts-1-hd",
            voice=settings.tts_voice,
            input=script,
        )
        output_path.write_bytes(response.content)

    elif settings.tts_provider == "edge":
        # edge-tts (free, no API key needed)
        import edge_tts  # type: ignore
        communicate = edge_tts.Communicate(script, voice="en-US-AriaNeural")
        await communicate.save(str(output_path))

    else:
        # macOS system TTS fallback
        mp3_path = output_path.with_suffix(".aiff")
        subprocess.run(["say", "-o", str(mp3_path), script], check=True)
        subprocess.run(
            [_ffmpeg_bin(), "-y", "-i", str(mp3_path), str(output_path)],
            check=True, capture_output=True,
        )
        mp3_path.unlink(missing_ok=True)

    return output_path


async def export_narrated_video(
    settings,
    guide: LabGuide,
    recording_path: Path,
    output_path: Path,
    session_dir: Path,
) -> Path:
    """
    Combine the original recording with AI-generated narration audio.
    If recording has audio, it's mixed; otherwise narration is the only track.
    """
    narration_path = session_dir / "narration.mp3"
    await generate_narration_audio(settings, guide, narration_path)

    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Mix original audio (if any) with narration, or just replace
    subprocess.run([
        _ffmpeg_bin(), "-y",
        "-i", str(recording_path),
        "-i", str(narration_path),
        "-filter_complex",
        "[0:a][1:a]amix=inputs=2:duration=first:dropout_transition=2[aout]",
        "-map", "0:v",
        "-map", "[aout]",
        "-c:v", "copy",
        "-c:a", "aac",
        "-shortest",
        str(output_path),
    ], check=True, capture_output=True)

    return output_path
