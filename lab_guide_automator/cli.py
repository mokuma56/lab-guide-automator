"""
CLI entry point — wraps the MCP tools for direct command-line use.
Useful for batch jobs, cron, CI/CD.

Usage:
  lab-guide record start [--no-audio]
  lab-guide record stop <session_id>
  lab-guide record screenshot <session_id> [--label TEXT]
  lab-guide ingest video <video_path> <title> [--interval 5]
  lab-guide ingest screenshots <folder> <title>
  lab-guide list
  lab-guide show <guide_id>
  lab-guide export <guide_id> --format [md|pdf|html|docx|mkdocs]
"""
from __future__ import annotations
import asyncio
import json
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from rich.markdown import Markdown

app = typer.Typer(name="lab-guide", help="Lab Guide Automator CLI")
record_app = typer.Typer(help="Recording commands")
ingest_app = typer.Typer(help="Ingestion commands")
export_app = typer.Typer(help="Export commands")
app.add_typer(record_app, name="record")
app.add_typer(ingest_app, name="ingest")
app.add_typer(export_app, name="export")

console = Console()


def _settings():
    from lab_guide_automator.config import Settings
    return Settings()


# ─────────────────────────────────────────────────────────────
# Record
# ─────────────────────────────────────────────────────────────

@record_app.command("start")
def record_start(
    no_audio: bool = typer.Option(False, "--no-audio", help="Disable audio capture"),
    fps: int = typer.Option(15, help="Frames per second"),
):
    """Start a screen recording session."""
    from lab_guide_automator.server import start_screen_recording
    result = start_screen_recording(audio=not no_audio, fps=fps)
    console.print(f"[green]Recording started[/green] — session: [bold]{result['session_id']}[/bold]")
    console.print(f"Output: {result['output_dir']}")
    console.print(f"Stop with: [cyan]lab-guide record stop {result['session_id']}[/cyan]")


@record_app.command("stop")
def record_stop(session_id: str = typer.Argument(..., help="Session ID")):
    """Stop a recording session."""
    from lab_guide_automator.server import stop_screen_recording
    result = stop_screen_recording(session_id)
    console.print(f"[green]Recording saved:[/green] {result['video_path']}")
    console.print(f"Duration: {result['duration_s']}s")


@record_app.command("screenshot")
def record_screenshot(
    session_id: str = typer.Argument(...),
    label: str = typer.Option("", "--label", "-l"),
):
    """Take a screenshot during a recording."""
    from lab_guide_automator.server import capture_screenshot
    result = capture_screenshot(session_id, label)
    console.print(f"[green]Screenshot saved:[/green] {result['path']}")


# ─────────────────────────────────────────────────────────────
# Ingest
# ─────────────────────────────────────────────────────────────

@ingest_app.command("video")
def ingest_video_cmd(
    video_path: str = typer.Argument(..., help="Path to .mp4 recording"),
    title: str = typer.Argument(..., help="Lab guide title"),
    interval: float = typer.Option(5.0, "--interval", "-i", help="Frame extraction interval in seconds"),
):
    """Process a screen recording into a draft lab guide."""
    from lab_guide_automator.server import ingest_video
    console.print(f"[cyan]Processing video:[/cyan] {video_path}")
    result = asyncio.run(ingest_video(video_path, title, frame_interval_seconds=interval))
    console.print(f"[green]Guide created:[/green] {result['guide_id']}")
    console.print(f"  Sections: {result['sections']}  Steps: {result['steps']}  Objectives: {result['objectives']}")
    console.print(f"  Saved: {result['guide_path']}")


@ingest_app.command("screenshots")
def ingest_screenshots_cmd(
    folder: str = typer.Argument(..., help="Folder of screenshots"),
    title: str = typer.Argument(..., help="Lab guide title"),
):
    """Process a folder of screenshots into a draft lab guide."""
    from lab_guide_automator.server import ingest_screenshot_folder
    result = asyncio.run(ingest_screenshot_folder(folder, title))
    console.print(f"[green]Guide created:[/green] {result['guide_id']}")


# ─────────────────────────────────────────────────────────────
# Guide management
# ─────────────────────────────────────────────────────────────

@app.command("list")
def list_guides_cmd():
    """List all saved lab guides."""
    from lab_guide_automator.server import list_guides
    guides = list_guides()
    if not guides:
        console.print("[yellow]No guides found.[/yellow]")
        return
    table = Table("ID", "Title", "Version", "Sections", "Steps", "Updated")
    for g in guides:
        table.add_row(
            g["guide_id"][:8], g["title"], g["version"],
            str(g["sections"]), str(g["steps"]), g["updated_at"][:10],
        )
    console.print(table)


@app.command("show")
def show_guide(guide_id: str = typer.Argument(...)):
    """Show a guide's structure."""
    from lab_guide_automator.server import get_guide_summary
    summary = get_guide_summary(guide_id)
    console.print(f"\n[bold]{summary['metadata']['title']}[/bold]  v{summary['metadata']['version']}")
    console.print(f"Author: {summary['metadata']['author']}  |  {summary['metadata']['difficulty']}")
    console.print(f"\n[cyan]Learning Objectives:[/cyan]")
    for o in summary["learning_objectives"]:
        console.print(f"  [{o['bloom_level']}] {o['text']}")
    console.print(f"\n[cyan]Sections:[/cyan]")
    for sec in summary["sections"]:
        console.print(f"  [bold]{sec['title']}[/bold]  ({len(sec['steps'])} steps)")
        for s in sec["steps"]:
            console.print(f"    {s['order']}. {s['title']}  [{s['id']}]")


# ─────────────────────────────────────────────────────────────
# Export
# ─────────────────────────────────────────────────────────────

@export_app.command("all")
def export_all(
    guide_id: str = typer.Argument(...),
    output_dir: Optional[str] = typer.Option(None, "--out"),
):
    """Export in all formats (md, pdf, html, docx, mkdocs)."""
    from lab_guide_automator.server import (
        export_guide_markdown, export_guide_pdf,
        export_guide_html, export_guide_docx, export_guide_mkdocs,
    )
    base = Path(output_dir) if output_dir else Path(f"./exports/{guide_id}")
    base.mkdir(parents=True, exist_ok=True)

    for fmt, fn in [
        ("Markdown", lambda: export_guide_markdown(guide_id, str(base / f"{guide_id}.md"))),
        ("PDF", lambda: export_guide_pdf(guide_id, str(base / f"{guide_id}.pdf"))),
        ("HTML", lambda: export_guide_html(guide_id, str(base / f"{guide_id}.html"))),
        ("DOCX", lambda: export_guide_docx(guide_id, str(base / f"{guide_id}.docx"))),
        ("MkDocs", lambda: export_guide_mkdocs(guide_id, str(base / "mkdocs"))),
    ]:
        try:
            result = fn()
            console.print(f"[green]{fmt}:[/green] {result['path']}")
        except Exception as e:
            console.print(f"[red]{fmt} failed:[/red] {e}")


if __name__ == "__main__":
    app()
