# Lab Guide Automator

Agentic MCP server that automates writing technical lab guides from screen recordings.

## What it does

1. **Record** — captures your screen (+ audio) as you walk through lab tasks using macOS native capture via ffmpeg
2. **Ingest** — extracts frames, uses vision AI to describe each step, then clusters into sections with learning objectives and introduction — all auto-drafted
3. **Edit** — conversational agentic editing loop: rewrite steps, add objectives, tweak introductions — all via natural language feedback
4. **Export** — push to PDF, DOCX, HTML (Moodle), MkDocs site, or narrated video with AI voiceover

## Quick start

```bash
cd ~/sw_projects/lab_guide_automator
cp .env.example .env
# Fill in OPENAI_API_KEY (or ANTHROPIC_API_KEY / OLLAMA_BASE_URL)

uv sync
uv run lab-guide-mcp   # Start MCP server
```

## Use with OpenCode

Add to your `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "lab-guide": {
      "type": "local",
      "command": "uv",
      "args": ["run", "--project", "/Users/maokuma/sw_projects/lab_guide_automator", "lab-guide-mcp"]
    }
  }
}
```

## MCP Tools Reference

### Recording
| Tool | Description |
|------|-------------|
| `start_screen_recording` | Start recording screen (macOS) |
| `stop_screen_recording` | Stop and save the recording |
| `capture_screenshot` | Take a screenshot mid-recording with a label |
| `list_recording_sessions` | List active recording sessions |

### Ingestion (video/screenshots → draft guide)
| Tool | Description |
|------|-------------|
| `ingest_video` | Process a .mp4 into a full draft LabGuide |
| `ingest_screenshot_folder` | Process a folder of screenshots |
| `create_blank_guide` | Start a new blank guide manually |

### Editing
| Tool | Description |
|------|-------------|
| `rewrite_step_instruction` | Rewrite a step with feedback |
| `rewrite_section_overview_text` | Rewrite a section overview |
| `add_learning_objective` | Add an objective (AI formalizes it) |
| `add_step_to_section` | Insert a new AI-drafted step |
| `rewrite_introduction_text` | Rewrite the intro |
| `rewrite_conclusion_text` | Rewrite the conclusion |
| `update_guide_metadata` | Update title, author, tags, etc. |
| `suggest_guide_improvements` | AI reviews and suggests improvements |

### Navigation
| Tool | Description |
|------|-------------|
| `list_guides` | List all saved guides |
| `get_guide_summary` | Full structure with section/step IDs |
| `get_step_detail` | Full content of a specific step |

### Export
| Tool | Description |
|------|-------------|
| `export_guide_markdown` | Export as `.md` |
| `export_guide_pdf` | Export as PDF (weasyprint) |
| `export_guide_html` | Export as HTML (Moodle-compatible, images embedded) |
| `export_guide_docx` | Export as Word document |
| `export_guide_mkdocs` | Generate MkDocs Material site (+ optional git push) |
| `export_guide_narrated_video` | Generate narrated .mp4 with AI TTS |

## CLI usage

```bash
# Record
lab-guide record start
lab-guide record screenshot abc123 --label "ospf-neighbors"
lab-guide record stop abc123

# Ingest
lab-guide ingest video /path/to/recording.mp4 "OSPF Lab" --interval 4
lab-guide ingest screenshots ./screenshots "BGP Lab"

# Manage
lab-guide list
lab-guide show <guide_id>

# Export all formats
lab-guide export all <guide_id> --out ./my-lab-exports
```

## Environment variables

| Variable | Default | Description |
|----------|---------|-------------|
| `AI_PROVIDER` | `openai` | `openai` \| `anthropic` \| `ollama` |
| `OPENAI_API_KEY` | — | Required for OpenAI |
| `ANTHROPIC_API_KEY` | — | Required for Anthropic |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama endpoint |
| `TTS_PROVIDER` | `openai` | `openai` \| `edge` \| `system` |
| `TTS_VOICE` | `nova` | OpenAI TTS voice |
| `MKDOCS_REPO_URL` | — | Git remote for MkDocs push |
| `DATA_DIR` | `./data` | Where sessions/guides/exports are stored |

## Dependencies

- **ffmpeg** — `brew install ffmpeg` (required for recording + video export)
- **weasyprint** — PDF export (auto-installed via uv)
- **python-docx** — DOCX export (auto-installed)
- **mkdocs-material** — MkDocs site generation (auto-installed)
