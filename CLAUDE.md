# Lab Guide Automator

Browser-based authoring environment for Cisco technical lab guides: ingest screen
recordings/screenshots → AI-drafted guide → block editor with screenshot annotation →
publish to MkDocs / .docx / Markdown / PDF.

## Commands

```bash
# Dashboard (http://localhost:5051)
cd ~/sw_projects/lab_guide_automator && uv run python dashboard.py

# CLI
uv run lab-guide-mcp                             # MCP server entrypoint
uv run python generate_docs.py                   # generate user guide docx
uv run python reingest_docx.py <file.docx>       # re-ingest an existing guide
```

## Architecture

- `dashboard.py` — Flask app + embedded HTML/JS UI (the editor, annotation canvas,
  preview pane). ~8,400 lines; this is where nearly all UI work lands.
- `lab_guide_automator/` — the importable package:
  - `server.py` — MCP server
  - `ingest.py` / `ingest_document.py` — recording and document ingestion
  - `ai_client.py` — AI calls (section rewrite, step enhance, caption, normalize, objectives)
  - `editor.py` — block/section model operations
  - `cli.py` — CLI entrypoints
- `export/exporter.py` — MkDocs / docx / Markdown / PDF export
- `recording/recorder.py` — screen recording capture
- `data/` — runtime state (guides, sessions, uploads, screenshots, exports); all gitignored

Content model: a guide has ordered **sections**; each section holds ordered **steps**;
each step is composed of ordered **blocks** (text, screenshot, callout, divider, table).
Callout titles are stored in the block's `caption` field.

Callout types: Note (blue), Caution (orange), Expected Result (green), Congratulations
(purple), Tip (teal), Team Challenge (gold).

## Configuration

`.env` (gitignored) holds `DATA_DIR` and `MKDOCS_REPO_URL`. See `.env.example` for the
full set. AI provider keys belong in `.env`, never in source.

`certs/` is gitignored — don't add certificate material to the repo.

## Conventions

- Package manager: `uv`.
- Reordering in the UI uses ▲▼ buttons deliberately, not drag-and-drop — drag-and-drop
  proved unreliable in the block editor. Learning objectives are the one exception
  (drag-to-reorder).
- Annotated screenshots are saved back to disk and thumbnails must be cache-busted, or
  the browser serves the pre-annotation image.
- There are no tests. When fixing logic in `export/exporter.py`, `editor.py`, or
  `ingest.py`, add a `pytest` test alongside it.
- `dashboard.py` has ~49 broad `except Exception` handlers. Don't add more; narrow and
  log when you touch one.

## Note on `data/screenshots/`

64 screenshots are tracked in git from before the ignore rule was added — reviewed and
clean (the API-token integration form is captured empty, showing only placeholders).
The ignore rules now use `**/data/screenshots/` so they match at any depth. Keep it that
way, and don't commit screenshots that show a filled-in token, key, or session cookie.
