# Lab Guide Automator

A full-featured web application for creating, editing, and publishing professional technical lab guides. Built for Cisco technical educators, solution engineers, and content creators who need to rapidly produce high-quality lab documentation from screen recordings, screenshots, or scratch.

---

## Overview

Lab Guide Automator gives you a browser-based authoring environment that handles the entire lab guide lifecycle:

- **Ingest** — record your screen or upload screenshots and let AI generate a draft guide automatically
- **Edit** — a rich block-based editor with text, screenshots, callouts, dividers, and tables
- **Annotate** — draw on screenshots directly in the browser with arrows, boxes, text, and speech bubbles
- **Publish** — export to a live MkDocs website (GitHub Pages / AWS Amplify), Word (.docx), Markdown, or PDF

---

## Features

### Guide Editor
- **Block-based content** — every section and step is composed of ordered blocks: text, screenshot, callout, or divider
- **Rich text editor** — Quill-powered with bold, italic, headings, lists, links, code blocks, tables, and horizontal dividers
- **Drag-free reordering** — ▲▼ arrow buttons on every block and section for precise ordering without drag-and-drop issues
- **Insert anywhere** — add blocks at the top, bottom, or between any existing blocks
- **Section management** — create, rename, reorder (▲▼), and delete sections; steps live inside sections

### Callout Types
Six styled callout block types with colour-coded rendering in the editor, preview pane, and published site:

| Type | Colour | Use |
|---|---|---|
| 📝 Note | Blue | Important information |
| ⚠️ Caution | Orange | Warnings and risks |
| ✅ Expected Result | Green | What the student should see |
| 🎉 Congratulations | Purple | Milestone reached |
| 💡 Tip | Teal | Helpful hints |
| 🏆 Team Challenge | Gold | Group activities and competitions |

Callouts support custom titles (e.g. "🏆 Team Challenge — AI Canvas RCA") stored in the caption field.

### Screenshot Annotation
- Draw arrows, boxes, circles, text labels, and speech bubbles directly on screenshots
- Move, resize, and delete annotation objects
- Save annotated screenshots back to disk — thumbnails update immediately (no browser cache issues)
- Annotate from session recordings or from the screenshot repository

### AI Tools
- **AI Section rewrite** — rewrite an entire section's content in a consistent tone
- **AI Step enhance** — improve individual step instructions
- **AI Caption** — auto-generate captions for screenshots using vision AI
- **AI Normalize** — standardize tone and formatting across all steps in a section
- **AI Objectives** — generate Bloom's taxonomy learning objectives from a plain-English description

### Learning Objectives
- Add, edit, delete, and **drag-to-reorder** learning objectives
- Each objective has a Bloom's taxonomy level (remember / understand / apply / analyze / evaluate / create)
- Inline editor with textarea and level dropdown — no page reload

### Export Formats

#### MkDocs Website (Primary)
- Generates a full MkDocs Material site with navigation, search, and Cisco branding
- Pushes directly to a GitHub repository via git — works with GitHub Pages or AWS Amplify auto-deploy
- Custom admonitions for all callout types including Team Challenge (gold, trophy icon)
- Copy buttons on all code blocks automatically
- Responsive layout, dark/light mode toggle, glightbox image zoom

#### Word Document (.docx)
- Full HTML-to-Word conversion — no raw markdown or HTML in the output
- Bullet lists → Word "List Bullet" style
- Numbered lists → Word "List Number" style
- Tables → proper Word tables with bold headers
- Callouts → shaded paragraphs with correct colour per type
- Code blocks → grey-shaded Courier New
- Screenshots embedded at 5.5" width, centred
- Dividers → Word horizontal rules

#### Markdown
- Clean Markdown export of the full guide

### Appendix Support
- Appendix sections render identically to regular sections in the published site
- Raw Markdown blocks (via `<!--markdown-->` marker) bypass the HTML converter for precise control
- Ideal for prompt libraries, reference tables, and agent instruction prompts with copy buttons

---

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- A GitHub account (for publishing to a website)
- A GitHub Copilot token or compatible OpenAI-compatible API key (for AI features)

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/mokuma56/lab-guide-automator.git
cd lab-guide-automator
```

### 2. Install dependencies

Using `uv` (recommended):

```bash
uv sync
```

Using pip:

```bash
pip install -e .
pip install python-docx openpyxl pillow
```

### 3. Configure environment

Copy the example environment file and fill in your values:

```bash
cp .env.example .env
```

Edit `.env`:

```env
# GitHub Copilot / OpenAI-compatible API key for AI features
GITHUB_TOKEN=your_token_here

# Optional: default GitHub repo for publishing
GITHUB_REPO=https://github.com/your-org/your-lab-guide-repo.git
GITHUB_BRANCH=main
```

### 4. Start the dashboard

```bash
uv run python3 dashboard.py
```

Or with pip:

```bash
python3 dashboard.py
```

The dashboard runs at **http://localhost:5051** by default.

To use a different port:

```bash
uv run python3 dashboard.py --port 5052
```

---

## Quick Start

### Create your first guide

1. Open **http://localhost:5051**
2. Click **+ New Guide** in the left sidebar
3. Fill in the title, author, duration, and difficulty
4. Click **+ Add Section** to create your first section
5. Use **+ Text**, **🖼 Screenshot**, or **🟩 Callout** buttons to add content blocks
6. Click any text block to open the rich text editor
7. Click **Save** to save your changes

### Ingest from screenshots

1. Click **+ Ingest Screenshots** in the sidebar
2. Upload a folder of screenshots from your lab recording
3. AI will generate step-by-step instructions and captions automatically
4. Review and edit the generated content

### Publish to the web

1. Open your guide and click the **Export** tab
2. Under **Publish to GitHub**, enter your repository URL
3. Click **Build & Publish**
4. The dashboard pushes the MkDocs site to your repo
5. If GitHub Pages or Amplify is configured on the repo, the site goes live automatically

### Export to Word

1. Open your guide and click the **Export** tab
2. Click **Word (.docx)** to download the document

---

## Project Structure

```
lab-guide-automator/
├── dashboard.py              # Main Flask web application (~8000 lines)
├── export/
│   └── exporter.py           # Export engines: MkDocs, DOCX, Markdown, PDF
├── lab_guide_automator/
│   ├── models.py             # Pydantic data models (LabGuide, LabSection, LabStep, ContentBlock)
│   ├── editor.py             # AI-powered editing functions
│   ├── ingest.py             # Screenshot and recording ingestion
│   └── config.py             # Settings and configuration
├── templates/                # Jinja2 templates
├── recording/                # Screen recording utilities
├── data/                     # Runtime data (gitignored)
│   ├── guides/               # Guide JSON files
│   ├── screenshots/          # Screenshot repository
│   ├── sessions/             # Recording sessions
│   └── exports/              # Generated exports
├── pyproject.toml
└── .env.example
```

---

## Data Model

Each guide is stored as a single JSON file with this structure:

```
LabGuide
├── metadata (title, author, version, difficulty, duration, tags)
├── introduction (markdown text)
├── learning_objectives[] (text, bloom_level)
├── sections[]
│   ├── title, overview, order
│   ├── blocks[] (text | screenshot | callout | divider)
│   └── steps[]
│       ├── title, instruction, order
│       └── blocks[] (text | screenshot | callout | divider)
└── conclusion
```

### ContentBlock types

| Type | Fields | Description |
|---|---|---|
| `text` | `content` (HTML) | Rich text paragraph(s) |
| `screenshot` | `path`, `caption` | Image block |
| `callout` | `callout_type`, `content`, `caption` | Styled callout box |
| `divider` | — | Horizontal rule |

---

## Keyboard Shortcuts

| Shortcut | Action |
|---|---|
| `Cmd+Shift+S` | Save current guide (global hotkey) |
| `Enter` in text editor | Commit text annotation |
| `Escape` in text editor | Cancel annotation |

---

## Publishing Setup

### GitHub Pages

1. Create a new GitHub repository (public or private)
2. Go to **Settings → Pages** and set source to **GitHub Actions**
3. Enter the repository URL in the Lab Guide Automator Export tab
4. Click **Build & Publish** — the included GitHub Actions workflow deploys automatically

### AWS Amplify

1. Connect your GitHub repository to AWS Amplify
2. Use the default build settings (Amplify will detect the static site)
3. Every push from Lab Guide Automator triggers an automatic Amplify deploy

---

## AI Features Setup

AI features require a GitHub Copilot token or an OpenAI-compatible API key set in your `.env` file.

Features that use AI:
- Section rewrite (`✦ AI` button on sections)
- Step instruction enhancement
- Screenshot captioning (`✦ AI Caption`)
- Learning objective generation
- Tone normalization across steps

If no API key is configured, AI buttons are still visible but will return an error when clicked. All other features work without AI.

---

## Troubleshooting

### Dashboard won't start
- Check that port 5051 is not already in use: `lsof -i :5051`
- If running alongside another dashboard on 5050, they operate independently — use `--port` to assign different ports

### Guide not loading after changes
- The dashboard caches guides in memory — restart the dashboard to pick up external JSON edits: `pkill -f "python3 dashboard.py"`
- Always stop the dashboard before editing guide JSON files directly

### Publish fails
- Ensure the GitHub token configured in your git credential helper has `repo` write access
- Check that the remote URL is correct in the Export tab

### Word export looks wrong
- Ensure `python-docx` is installed: `uv add python-docx`
- Complex HTML in text blocks (e.g. custom `<div>` elements) is stripped to plain text in the DOCX export

---

## Contributing

Pull requests are welcome. For major changes please open an issue first to discuss what you would like to change.

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Commit your changes: `git commit -m "feat: add my feature"`
4. Push to the branch: `git push origin feature/my-feature`
5. Open a Pull Request

---

## License

MIT License — see [LICENSE](LICENSE) for details.

---

## Acknowledgements

Built with:
- [Flask](https://flask.palletsprojects.com/) — web framework
- [Quill.js](https://quilljs.com/) — rich text editor
- [MkDocs Material](https://squidfunk.github.io/mkdocs-material/) — documentation site theme
- [python-docx](https://python-docx.readthedocs.io/) — Word document generation
- [Pillow](https://pillow.readthedocs.io/) — image processing
- [Pydantic](https://docs.pydantic.dev/) — data validation and models
