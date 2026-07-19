"""
Lab Guide Automator — Web Dashboard
Runs on http://localhost:5051 by default
"""
from __future__ import annotations
import asyncio
import json
import queue
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from flask import Flask, Response, jsonify, request, send_from_directory, stream_with_context
from flask_cors import CORS

from lab_guide_automator.config import Settings
from lab_guide_automator.models import LabGuide, LabMetadata, LabSection, LabStep
import lab_guide_automator.editor as editor
import lab_guide_automator.ingest as ingest
from export.exporter import (
    export_markdown, export_pdf, export_html, export_docx, export_mkdocs,
)
from recording.recorder import (
    RecordingSession, start_recording, start_screenshot_session,
    start_audio_session, stop_recording, take_screenshot,
    list_browser_windows, list_audio_devices,
)

app = Flask(__name__)
CORS(app)

settings = Settings()

# ── In-memory state ──────────────────────────────────────────
_active_sessions: dict[str, RecordingSession] = {}
_session_window: dict[str, int] = {}    # session_id → pinned CGWindowID (optional)
_paused_sessions: set[str] = set()      # session_ids currently paused (screenshots blocked)
_loaded_guides: dict[str, LabGuide] = {}
_progress_queues: dict[str, queue.Queue] = {}   # job_id → SSE event queue


# ── Helpers ──────────────────────────────────────────────────

def _data_dir() -> Path:
    d = settings.data_dir
    if not d.is_absolute():
        d = Path(__file__).parent / d
    d.mkdir(parents=True, exist_ok=True)
    return d


def _guide_path(guide_id: str) -> Path:
    return _data_dir() / "guides" / f"{guide_id}.json"


def _load_guide(guide_id: str) -> LabGuide:
    if guide_id in _loaded_guides:
        return _loaded_guides[guide_id]
    p = _guide_path(guide_id)
    if not p.exists():
        raise FileNotFoundError(f"Guide not found: {guide_id}")
    g = LabGuide.load(p)
    _loaded_guides[guide_id] = g
    return g


def _save_guide(guide: LabGuide) -> None:
    _loaded_guides[guide.id] = guide
    guide.save(_guide_path(guide.id))


def _renumber_steps(guide: LabGuide) -> None:
    """Assign globally sequential step.order values across all sections."""
    n = 1
    for sec in guide.sections:
        for step in sec.steps:
            step.order = n
            n += 1


def _sse_event(data: dict) -> str:
    return f"data: {json.dumps(data)}\n\n"


def _run_async(coro):
    """Run an async coroutine from a sync Flask thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ─────────────────────────────────────────────────────────────
# Main page
# ─────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return Response(_render_html(), mimetype="text/html")


# ─────────────────────────────────────────────────────────────
# API — Guides
# ─────────────────────────────────────────────────────────────

@app.route("/api/guides")
def api_list_guides():
    guides_dir = _data_dir() / "guides"
    guides_dir.mkdir(exist_ok=True)
    result = []
    for p in sorted(guides_dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True):
        try:
            g = LabGuide.load(p)
            result.append({
                "id": g.id,
                "title": g.metadata.title,
                "version": g.metadata.version,
                "author": g.metadata.author,
                "difficulty": g.metadata.difficulty,
                "duration": g.metadata.lab_duration_minutes,
                "sections": len(g.sections),
                "steps": g.step_count(),
                "objectives": len(g.learning_objectives),
                "updated_at": g.updated_at,
            })
        except Exception:
            pass
    return jsonify(result)


@app.route("/api/guides", methods=["POST"])
def api_create_guide():
    from lab_guide_automator.models import LabSection, LabStep
    data = request.json or {}
    title = data.get("title", "Untitled Lab")
    author = data.get("author", "")

    default_sections = [
        LabSection(
            title="Introduction",
            overview="Overview of the lab objectives and prerequisites.",
            steps=[
                LabStep(
                    order=1,
                    title="Lab Overview",
                    instruction="Welcome to the lab. In this section you will learn what the lab covers, what you need before starting, and how to navigate the environment.",
                    expected_result="",
                )
            ],
        ),
        LabSection(
            title="Section 1",
            overview="",
            steps=[
                LabStep(
                    order=1,
                    title="Step title",
                    instruction="Describe what the learner should do in this step.",
                    expected_result="",
                )
            ],
        ),
        LabSection(
            title="Conclusion",
            overview="Summary and next steps.",
            steps=[
                LabStep(
                    order=1,
                    title="Summary",
                    instruction="Congratulations — you have completed the lab. Review what you accomplished and explore the suggested next steps.",
                    expected_result="",
                )
            ],
        ),
    ]

    guide = LabGuide(
        metadata=LabMetadata(
            title=title,
            author=author,
            difficulty=data.get("difficulty", "intermediate"),
            lab_duration_minutes=int(data.get("duration", 60)),
        ),
        introduction=f"This lab guides you through {title}.",
        conclusion="You have successfully completed all sections of this lab.",
        sections=default_sections,
    )
    _save_guide(guide)
    return jsonify({"id": guide.id, "title": guide.metadata.title})


@app.route("/api/guides/<guide_id>")
def api_get_guide(guide_id):
    try:
        g = _load_guide(guide_id)
        return jsonify(g.model_dump())
    except FileNotFoundError:
        return jsonify({"error": "Not found"}), 404


@app.route("/api/guides/<guide_id>", methods=["DELETE"])
def api_delete_guide(guide_id):
    p = _guide_path(guide_id)
    if p.exists():
        p.unlink()
    _loaded_guides.pop(guide_id, None)
    return jsonify({"ok": True})


@app.route("/api/guides/<guide_id>/metadata", methods=["POST"])
def api_update_metadata(guide_id):
    try:
        g = _load_guide(guide_id)
        data = request.json or {}
        m = g.metadata
        for field in ["title", "subtitle", "version", "author", "difficulty"]:
            if field in data:
                setattr(m, field, data[field])
        if "duration" in data:
            m.lab_duration_minutes = int(data["duration"])
        if "tags" in data:
            m.tags = data["tags"]
        if "prerequisites" in data:
            m.prerequisites = data["prerequisites"]
        g.touch()
        _save_guide(g)
        return jsonify(m.model_dump())
    except FileNotFoundError:
        return jsonify({"error": "Not found"}), 404


# ─────────────────────────────────────────────────────────────
# API — Guide content editing
# ─────────────────────────────────────────────────────────────

@app.route("/api/guides/<guide_id>/step/<step_id>/rewrite", methods=["POST"])
def api_rewrite_step(guide_id, step_id):
    try:
        g = _load_guide(guide_id)
        feedback = (request.json or {}).get("feedback", "")
        step = _run_async(editor.rewrite_step(settings, g, step_id, feedback))
        _save_guide(g)
        return jsonify({"id": step.id, "instruction": step.instruction, "expected_result": step.expected_result})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/guides/<guide_id>/step/<step_id>", methods=["POST"])
def api_update_step(guide_id, step_id):
    """Direct (non-AI) update to a step's fields."""
    try:
        g = _load_guide(guide_id)
        step = g.get_step(step_id)
        if not step:
            return jsonify({"error": "Step not found"}), 404
        data = request.json or {}
        for field in ["title", "instruction", "expected_result", "notes"]:
            if field in data:
                setattr(step, field, data[field])
        g.touch()
        _save_guide(g)
        return jsonify(step.model_dump())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/guides/<guide_id>/section/<section_id>/rewrite", methods=["POST"])
def api_rewrite_section(guide_id, section_id):
    try:
        g = _load_guide(guide_id)
        feedback = (request.json or {}).get("feedback", "")
        sec = _run_async(editor.rewrite_section_overview(settings, g, section_id, feedback))
        _save_guide(g)
        return jsonify({"id": sec.id, "overview": sec.overview})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/guides/<guide_id>/introduction/rewrite", methods=["POST"])
def api_rewrite_intro(guide_id):
    try:
        g = _load_guide(guide_id)
        feedback = (request.json or {}).get("feedback", "")
        text = _run_async(editor.rewrite_introduction(settings, g, feedback))
        _save_guide(g)
        return jsonify({"introduction": text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/guides/<guide_id>/introduction", methods=["POST"])
def api_save_intro(guide_id):
    try:
        g = _load_guide(guide_id)
        g.introduction = (request.json or {}).get("text", g.introduction)
        g.touch()
        _save_guide(g)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/guides/<guide_id>/conclusion/rewrite", methods=["POST"])
def api_rewrite_conclusion(guide_id):
    try:
        g = _load_guide(guide_id)
        feedback = (request.json or {}).get("feedback", "")
        text = _run_async(editor.rewrite_conclusion(settings, g, feedback))
        _save_guide(g)
        return jsonify({"conclusion": text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/guides/<guide_id>/conclusion", methods=["POST"])
def api_save_conclusion(guide_id):
    try:
        g = _load_guide(guide_id)
        g.conclusion = (request.json or {}).get("text", g.conclusion)
        g.touch()
        _save_guide(g)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/guides/<guide_id>/objective", methods=["POST"])
def api_add_objective(guide_id):
    try:
        g = _load_guide(guide_id)
        desc = (request.json or {}).get("description", "")
        obj = _run_async(editor.add_learning_objective(settings, g, desc))
        _save_guide(g)
        return jsonify({"id": obj.id, "text": obj.text, "bloom_level": obj.bloom_level})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/guides/<guide_id>/objective/<obj_id>", methods=["DELETE"])
def api_delete_objective(guide_id, obj_id):
    try:
        g = _load_guide(guide_id)
        g.learning_objectives = [o for o in g.learning_objectives if o.id != obj_id]
        g.touch()
        _save_guide(g)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/models")
def api_models():
    """Return available Copilot models, fetched live from the Copilot API."""
    import subprocess
    try:
        from lab_guide_automator.ai_client import _get_copilot_token, _make_http_client
        import json as _json
        token = _get_copilot_token()
        client = _make_http_client()
        resp = client.get(
            "https://api.githubcopilot.com/models",
            headers={
                "Authorization": f"Bearer {token}",
                "Copilot-Integration-Id": "vscode-chat",
            },
        )
        data = resp.json()
        models = [
            {"id": m["id"], "name": m.get("name", m["id"])}
            for m in data.get("data", [])
            if "embedding" not in m["id"].lower()
        ]
        return jsonify({"models": models, "current": settings.copilot_model})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/settings/model", methods=["POST"])
def api_set_model():
    """Switch the active Copilot model at runtime (no restart needed)."""
    model = (request.json or {}).get("model", "").strip()
    if not model:
        return jsonify({"error": "model required"}), 400
    settings.copilot_model = model
    return jsonify({"ok": True, "model": model})


@app.route("/api/guides/<guide_id>/suggest", methods=["POST"])
def api_suggest(guide_id):
    try:
        g = _load_guide(guide_id)
        suggestions = _run_async(editor.suggest_improvements(settings, g))
        return jsonify({"suggestions": suggestions})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/guides/<guide_id>/apply-suggestion", methods=["POST"])
def api_apply_suggestion(guide_id):
    try:
        g = _load_guide(guide_id)
        data = request.json or {}
        suggestion = data.get("suggestion", "").strip()
        if not suggestion:
            return jsonify({"error": "suggestion is required"}), 400
        updated = _run_async(editor.apply_suggestion(settings, g, suggestion))
        _save_guide(updated)
        return jsonify({"ok": True, "title": updated.metadata.title})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/guides/<guide_id>/sync-config", methods=["GET", "POST"])
def api_sync_config(guide_id):
    """Get or save the GitHub sync config for a specific guide."""
    g = _load_guide(guide_id)
    if request.method == "GET":
        return jsonify({
            "github_repo": g.github_repo,
            "github_branch": g.github_branch,
            "last_published": g.last_published,
        })
    data = request.json or {}
    g.github_repo = data.get("github_repo", g.github_repo).strip()
    g.github_branch = data.get("github_branch", g.github_branch).strip() or "main"
    _save_guide(g)
    return jsonify({"ok": True, "github_repo": g.github_repo, "github_branch": g.github_branch})


@app.route("/api/guides/<guide_id>/publish")
def api_publish(guide_id):
    """
    Build MkDocs site and push to the guide's configured GitHub repo.
    Streams progress as SSE so the UI can show a live log.
    """
    from export.exporter import export_mkdocs, push_mkdocs_to_git
    from datetime import datetime as _dt

    def _generate():
        try:
            g = _load_guide(guide_id)
            if not g.github_repo:
                yield f"data: {json.dumps({'type':'error','message':'No GitHub repo configured for this guide.'})}\n\n"
                return

            yield f"data: {json.dumps({'type':'progress','message':'Building MkDocs site…'})}\n\n"
            exports_dir = _data_dir() / "exports"
            out_dir = exports_dir / f"{guide_id}-mkdocs"
            export_mkdocs(g, out_dir)
            yield f"data: {json.dumps({'type':'progress','message':f'Site built → {out_dir.name}'})}\n\n"

            yield f"data: {json.dumps({'type':'progress','message':f'Pushing to {g.github_repo} ({g.github_branch})…'})}\n\n"
            result = push_mkdocs_to_git(out_dir, g.github_repo, g.github_branch)
            yield f"data: {json.dumps({'type':'progress','message':result})}\n\n"

            # Record timestamp
            g.last_published = _dt.utcnow().isoformat()
            _save_guide(g)

            yield f"data: {json.dumps({'type':'done','message':'Published successfully!','last_published':g.last_published})}\n\n"
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr or ""
            msg = f"git error (exit {exc.returncode}): {stderr.strip() or str(exc)}"
            yield f"data: {json.dumps({'type':'error','message':msg})}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type':'error','message':str(exc)})}\n\n"

    return app.response_class(_generate(), mimetype="text/event-stream",
                              headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/guides/<guide_id>/step", methods=["POST"])
def api_add_step(guide_id):
    try:
        g = _load_guide(guide_id)
        data = request.json or {}
        step = _run_async(editor.add_step(
            settings, g,
            data["section_id"],
            data["title"],
            data.get("description", data["title"]),
            data.get("insert_after_step_id"),
        ))
        _renumber_steps(g)
        _save_guide(g)
        return jsonify(step.model_dump())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/guides/<guide_id>/section", methods=["POST"])
def api_add_section(guide_id):
    try:
        g = _load_guide(guide_id)
        data = request.json or {}
        title = data.get("title", "New Section")
        overview = data.get("overview", "")
        sec = LabSection(title=title, overview=overview)
        g.sections.append(sec)
        g.touch()
        _save_guide(g)
        return jsonify({"id": sec.id, "title": sec.title, "overview": sec.overview})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/guides/<guide_id>/section/<section_id>", methods=["POST"])
def api_update_section(guide_id, section_id):
    try:
        g = _load_guide(guide_id)
        sec = g.get_section(section_id)
        if not sec:
            return jsonify({"error": "Section not found"}), 404
        data = request.json or {}
        if "title" in data:
            sec.title = data["title"]
        if "overview" in data:
            sec.overview = data["overview"]
        g.touch()
        _save_guide(g)
        return jsonify({"id": sec.id, "title": sec.title, "overview": sec.overview})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/guides/<guide_id>/section/<section_id>", methods=["DELETE"])
def api_delete_section(guide_id, section_id):
    try:
        g = _load_guide(guide_id)
        g.sections = [s for s in g.sections if s.id != section_id]
        _renumber_steps(g)
        g.touch()
        _save_guide(g)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/guides/<guide_id>/sections/reorder", methods=["POST"])
def api_sections_reorder(guide_id):
    try:
        g = _load_guide(guide_id)
        order = (request.json or {}).get("order", [])
        id_to_sec = {s.id: s for s in g.sections}
        g.sections = [id_to_sec[sid] for sid in order if sid in id_to_sec]
        # Keep any sections not mentioned in the order list at the end
        mentioned = set(order)
        g.sections += [s for s in id_to_sec.values() if s.id not in mentioned]
        _renumber_steps(g)
        g.touch()
        _save_guide(g)
        return jsonify({"ok": True, "guide": g.model_dump()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────
# API — Recording
# ─────────────────────────────────────────────────────────────

@app.route("/api/record/start", methods=["POST"])
def api_record_start():
    try:
        data = request.json or {}
        sid = str(uuid.uuid4())[:8]
        out_dir = _data_dir() / "sessions" / sid / "recording"
        # modes: screenshots | video | combo | audio
        mode = data.get("mode", "screenshots")

        if mode == "video":
            # video only — no manual screenshots
            session = start_recording(sid, out_dir, audio=data.get("audio", True))
        elif mode == "combo":
            # video recording + manual screenshots simultaneously
            session = start_recording(sid, out_dir, audio=data.get("audio", True))
        elif mode == "audio":
            # audio-only recording (no video) + manual screenshots
            session = start_audio_session(sid, out_dir)
        else:
            # screenshots only
            session = start_screenshot_session(sid, out_dir)

        _active_sessions[sid] = session
        window_id = data.get("window_id")
        if window_id is not None:
            _session_window[sid] = int(window_id)
        meta = {
            "session_id": sid,
            "name": (data.get("name") or "").strip() or sid,
            "mode": mode,
            "started_at": datetime.now().isoformat(timespec="seconds"),
        }
        meta_path = _data_dir() / "sessions" / sid / "meta.json"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(meta, indent=2))
        return jsonify({"session_id": sid, "status": "recording", "name": meta["name"], "mode": mode})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/record/stop", methods=["POST"])
def api_record_stop():
    try:
        sid = (request.json or {}).get("session_id", "")
        session = _active_sessions.get(sid)
        if not session:
            return jsonify({"error": "No active session"}), 404
        video_path = stop_recording(session)
        del _active_sessions[sid]
        _paused_sessions.discard(sid)
        _session_window.pop(sid, None)
        # Update meta with ended_at, screenshot count, and video_path
        meta_path = _data_dir() / "sessions" / sid / "meta.json"
        try:
            meta = json.loads(meta_path.read_text()) if meta_path.exists() else {"session_id": sid, "name": sid}
            meta["ended_at"] = datetime.now().isoformat(timespec="seconds")
            meta["screenshot_count"] = len(session.screenshots)
            # video_path is None when recording was screenshot-only (no ffmpeg process)
            if video_path and Path(str(video_path)).exists():
                meta["video_path"] = str(video_path)
            meta_path.write_text(json.dumps(meta, indent=2))
        except Exception:
            pass
        return jsonify({
            "session_id": sid,
            "video_path": str(video_path) if video_path else None,
            "duration_s": round(session.elapsed, 1),
            "screenshots": len(session.screenshots),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/record/pause", methods=["POST"])
def api_record_pause():
    sid = (request.json or {}).get("session_id", "")
    if not sid or sid not in _active_sessions:
        return jsonify({"error": "No active session"}), 404
    _paused_sessions.add(sid)
    return jsonify({"session_id": sid, "paused": True})


@app.route("/api/record/resume", methods=["POST"])
def api_record_resume():
    sid = (request.json or {}).get("session_id", "")
    if not sid or sid not in _active_sessions:
        return jsonify({"error": "No active session"}), 404
    _paused_sessions.discard(sid)
    return jsonify({"session_id": sid, "paused": False})


@app.route("/api/record/screenshot", methods=["POST"])
def api_screenshot():
    try:
        data = request.json or {}
        sid = data.get("session_id", "")
        # If no session_id given (e.g. hotkey / float panel), use the most recent active session
        session = _active_sessions.get(sid)
        if not session:
            running = [s for s in _active_sessions.values() if s.is_running()]
            if not running:
                return jsonify({"error": "No active recording session. Start recording first."}), 404
            session = running[-1]
        # Block capture when paused
        if session.session_id in _paused_sessions:
            return jsonify({"error": "Session is paused. Resume before capturing."}), 409
        wid = _session_window.get(session.session_id)
        path, seq = take_screenshot(session, data.get("label", ""), window_id=wid)
        import shutil as _shutil
        dest = _ss_dir() / path.name
        _shutil.copy2(path, dest)
        # Brief macOS notification so user gets feedback without switching windows
        try:
            import subprocess as _sp
            _sp.Popen([
                "osascript", "-e",
                f'display notification "Screenshot {seq} captured" with title "Lab Guide Automator" sound name "Tink"'
            ])
        except Exception:
            pass
        return jsonify({"path": str(path), "seq": seq,
                        "elapsed_s": round(session.elapsed, 1),
                        "repo_filename": path.name,
                        "session_id": session.session_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/record/status")
def api_record_status():
    return jsonify([
        {"session_id": sid, "elapsed_s": round(s.elapsed, 1),
         "screenshots": len(s.screenshots), "running": s.is_running(),
         "paused": sid in _paused_sessions}
        for sid, s in _active_sessions.items()
    ])


@app.route("/api/audio-devices")
def api_audio_devices():
    """Return list of AVFoundation audio input devices."""
    try:
        devices = list_audio_devices()
        return jsonify({"devices": devices})
    except Exception as e:
        return jsonify({"devices": [], "error": str(e)})


@app.route("/api/sessions")
def api_list_sessions():
    """List completed capture sessions, newest first.

    Each entry: {session_id, name, screenshot_count, recorded_at, folder_path, size_mb}
    """
    sessions_root = _data_dir() / "sessions"
    if not sessions_root.exists():
        return jsonify([])

    result = []
    for sid_dir in sorted(sessions_root.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if not sid_dir.is_dir():
            continue
        rec_dir = sid_dir / "recording"
        if not rec_dir.exists():
            continue
        shots = sorted(
            list(rec_dir.glob("step-*.png")) +
            list(rec_dir.glob("step-*.jpg")) +
            list(rec_dir.glob("frame_*.jpg"))
        )
        if not shots:
            continue
        # Read name/mode from meta.json if present
        meta_path = sid_dir / "meta.json"
        name = sid_dir.name
        session_mode = "screenshots"
        video_path = None
        audio_path = None
        try:
            if meta_path.exists():
                m = json.loads(meta_path.read_text())
                name = m.get("name", sid_dir.name) or sid_dir.name
                session_mode = m.get("mode", "screenshots")
                vp = m.get("video_path")
                if vp and Path(vp).exists():
                    if Path(vp).suffix == ".m4a":
                        audio_path = vp
                    else:
                        video_path = vp
        except Exception:
            pass
        # Also scan recording dir for mp4/m4a if meta didn't have paths
        if video_path is None:
            for f in rec_dir.glob("*.mp4"):
                video_path = str(f); break
        if audio_path is None:
            for f in rec_dir.glob("*.m4a"):
                audio_path = str(f); break
        mtime = rec_dir.stat().st_mtime
        size_bytes = sum(f.stat().st_size for f in shots)
        result.append({
            "session_id": sid_dir.name,
            "name": name,
            "mode": session_mode,
            "screenshot_count": len(shots),
            "recorded_at": datetime.fromtimestamp(mtime).strftime("%b %d, %Y %H:%M"),
            "folder_path": str(rec_dir),
            "size_mb": round(size_bytes / 1_048_576, 1),
            "has_video": video_path is not None,
            "has_audio": audio_path is not None,
            "video_path": video_path,
            "audio_path": audio_path,
        })
    return jsonify(result)


@app.route("/api/sessions/<sid>", methods=["DELETE"])
def api_delete_session(sid):
    """Delete a capture session folder from disk."""
    import shutil as _shutil
    # Safety: only allow deleting sessions not currently active
    if sid in _active_sessions:
        return jsonify({"error": "Cannot delete an active session. End it first."}), 409
    session_dir = _data_dir() / "sessions" / sid
    if not session_dir.exists():
        return jsonify({"error": "Session not found"}), 404
    try:
        _shutil.rmtree(session_dir)
        return jsonify({"deleted": sid})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sessions/<sid>/rename", methods=["POST"])
def api_rename_session(sid):
    """Rename a session (updates meta.json name field)."""
    name = ((request.json or {}).get("name") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    meta_path = _data_dir() / "sessions" / sid / "meta.json"
    try:
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {"session_id": sid}
        meta["name"] = name
        meta_path.write_text(json.dumps(meta, indent=2))
        return jsonify({"session_id": sid, "name": name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sessions/<sid>/screenshots")
def api_session_screenshots(sid):
    """Return list of screenshot filenames for a session."""
    rec_dir = _data_dir() / "sessions" / sid / "recording"
    if not rec_dir.exists():
        return jsonify({"error": "Session not found"}), 404
    shots = sorted(
        list(rec_dir.glob("step-*.png")) +
        list(rec_dir.glob("step-*.jpg")) +
        list(rec_dir.glob("frame_*.jpg"))
    )
    return jsonify([{"filename": f.name, "path": str(f)} for f in shots])


@app.route("/api/sessions/<sid>/screenshots/<filename>")
def api_session_screenshot_file(sid, filename):
    """Serve a screenshot image from a session folder."""
    rec_dir = _data_dir() / "sessions" / sid / "recording"
    return send_from_directory(str(rec_dir), filename)


@app.route("/api/sessions/<sid>/video")
def api_session_video(sid):
    """Serve the video recording for a session."""
    meta_path = _data_dir() / "sessions" / sid / "meta.json"
    try:
        meta = json.loads(meta_path.read_text())
        vp = meta.get("video_path")
        if vp and Path(vp).exists() and Path(vp).suffix != ".m4a":
            return send_from_directory(str(Path(vp).parent), Path(vp).name)
    except Exception:
        pass
    rec_dir = _data_dir() / "sessions" / sid / "recording"
    for f in rec_dir.glob("*.mp4"):
        return send_from_directory(str(rec_dir), f.name)
    return jsonify({"error": "No video found"}), 404


@app.route("/api/sessions/<sid>/audio")
def api_session_audio(sid):
    """Serve the audio recording (.m4a) for a session."""
    rec_dir = _data_dir() / "sessions" / sid / "recording"
    for f in rec_dir.glob("*.m4a"):
        return send_from_directory(str(rec_dir), f.name)
    return jsonify({"error": "No audio found"}), 404


@app.route("/api/sessions/<sid>/screenshots/<filename>/thumb")
def api_session_screenshot_thumb(sid, filename):
    """Serve a downscaled thumbnail (max 400px wide) for fast grid display."""
    import io
    rec_dir = _data_dir() / "sessions" / sid / "recording"
    src = rec_dir / filename
    if not src.exists():
        return jsonify({"error": "Not found"}), 404
    try:
        from PIL import Image as PILImage
        img = PILImage.open(src)
        img.thumbnail((400, 400), PILImage.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82, optimize=True)
        buf.seek(0)
        from flask import Response
        return Response(buf.read(), mimetype="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})
    except ImportError:
        # Pillow not installed — fall back to serving the original
        return send_from_directory(str(rec_dir), filename)


# ─────────────────────────────────────────────────────────────
# API — Browser window listing + per-session window pin
# ─────────────────────────────────────────────────────────────

@app.route("/api/windows")
def api_windows():
    """Return visible browser windows (id, app, title) via Quartz."""
    return jsonify(list_browser_windows())


@app.route("/api/record/set-window", methods=["POST"])
def api_set_window():
    """Pin a CGWindowID to the active session so screenshots target that window."""
    data = request.json or {}
    window_id = data.get("window_id")   # None = full screen
    # Attach to the most recent running session (or a specific one if given)
    sid = data.get("session_id", "")
    session = _active_sessions.get(sid)
    if not session:
        running = [s for s in _active_sessions.values() if s.is_running()]
        if running:
            session = running[-1]
            sid = session.session_id
    if window_id is None:
        _session_window.pop(sid, None)
    else:
        _session_window[sid] = int(window_id)
    return jsonify({"ok": True, "session_id": sid, "window_id": window_id})


# ─────────────────────────────────────────────────────────────
# Floating capture panel — tiny always-on-top popup window
# Open at http://localhost:5051/capture  (200×180px popup)
# ─────────────────────────────────────────────────────────────

@app.route("/capture")
def capture_panel():
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>📸 Capture</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  html, body {
    width:340px; min-height:100%;
    background:#0d1117; color:#e6edf3;
    font-family:-apple-system,'Segoe UI',sans-serif;
    font-size:12px; user-select:none; overflow-x:hidden;
  }
  body { display:flex; flex-direction:column; gap:8px; padding:12px; }

  .row  { display:flex; gap:6px; align-items:center; }
  .col  { display:flex; flex-direction:column; gap:4px; }
  .lbl  { font-size:10px; color:#8b949e; text-transform:uppercase; letter-spacing:.5px; }
  hr    { border:none; border-top:1px solid #21262d; }

  /* mode toggle */
  .mode-toggle { display:flex; background:#161b22; border:1px solid #30363d; border-radius:7px; padding:2px; gap:2px; }
  .mode-btn {
    flex:1; padding:5px 0; border:none; border-radius:5px;
    font-size:11px; font-weight:600; cursor:pointer;
    background:transparent; color:#8b949e; transition:all .15s;
  }
  .mode-btn.active { background:#21262d; color:#e6edf3; }

  /* status */
  #status { font-size:11px; color:#8b949e; }
  #status.live   { color:#f85149; font-weight:600; }
  #status.paused { color:#d29922; font-weight:600; }
  #timer { font-size:11px; font-variant-numeric:tabular-nums; color:#58a6ff; }
  #count { font-size:11px; color:#8b949e; text-align:center; }

  /* buttons */
  .btn {
    flex:1; padding:7px 0; border:none; border-radius:6px;
    font-size:12px; font-weight:600; cursor:pointer; transition:background .15s;
  }
  .btn:disabled { opacity:.35; cursor:not-allowed; }
  #btn-start  { background:#b91c1c; color:#fff; }
  #btn-start:hover:not(:disabled)  { background:#dc2626; }
  #btn-pause  { background:#854d0e; color:#fef3c7; }
  #btn-pause:hover:not(:disabled)  { background:#a16207; }
  #btn-resume { background:#1e4d2b; color:#bbf7d0; }
  #btn-resume:hover:not(:disabled) { background:#166534; }
  #btn-stop   { background:#21262d; color:#e6edf3; border:1px solid #30363d; }
  #btn-stop:hover:not(:disabled)   { background:#30363d; }

  /* inputs */
  .inp {
    background:#161b22; border:1px solid #30363d; border-radius:5px;
    color:#e6edf3; padding:5px 7px; font-size:11px; outline:none; width:100%;
  }
  .inp:focus   { border-color:#58a6ff; }
  .inp:disabled { opacity:.4; }

  /* window picker */
  #win-select {
    flex:1; background:#161b22; border:1px solid #30363d;
    border-radius:5px; color:#e6edf3; padding:4px 6px;
    font-size:11px; outline:none; min-width:0;
  }
  #win-select:focus { border-color:#58a6ff; }
  #btn-refresh {
    background:#21262d; border:1px solid #30363d; border-radius:5px;
    color:#8b949e; font-size:13px; cursor:pointer; padding:0 7px;
  }
  #btn-refresh:hover { color:#e6edf3; }
  #pin-status { font-size:10px; color:#3fb950; min-height:13px; padding-left:2px; }

  /* capture button */
  #btn-shot {
    width:100%; padding:11px; background:#238636; border:none; border-radius:6px;
    color:#fff; font-size:15px; font-weight:600; cursor:pointer; transition:background .15s;
  }
  #btn-shot:hover:not(:disabled) { background:#2ea043; }
  #btn-shot:disabled { background:#21262d; color:#484f58; cursor:not-allowed; }
  #btn-shot:active:not(:disabled) { transform:scale(.97); }

  /* audio meter */
  #meter-bar-wrap { height:8px; background:#21262d; border-radius:4px; overflow:hidden; }
  #meter-bar { height:100%; width:0%; background:#238636; border-radius:4px; transition:width .07s linear; }
  #audio-status { font-size:10px; color:#8b949e; }
  #btn-mic-test {
    background:#21262d; border:1px solid #30363d; border-radius:5px;
    color:#8b949e; font-size:10px; cursor:pointer; padding:3px 8px; white-space:nowrap;
  }
  #btn-mic-test.active { color:#3fb950; border-color:#238636; }
  #btn-mic-test:hover  { color:#e6edf3; }
  /* gain slider */
  #gain-row { display:none; align-items:center; gap:6px; margin-top:2px; }
  #gain-slider {
    flex:1; accent-color:#58a6ff; cursor:pointer; height:4px;
  }
  #gain-label { font-size:10px; color:#58a6ff; min-width:28px; text-align:right; }

  /* video-mode note */
  .video-note {
    background:#0d1f2d; border:1px solid #1d4ed8; border-radius:6px;
    padding:.5rem .65rem; font-size:.72rem; color:#93c5fd; line-height:1.55;
  }

  /* done banner */
  #done-banner {
    display:none; background:#0f2918; border:1px solid #238636; border-radius:6px;
    padding:.5rem .75rem; font-size:.78rem; color:#3fb950; line-height:1.55; text-align:center;
  }

  #hotkey  { font-size:10px; color:#484f58; text-align:center; }
  #feedback { font-size:11px; color:#f85149; text-align:center; min-height:14px; }
  #flash { position:fixed; inset:0; background:#fff; opacity:0; pointer-events:none; transition:opacity .08s; }
</style>
</head>
<body>
<div id="flash"></div>

<!-- Mode toggle -->
<div class="mode-toggle" id="mode-toggle">
  <button class="mode-btn active" id="mbtn-screenshots" onclick="setMode('screenshots')">📸 Screenshots</button>
  <button class="mode-btn"        id="mbtn-video"        onclick="setMode('video')">⏺ Video</button>
  <button class="mode-btn"        id="mbtn-combo"        onclick="setMode('combo')">🎬+📸 Combo</button>
  <button class="mode-btn"        id="mbtn-audio"        onclick="setMode('audio')">🎙 Audio</button>
</div>

<!-- Status row -->
<div class="row">
  <span id="status">No session</span>
  <span style="flex:1"></span>
  <span id="timer"></span>
</div>
<div id="count"></div>

<hr>

<!-- Session name -->
<div id="name-section" class="col">
  <span class="lbl">Session name</span>
  <input id="session-name" class="inp" type="text" placeholder="e.g. Configure SD-WAN Policy" maxlength="80">
</div>

<!-- Video mode note (hidden in screenshot mode) -->
<div id="video-note" class="video-note" style="display:none">
  🎙 Just talk through your steps naturally while recording.<br>
  AI will extract key frames and write the guide for you — no manual screenshots needed.<br>
  <strong>Tip:</strong> narrate what you're doing as you go.
</div>

<!-- Start controls -->
<div class="row" id="ctrl-row-1">
  <button class="btn" id="btn-start" onclick="startSession()">📸 Start Session</button>
</div>
<div class="row" id="ctrl-row-2" style="display:none">
  <button class="btn" id="btn-pause"  onclick="pauseRec()" style="display:none">⏸ Pause</button>
  <button class="btn" id="btn-resume" onclick="resumeRec()" style="display:none">▶ Resume</button>
  <button class="btn" id="btn-stop"   onclick="stopSession()">⏹ End</button>
</div>

<!-- Done banner (shown after video session ends) -->
<div id="done-banner">
  ✓ Session saved!<br>
  Go to the <strong>Sessions</strong> tab to review, or <strong>Ingest</strong> to generate your guide.
</div>

<hr>

<!-- Audio meter -->
<div class="col" id="audio-section">
  <div class="row">
    <span class="lbl" style="flex:1">Microphone</span>
    <button id="btn-mic-test" onclick="toggleMicTest()">🎙 Test mic</button>
  </div>
  <div class="row" style="margin-top:4px">
    <span class="lbl" style="white-space:nowrap">Audio source</span>
    <select id="audio-device-select" style="flex:1;background:#21262d;color:#e6edf3;border:1px solid #30363d;border-radius:4px;font-size:11px;padding:2px 4px;">
      <option value="0">Loading…</option>
    </select>
  </div>
  <div id="meter-bar-wrap"><div id="meter-bar"></div></div>
  <div id="gain-row">
    <span class="lbl" style="white-space:nowrap">Input gain</span>
    <input id="gain-slider" type="range" min="0.1" max="2" step="0.05" value="1" oninput="updateGain(this.value)">
    <span id="gain-label">1.0×</span>
  </div>
  <div id="audio-status">Click "Test mic" to check your microphone level</div>
</div>

<hr>

<!-- Window picker -->
<div class="col" id="window-section">
  <span class="lbl">Capture window</span>
  <div class="row">
    <select id="win-select">
      <option value="">-- Full screen --</option>
    </select>
    <button id="btn-refresh" onclick="loadWindows()" title="Refresh windows">↺</button>
  </div>
  <div id="pin-status"></div>
</div>

<!-- Screenshot controls (hidden in video mode) -->
<div id="screenshot-controls">
  <input id="label" class="inp" type="text" placeholder="Step label (optional — Enter = capture)" disabled>
  <button id="btn-shot" disabled onclick="snap()">📸 Capture</button>
  <button id="btn-voice" onclick="toggleVoiceCapture()" title="Say 'Capture' to snap a screenshot" style="background:#21262d;border:1px solid #30363d;border-radius:5px;color:#8b949e;font-size:11px;cursor:pointer;padding:5px 10px;width:100%">🎤 Voice capture: off</button>
  <div id="hotkey">or press ⌘⇧S from any app</div>
</div>

<div id="feedback"></div>

<script>
let _sid    = null;
let _mode   = 'screenshots';   // 'screenshots' | 'video' | 'combo' | 'audio'
let _paused = false;
let _running = false;
let _timer  = null;
let _elapsed = 0;

// mic test state
let _micStream = null, _micCtx = null, _micAnalyser = null, _micGain = null;
let _micActive = false, _micRaf = null, _meterSmooth = 0;

function fmt(s) {
  const m = Math.floor(s/60), sec = Math.floor(s%60);
  return String(m).padStart(2,'0') + ':' + String(sec).padStart(2,'0');
}

// ── Mode toggle ───────────────────────────────────────────────
function setMode(mode) {
  if (_running) return;
  _mode = mode;
  ['screenshots','video','combo','audio'].forEach(m =>
    document.getElementById('mbtn-' + m).classList.toggle('active', mode === m)
  );
  const hasShots = mode === 'screenshots' || mode === 'combo' || mode === 'audio';
  const hasVideo = mode === 'video' || mode === 'combo';
  document.getElementById('video-note').style.display           = hasVideo  ? 'block' : 'none';
  document.getElementById('screenshot-controls').style.display  = hasShots  ? 'flex'  : 'none';
  document.getElementById('screenshot-controls').style.flexDirection = 'column';
  document.getElementById('screenshot-controls').style.gap      = '6px';
  const labels = {screenshots: '📸 Start Session', video: '⏺ Start Recording',
                  combo: '🎬+📸 Start Combo', audio: '🎙 Start Audio Session'};
  document.getElementById('btn-start').textContent = labels[mode] || '▶ Start';
}

// ── Session start / stop ──────────────────────────────────────
async function startSession() {
  const name = document.getElementById('session-name').value.trim() || 'Untitled Session';
  const winSel = document.getElementById('win-select');
  const windowId = winSel.value ? parseInt(winSel.value) : null;
  document.getElementById('feedback').textContent = '';
  document.getElementById('done-banner').style.display = 'none';
  try {
    const res = await fetch('/api/record/start', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({mode: _mode, audio: true, name, window_id: windowId})
    });
    const d = await res.json();
    if (d.error) { document.getElementById('feedback').textContent = '✗ ' + d.error; return; }
    setRunning(true, d.session_id, false);
    _elapsed = 0;
    _timer = setInterval(() => { _elapsed++; document.getElementById('timer').textContent = fmt(_elapsed); }, 1000);
    await pinWindow();
  } catch(e) {
    document.getElementById('feedback').textContent = '✗ ' + e;
  }
}

async function stopSession() {
  if (!_sid) return;
  stopVoiceCapture();   // stop voice listener if active
  try {
    await fetch('/api/record/stop', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({session_id: _sid})
    });
  } catch(e) {}
  const showBanner = _mode === 'video' || _mode === 'combo' || _mode === 'audio';
  setRunning(false, null, false);
  _sid = null;
  if (showBanner) {
    document.getElementById('done-banner').style.display = 'block';
  }
}

function setRunning(yes, sid, paused) {
  _running = yes;
  _paused  = !!paused;
  if (sid) _sid = sid;

  document.getElementById('session-name').disabled = yes;
  document.getElementById('name-section').style.display = yes ? 'none' : 'flex';
  document.getElementById('mode-toggle').style.opacity  = yes ? '.4' : '1';
  document.getElementById('mode-toggle').style.pointerEvents = yes ? 'none' : '';

  document.getElementById('ctrl-row-1').style.display = yes ? 'none' : 'flex';
  document.getElementById('ctrl-row-2').style.display = yes ? 'flex' : 'none';

  // pause/resume only for screenshot / combo / audio modes
  const hasShots = _mode === 'screenshots' || _mode === 'combo' || _mode === 'audio';
  const showPauseResume = yes && hasShots;
  const btnPause  = document.getElementById('btn-pause');
  const btnResume = document.getElementById('btn-resume');
  btnPause.style.display  = showPauseResume && !_paused ? 'flex' : 'none';
  btnResume.style.display = showPauseResume && _paused  ? 'flex' : 'none';

  const shotEnabled = yes && !_paused && hasShots;
  document.getElementById('btn-shot').disabled = !shotEnabled;
  document.getElementById('label').disabled    = !shotEnabled;
  // window picker stays enabled at all times

  const st = document.getElementById('status');
  if (!yes)         { st.className = '';       st.textContent = 'No session'; }
  else if (_paused) { st.className = 'paused'; st.textContent = '⏸ Paused'; }
  else if (_mode === 'video')  { st.className = 'live'; st.textContent = '⏺ Recording'; }
  else if (_mode === 'combo')  { st.className = 'live'; st.textContent = '🎬+📸 Recording'; }
  else if (_mode === 'audio')  { st.className = 'live'; st.textContent = '🎙 Recording audio'; }
  else              { st.className = 'live';   st.textContent = '● Capturing'; }

  if (!yes) {
    document.getElementById('timer').textContent = '';
    document.getElementById('count').textContent = '';
    clearInterval(_timer); _timer = null; _elapsed = 0;
  }
}

// ── Pause / Resume (screenshots mode only) ───────────────────
async function pauseRec() {
  if (!_sid) return;
  await fetch('/api/record/pause', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({session_id: _sid})});
  _paused = true;
  document.getElementById('btn-pause').style.display  = 'none';
  document.getElementById('btn-resume').style.display = 'flex';
  document.getElementById('btn-shot').disabled = true;
  document.getElementById('label').disabled    = true;
  const st = document.getElementById('status');
  st.className = 'paused'; st.textContent = '⏸ Paused';
  clearInterval(_timer);
}

async function resumeRec() {
  if (!_sid) return;
  await fetch('/api/record/resume', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({session_id: _sid})});
  _paused = false;
  document.getElementById('btn-pause').style.display  = 'flex';
  document.getElementById('btn-resume').style.display = 'none';
  document.getElementById('btn-shot').disabled = false;
  document.getElementById('label').disabled    = false;
  const st = document.getElementById('status');
  st.className = 'live'; st.textContent = '● Capturing';
  _timer = setInterval(() => { _elapsed++; document.getElementById('timer').textContent = fmt(_elapsed); }, 1000);
}

// ── Window picker ─────────────────────────────────────────────
async function loadWindows() {
  try {
    const res = await fetch('/api/windows');
    const wins = await res.json();
    const sel = document.getElementById('win-select');
    const prev = sel.value;
    sel.innerHTML = '<option value="">-- Full screen --</option>';
    wins.forEach(w => {
      const opt = document.createElement('option');
      opt.value = String(w.id);
      const short = w.title.length > 44 ? w.title.slice(0,42) + '\u2026' : w.title;
      opt.textContent = w.app + ' \u2014 ' + short;
      opt.title = w.title;
      if (String(w.id) === prev) opt.selected = true;
      sel.appendChild(opt);
    });
  } catch(e) {}
}

async function pinWindow() {
  const sel = document.getElementById('win-select');
  const wid = sel.value ? parseInt(sel.value) : null;
  const label = wid ? '\u2713 ' + sel.options[sel.selectedIndex].textContent.slice(0,42) : '';
  document.getElementById('pin-status').textContent = label;
  // If a session is already running, update it immediately on the server too
  if (_sid) {
    try {
      await fetch('/api/record/set-window', {
        method:'POST', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({window_id: wid, session_id: _sid})
      });
    } catch(e) {}
  }
}
document.getElementById('win-select').addEventListener('change', pinWindow);

// ── Manual capture (screenshots mode) ────────────────────────
async function snap() {
  if (!_running || _paused || _mode === 'video') return;
  const label = document.getElementById('label').value.trim();
  document.getElementById('label').value = '';
  const fl = document.getElementById('flash');
  fl.style.opacity = '1';
  setTimeout(() => { fl.style.opacity = '0'; }, 80);
  document.getElementById('feedback').textContent = '';
  try {
    const res = await fetch('/api/record/screenshot', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({label, session_id: _sid || ''})
    });
    const d = await res.json();
    if (d.error) {
      document.getElementById('feedback').textContent = '\u2717 ' + d.error;
    } else {
      document.getElementById('count').textContent = d.seq + ' screenshot' + (d.seq !== 1 ? 's' : '');
    }
  } catch(e) {
    document.getElementById('feedback').textContent = '\u2717 Request failed';
  }
}
document.getElementById('label').addEventListener('keydown', e => { if (e.key === 'Enter') snap(); });

// ── Mic test ──────────────────────────────────────────────────
async function toggleMicTest() { _micActive ? stopMicTest() : await startMicTest(); }

async function startMicTest() {
  try {
    _micStream = await navigator.mediaDevices.getUserMedia({audio:true, video:false});
    _micCtx = new AudioContext();
    _micAnalyser = _micCtx.createAnalyser();
    _micAnalyser.fftSize = 1024;
    _micAnalyser.smoothingTimeConstant = 0.8;
    _micGain = _micCtx.createGain();
    const src = _micCtx.createMediaStreamSource(_micStream);
    // chain: mic → gain → analyser
    src.connect(_micGain);
    _micGain.connect(_micAnalyser);
    // restore last-used gain value from slider
    const slider = document.getElementById('gain-slider');
    _micGain.gain.value = parseFloat(slider.value);
    _micActive = true;
    document.getElementById('btn-mic-test').classList.add('active');
    document.getElementById('btn-mic-test').textContent = '\uD83C\uDF99 Stop test';
    document.getElementById('audio-status').textContent = 'Listening\u2026 speak to see levels';
    document.getElementById('gain-row').style.display = 'flex';
    drawMeter();
  } catch(e) {
    document.getElementById('audio-status').textContent = '\u2717 Mic access denied: ' + e.message;
  }
}

function stopMicTest() {
  _micActive = false;
  if (_micRaf) { cancelAnimationFrame(_micRaf); _micRaf = null; }
  if (_micStream) { _micStream.getTracks().forEach(t => t.stop()); _micStream = null; }
  if (_micCtx) { _micCtx.close(); _micCtx = null; }
  _micGain = null;
  _meterSmooth = 0;
  document.getElementById('meter-bar').style.width = '0%';
  document.getElementById('meter-bar').style.background = '#238636';
  document.getElementById('btn-mic-test').classList.remove('active');
  document.getElementById('btn-mic-test').textContent = '\uD83C\uDF99 Test mic';
  document.getElementById('audio-status').textContent = 'Mic test stopped';
  document.getElementById('gain-row').style.display = 'none';
}

function updateGain(val) {
  const v = parseFloat(val);
  if (_micGain) _micGain.gain.value = v;
  document.getElementById('gain-label').textContent = v.toFixed(1) + '\u00d7';
}

function drawMeter() {
  if (!_micActive || !_micAnalyser) return;
  const data = new Uint8Array(_micAnalyser.frequencyBinCount);
  _micAnalyser.getByteFrequencyData(data);
  // RMS across all bins
  let sumSq = 0;
  for (let i = 0; i < data.length; i++) sumSq += data[i] * data[i];
  const rms = Math.sqrt(sumSq / data.length);
  const raw = Math.min(100, rms * 0.6);   // conservative multiplier
  // Heavy smoothing: rise 0.12, fall 0.04 — stays calm, no jumps
  _meterSmooth = raw > _meterSmooth
    ? _meterSmooth + (raw - _meterSmooth) * 0.12
    : _meterSmooth + (raw - _meterSmooth) * 0.04;
  const pct = Math.round(_meterSmooth);
  const bar = document.getElementById('meter-bar');
  bar.style.width = pct + '%';
  bar.style.background = pct < 60 ? '#238636' : pct < 85 ? '#d29922' : '#f85149';
  const st = document.getElementById('audio-status');
  if (pct < 8)       st.textContent = 'Silence \u2014 speak to test';
  else if (pct < 60) st.textContent = 'Good level \u2713';
  else if (pct < 85) st.textContent = 'Loud \u2014 consider moving back';
  else               st.textContent = '\u26a0 Clipping \u2014 too loud!';
  _micRaf = requestAnimationFrame(drawMeter);
}

// ── Voice capture ─────────────────────────────────────────────
let _voiceRecognition = null;
let _voiceActive = false;

function toggleVoiceCapture() {
  _voiceActive ? stopVoiceCapture() : startVoiceCapture();
}

function startVoiceCapture() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) {
    document.getElementById('feedback').textContent = '✗ Voice capture not supported in this browser (use Chrome/Edge)';
    return;
  }
  _voiceRecognition = new SR();
  _voiceRecognition.continuous     = true;
  _voiceRecognition.interimResults = true;
  _voiceRecognition.lang           = 'en-US';

  let _lastSnap = 0;   // debounce: prevent double-fire within 1.5s

  _voiceRecognition.onresult = (e) => {
    for (let i = e.resultIndex; i < e.results.length; i++) {
      const transcript = e.results[i][0].transcript.trim().toLowerCase();
      if (transcript.includes('capture') || transcript.includes('screenshot')) {
        const now = Date.now();
        if (now - _lastSnap > 1500) {
          _lastSnap = now;
          snap();
          // brief flash on the voice button so user knows it fired
          const btn = document.getElementById('btn-voice');
          btn.style.background = '#1a4731';
          setTimeout(() => { if (_voiceActive) btn.style.background = '#0d2b1f'; }, 400);
        }
      }
    }
  };

  _voiceRecognition.onerror = (e) => {
    if (e.error !== 'no-speech') {
      document.getElementById('feedback').textContent = '✗ Voice error: ' + e.error;
      stopVoiceCapture();
    }
  };

  _voiceRecognition.onend = () => {
    // auto-restart if still active (browser stops after silence)
    if (_voiceActive) _voiceRecognition.start();
  };

  _voiceRecognition.start();
  _voiceActive = true;
  const btn = document.getElementById('btn-voice');
  btn.textContent  = '🎤 Voice capture: listening…';
  btn.style.color  = '#3fb950';
  btn.style.borderColor = '#238636';
  btn.style.background  = '#0d2b1f';
}

function stopVoiceCapture() {
  _voiceActive = false;
  if (_voiceRecognition) { try { _voiceRecognition.stop(); } catch(e){} _voiceRecognition = null; }
  const btn = document.getElementById('btn-voice');
  if (!btn) return;
  btn.textContent   = '🎤 Voice capture: off';
  btn.style.color   = '#8b949e';
  btn.style.borderColor = '#30363d';
  btn.style.background  = '#21262d';
}

// ── Sync existing session on load ─────────────────────────────
async function syncExistingSession() {
  try {
    const res = await fetch('/api/record/status');
    const list = await res.json();
    const active = list.find(s => s.running);
    if (active) {
      setRunning(true, active.session_id, active.paused);
      _elapsed = Math.round(active.elapsed_s);
      if (!active.paused) {
        _timer = setInterval(() => { _elapsed++; document.getElementById('timer').textContent = fmt(_elapsed); }, 1000);
      }
      document.getElementById('count').textContent =
        active.screenshots + ' screenshot' + (active.screenshots !== 1 ? 's' : '');
    }
  } catch(e) {}
}

// initialise
setMode('screenshots');
loadWindows();
syncExistingSession();
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────
# API — Screenshot Repository

# ─────────────────────────────────────────────────────────────
# API — Screenshot Repository
# ─────────────────────────────────────────────────────────────

def _ss_dir() -> Path:
    d = _data_dir() / "screenshots"
    d.mkdir(parents=True, exist_ok=True)
    return d

def _ss_meta_path() -> Path:
    return _ss_dir() / "_meta.json"

def _load_ss_meta() -> dict:
    p = _ss_meta_path()
    if p.exists():
        try:
            return json.load(open(p))
        except Exception:
            pass
    return {}

def _save_ss_meta(meta: dict) -> None:
    json.dump(meta, open(_ss_meta_path(), "w"), indent=2)


@app.route("/api/screenshots")
def api_list_screenshots():
    """List all screenshots in the repository."""
    ss_dir = _ss_dir()
    meta = _load_ss_meta()
    items = []
    for f in sorted(ss_dir.glob("*.png")) + sorted(ss_dir.glob("*.jpg")) + sorted(ss_dir.glob("*.jpeg")):
        fname = f.name
        items.append({
            "filename": fname,
            "url": f"/api/screenshots/file/{fname}",
            "caption": meta.get(fname, {}).get("caption", ""),
            "size": f.stat().st_size,
            "mtime": f.stat().st_mtime,
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return jsonify(items)


@app.route("/api/screenshots/file/<filename>")
def api_screenshot_file(filename):
    """Serve a screenshot image."""
    from flask import send_from_directory
    return send_from_directory(str(_ss_dir()), filename)


@app.route("/api/screenshots/upload", methods=["POST"])
def api_screenshot_upload():
    """Upload one or more screenshots to the repository."""
    files = request.files.getlist("files")
    saved = []
    for f in files:
        ext = Path(f.filename).suffix.lower() or ".png"
        fname = f"{uuid.uuid4().hex[:10]}{ext}"
        dest = _ss_dir() / fname
        f.save(str(dest))
        saved.append({"filename": fname, "url": f"/api/screenshots/file/{fname}"})
    return jsonify(saved)


@app.route("/api/screenshots/<filename>/delete", methods=["POST"])
def api_screenshot_delete(filename):
    """Delete a screenshot from the repository."""
    p = _ss_dir() / filename
    if p.exists():
        p.unlink()
    meta = _load_ss_meta()
    meta.pop(filename, None)
    _save_ss_meta(meta)
    return jsonify({"ok": True})


@app.route("/api/screenshots/<filename>/annotate", methods=["POST"])
def api_screenshot_annotate(filename):
    """Overwrite a screenshot with an annotated version (base64 PNG from canvas)."""
    import base64
    try:
        data = request.json or {}
        b64 = data.get("image", "")
        if not b64:
            return jsonify({"error": "No image data"}), 400
        if "," in b64:
            b64 = b64.split(",", 1)[1]
        img_bytes = base64.b64decode(b64)
        p = _ss_dir() / filename
        p.write_bytes(img_bytes)
        return jsonify({"ok": True, "filename": filename})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/sessions/<sid>/screenshots/<filename>/annotate", methods=["POST"])
def api_session_screenshot_annotate(sid, filename):
    """Overwrite a session screenshot with an annotated version (base64 PNG)."""
    import base64
    try:
        data = request.json or {}
        b64 = data.get("image", "")
        if not b64:
            return jsonify({"error": "No image data"}), 400
        if "," in b64:
            b64 = b64.split(",", 1)[1]
        img_bytes = base64.b64decode(b64)
        rec_dir = _data_dir() / "sessions" / sid / "recording"
        p = rec_dir / filename
        if not p.exists():
            return jsonify({"error": "File not found"}), 404
        p.write_bytes(img_bytes)
        return jsonify({"ok": True, "sid": sid, "filename": filename})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/screenshots/<filename>/caption", methods=["POST"])
def api_screenshot_caption(filename):
    """AI-generate or manually set a caption for a screenshot."""
    data = request.json or {}
    manual = data.get("caption")
    meta = _load_ss_meta()
    if manual is not None:
        meta.setdefault(filename, {})["caption"] = manual
        _save_ss_meta(meta)
        return jsonify({"caption": manual})
    # AI vision caption
    p = _ss_dir() / filename
    if not p.exists():
        return jsonify({"error": "File not found"}), 404
    try:
        import base64
        img_b64 = base64.b64encode(p.read_bytes()).decode()
        ext = p.suffix.lstrip(".")
        caption = _run_async(_ai_caption_image(img_b64, ext))
        meta.setdefault(filename, {})["caption"] = caption
        _save_ss_meta(meta)
        return jsonify({"caption": caption})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


async def _ai_caption_image(img_b64: str, ext: str) -> str:
    """Call vision AI to generate a caption for an image."""
    from lab_guide_automator import ai_client as _ai
    client = _ai._make_http_client()
    token = _ai._get_copilot_token()
    model = settings.vision_model()
    payload = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/{ext};base64,{img_b64}"}},
                {"type": "text", "text": (
                    "You are writing captions for a Cisco lab guide. "
                    "Write a single concise caption (max 15 words) describing what this screenshot shows. "
                    "Focus on the UI element or action visible. No quotes, no period at the end."
                )},
            ]
        }],
        "max_tokens": 60,
    }
    resp = client.post(
        "https://api.githubcopilot.com/chat/completions",
        json=payload,
        headers={"Authorization": f"Bearer {token}"},
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip().strip('"')


# ── Step screenshot management ────────────────────────────────

@app.route("/api/guides/<guide_id>/step/<step_id>/screenshots", methods=["POST"])
def api_step_add_screenshot(guide_id, step_id):
    """Attach a repository screenshot to a step."""
    data = request.json or {}
    filename = data.get("filename", "")
    caption = data.get("caption", "")
    g = _load_guide(guide_id)
    step = g.get_step(step_id)
    if not step:
        return jsonify({"error": "Step not found"}), 404
    # Read caption from meta if not provided
    if not caption:
        meta = _load_ss_meta()
        caption = meta.get(filename, {}).get("caption", "")
    from lab_guide_automator.models import StepScreenshot
    step.screenshots.append(StepScreenshot(
        path=f"screenshots/{filename}",
        caption=caption,
        timestamp_s=0.0,
    ))
    g.touch()
    _save_guide(g)
    return jsonify([s.model_dump() for s in step.screenshots])


@app.route("/api/guides/<guide_id>/step/<step_id>/screenshots/<int:idx>", methods=["DELETE"])
def api_step_delete_screenshot(guide_id, step_id, idx):
    """Remove a screenshot from a step by index."""
    g = _load_guide(guide_id)
    step = g.get_step(step_id)
    if not step:
        return jsonify({"error": "Step not found"}), 404
    if idx < 0 or idx >= len(step.screenshots):
        return jsonify({"error": "Index out of range"}), 400
    step.screenshots.pop(idx)
    g.touch()
    _save_guide(g)
    return jsonify([s.model_dump() for s in step.screenshots])


@app.route("/api/guides/<guide_id>/step/<step_id>/screenshots/<int:idx>/caption", methods=["POST"])
def api_step_screenshot_caption(guide_id, step_id, idx):
    """Update the caption on a step's screenshot."""
    data = request.json or {}
    g = _load_guide(guide_id)
    step = g.get_step(step_id)
    if not step or idx >= len(step.screenshots):
        return jsonify({"error": "Not found"}), 404
    step.screenshots[idx].caption = data.get("caption", "")
    g.touch()
    _save_guide(g)
    return jsonify(step.screenshots[idx].model_dump())


@app.route("/api/guides/<guide_id>/step/<step_id>/screenshots/reorder", methods=["POST"])
def api_step_reorder_screenshots(guide_id, step_id):
    """Reorder screenshots: body = {order: [0,2,1]} (new index order)."""
    data = request.json or {}
    order = data.get("order", [])
    g = _load_guide(guide_id)
    step = g.get_step(step_id)
    if not step:
        return jsonify({"error": "Step not found"}), 404
    shots = step.screenshots
    if sorted(order) != list(range(len(shots))):
        return jsonify({"error": "Invalid order"}), 400
    step.screenshots = [shots[i] for i in order]
    g.touch()
    _save_guide(g)
    return jsonify([s.model_dump() for s in step.screenshots])


# ── Section screenshot management ─────────────────────────────

@app.route("/api/guides/<guide_id>/section/<section_id>/screenshots", methods=["POST"])
def api_section_add_screenshot(guide_id, section_id):
    """Attach a repository screenshot to a section."""
    data = request.json or {}
    filename = data.get("filename", "")
    caption = data.get("caption", "")
    g = _load_guide(guide_id)
    sec = g.get_section(section_id)
    if not sec:
        return jsonify({"error": "Section not found"}), 404
    if not caption:
        meta = _load_ss_meta()
        caption = meta.get(filename, {}).get("caption", "")
    from lab_guide_automator.models import StepScreenshot
    sec.screenshots.append(StepScreenshot(
        path=f"screenshots/{filename}",
        caption=caption,
        timestamp_s=0.0,
    ))
    g.touch()
    _save_guide(g)
    return jsonify([s.model_dump() for s in sec.screenshots])


@app.route("/api/guides/<guide_id>/section/<section_id>/screenshots/<int:idx>", methods=["DELETE"])
def api_section_delete_screenshot(guide_id, section_id, idx):
    g = _load_guide(guide_id)
    sec = g.get_section(section_id)
    if not sec or idx >= len(sec.screenshots):
        return jsonify({"error": "Not found"}), 404
    sec.screenshots.pop(idx)
    g.touch()
    _save_guide(g)
    return jsonify([s.model_dump() for s in sec.screenshots])


@app.route("/api/guides/<guide_id>/section/<section_id>/screenshots/<int:idx>/caption", methods=["POST"])
def api_section_screenshot_caption(guide_id, section_id, idx):
    data = request.json or {}
    g = _load_guide(guide_id)
    sec = g.get_section(section_id)
    if not sec or idx >= len(sec.screenshots):
        return jsonify({"error": "Not found"}), 404
    sec.screenshots[idx].caption = data.get("caption", "")
    g.touch()
    _save_guide(g)
    return jsonify(sec.screenshots[idx].model_dump())


@app.route("/api/guides/<guide_id>/section/<section_id>/screenshots/reorder", methods=["POST"])
def api_section_reorder_screenshots(guide_id, section_id):
    data = request.json or {}
    order = data.get("order", [])
    g = _load_guide(guide_id)
    sec = g.get_section(section_id)
    if not sec:
        return jsonify({"error": "Section not found"}), 404
    shots = sec.screenshots
    if sorted(order) != list(range(len(shots))):
        return jsonify({"error": "Invalid order"}), 400
    sec.screenshots = [shots[i] for i in order]
    g.touch()
    _save_guide(g)
    return jsonify([s.model_dump() for s in sec.screenshots])


# ── Section content blocks ────────────────────────────────────

@app.route("/api/guides/<guide_id>/section/<section_id>/blocks", methods=["GET"])
def api_section_blocks_get(guide_id, section_id):
    g = _load_guide(guide_id)
    sec = g.get_section(section_id)
    if not sec:
        return jsonify({"error": "Not found"}), 404
    return jsonify([b.model_dump() for b in sec.blocks])


@app.route("/api/guides/<guide_id>/section/<section_id>/blocks", methods=["POST"])
def api_section_block_add(guide_id, section_id):
    """Add a text or screenshot block at a given position (or end)."""
    data = request.json or {}
    block_type = data.get("type", "text")
    after_id = data.get("after_id")   # insert after this block id; None = append
    g = _load_guide(guide_id)
    sec = g.get_section(section_id)
    if not sec:
        return jsonify({"error": "Not found"}), 404
    from lab_guide_automator.models import ContentBlock
    block = ContentBlock(
        type=block_type,
        content=data.get("content", ""),
        path=data.get("path", ""),
        caption=data.get("caption", ""),
    )
    if after_id:
        idx = next((i for i, b in enumerate(sec.blocks) if b.id == after_id), None)
        if idx is not None:
            sec.blocks.insert(idx + 1, block)
        else:
            sec.blocks.append(block)
    else:
        sec.blocks.append(block)
    g.touch()
    _save_guide(g)
    return jsonify(block.model_dump())


@app.route("/api/guides/<guide_id>/section/<section_id>/blocks/<block_id>", methods=["POST"])
def api_section_block_update(guide_id, section_id, block_id):
    """Update content or caption of a block."""
    data = request.json or {}
    g = _load_guide(guide_id)
    sec = g.get_section(section_id)
    if not sec:
        return jsonify({"error": "Not found"}), 404
    block = next((b for b in sec.blocks if b.id == block_id), None)
    if not block:
        return jsonify({"error": "Block not found"}), 404
    if "content" in data:
        block.content = data["content"]
    if "caption" in data:
        block.caption = data["caption"]
    g.touch()
    _save_guide(g)
    return jsonify(block.model_dump())


@app.route("/api/guides/<guide_id>/section/<section_id>/blocks/<block_id>", methods=["DELETE"])
def api_section_block_delete(guide_id, section_id, block_id):
    g = _load_guide(guide_id)
    sec = g.get_section(section_id)
    if not sec:
        return jsonify({"error": "Not found"}), 404
    sec.blocks = [b for b in sec.blocks if b.id != block_id]
    g.touch()
    _save_guide(g)
    return jsonify({"ok": True})


@app.route("/api/guides/<guide_id>/section/<section_id>/blocks/reorder", methods=["POST"])
def api_section_blocks_reorder(guide_id, section_id):
    """Reorder blocks: body = {order: ['id1','id2',...]}"""
    data = request.json or {}
    order = data.get("order", [])
    g = _load_guide(guide_id)
    sec = g.get_section(section_id)
    if not sec:
        return jsonify({"error": "Not found"}), 404
    id_to_block = {b.id: b for b in sec.blocks}
    sec.blocks = [id_to_block[bid] for bid in order if bid in id_to_block]
    g.touch()
    _save_guide(g)
    return jsonify([b.model_dump() for b in sec.blocks])


@app.route("/api/guides/<guide_id>/section/<section_id>/blocks/<block_id>/attach-screenshot", methods=["POST"])
def api_section_block_attach_screenshot(guide_id, section_id, block_id):
    """Attach a repository screenshot to an existing screenshot block."""
    data = request.json or {}
    filename = data.get("filename", "")
    caption = data.get("caption", "")
    if not caption:
        meta = _load_ss_meta()
        caption = meta.get(filename, {}).get("caption", "")
    g = _load_guide(guide_id)
    sec = g.get_section(section_id)
    if not sec:
        return jsonify({"error": "Not found"}), 404
    block = next((b for b in sec.blocks if b.id == block_id), None)
    if not block:
        return jsonify({"error": "Block not found"}), 404
    block.path = f"screenshots/{filename}"
    block.caption = caption
    g.touch()
    _save_guide(g)
    return jsonify(block.model_dump())


# ─────────────────────────────────────────────────────────────
# API — Ingestion with SSE progress
# ─────────────────────────────────────────────────────────────

@app.route("/api/upload/video", methods=["POST"])
def api_upload_video():
    """Accept a video file upload, save it, return the saved path."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400
    upload_dir = _data_dir() / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w\-.]", "_", f.filename)
    dest = upload_dir / safe_name
    f.save(str(dest))
    return jsonify({"path": str(dest), "filename": safe_name, "size": dest.stat().st_size})


@app.route("/api/upload/document", methods=["POST"])
def api_upload_document():
    """Accept a document file upload (PDF/DOCX/MD/HTML), save it, return path."""
    if "file" not in request.files:
        return jsonify({"error": "No file provided"}), 400
    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400
    upload_dir = _data_dir() / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^\w\-.]", "_", f.filename)
    dest = upload_dir / safe_name
    f.save(str(dest))
    return jsonify({"path": str(dest), "filename": safe_name, "size": dest.stat().st_size})


@app.route("/api/upload/screenshots", methods=["POST"])
def api_upload_screenshots():
    """
    Accept multiple screenshot files, save them to a session folder, return folder path.
    Expects multipart with multiple files under the key 'files'.
    """
    files = request.files.getlist("files")
    if not files:
        return jsonify({"error": "No files provided"}), 400
    upload_dir = _data_dir() / "uploads" / f"screenshots_{uuid.uuid4().hex[:8]}"
    upload_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        if f.filename:
            safe_name = re.sub(r"[^\w\-.]", "_", f.filename)
            dest = upload_dir / safe_name
            f.save(str(dest))
            saved.append(safe_name)
    return jsonify({"folder_path": str(upload_dir), "count": len(saved), "files": saved})


@app.route("/api/ingest/video", methods=["POST"])
def api_ingest_video():
    data = request.json or {}
    video_path = data.get("video_path", "")
    lab_title = data.get("title", "Untitled Lab")
    session_id = data.get("session_id") or str(uuid.uuid4())[:8]
    interval = float(data.get("frame_interval", 5.0))

    if not Path(video_path).exists():
        return jsonify({"error": f"File not found: {video_path}"}), 400

    job_id = str(uuid.uuid4())[:8]
    q: queue.Queue = queue.Queue()
    _progress_queues[job_id] = q

    def run():
        session_dir = _data_dir() / "sessions" / session_id
        def progress(msg, *args):
            q.put({"type": "progress", "message": str(msg)})

        try:
            q.put({"type": "progress", "message": "Starting ingestion..."})
            guide = _run_async(ingest.ingest_recording(
                settings, Path(video_path), lab_title, session_dir,
                frame_interval_s=interval,
                progress_callback=progress,
            ))
            _save_guide(guide)
            q.put({"type": "done", "guide_id": guide.id, "title": guide.metadata.title,
                   "sections": len(guide.sections), "steps": guide.step_count()})
        except Exception as e:
            q.put({"type": "error", "message": str(e)})
        finally:
            q.put(None)  # sentinel

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/ingest/screenshots", methods=["POST"])
def api_ingest_screenshots():
    data = request.json or {}
    folder = data.get("folder_path", "")
    lab_title = data.get("title", "Untitled Lab")
    session_id = data.get("session_id") or str(uuid.uuid4())[:8]

    if not Path(folder).is_dir():
        return jsonify({"error": f"Not a directory: {folder}"}), 400

    job_id = str(uuid.uuid4())[:8]
    q: queue.Queue = queue.Queue()
    _progress_queues[job_id] = q

    screenshots = sorted(
        list(Path(folder).glob("*.png")) +
        list(Path(folder).glob("*.jpg")) +
        list(Path(folder).glob("*.jpeg"))
    )

    def run():
        session_dir = _data_dir() / "sessions" / session_id
        def progress(msg, *args):
            q.put({"type": "progress", "message": str(msg)})
        try:
            q.put({"type": "progress", "message": f"Processing {len(screenshots)} screenshots..."})
            guide = _run_async(ingest.ingest_screenshots(
                settings, screenshots, lab_title, session_dir,
                progress_callback=progress,
            ))
            _save_guide(guide)
            q.put({"type": "done", "guide_id": guide.id, "title": guide.metadata.title,
                   "sections": len(guide.sections), "steps": guide.step_count()})
        except Exception as e:
            q.put({"type": "error", "message": str(e)})
        finally:
            q.put(None)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/guides/<guide_id>/ingest-section", methods=["POST"])
def api_ingest_section(guide_id):
    """Generate a new section from screenshots and append it to an existing guide."""
    guide = _load_guide(guide_id)
    if guide is None:
        return jsonify({"error": "Guide not found"}), 404

    data = request.json or {}
    folder = data.get("folder_path", "")
    section_title = data.get("section_title", "New Section").strip() or "New Section"
    position = data.get("position")  # optional int index to insert at; None = append

    if not Path(folder).is_dir():
        return jsonify({"error": f"Not a directory: {folder}"}), 400

    screenshots = sorted(
        list(Path(folder).glob("*.png")) +
        list(Path(folder).glob("*.jpg")) +
        list(Path(folder).glob("*.jpeg"))
    )
    if not screenshots:
        return jsonify({"error": "No screenshots found in folder"}), 400

    job_id = str(uuid.uuid4())[:8]
    q: queue.Queue = queue.Queue()
    _progress_queues[job_id] = q

    def run():
        def progress(msg, *args):
            q.put({"type": "progress", "message": str(msg)})
        try:
            q.put({"type": "progress", "message": f"Processing {len(screenshots)} screenshot(s) for section '{section_title}'…"})
            new_section = _run_async(ingest.ingest_section(
                settings, screenshots,
                section_title=section_title,
                guide_context=guide.metadata.title,
                progress_callback=progress,
            ))
            # Reload guide fresh in case it was edited while the job ran
            g = _load_guide(guide_id)
            if g is None:
                raise RuntimeError("Guide was deleted while job was running")
            if position is not None and 0 <= int(position) <= len(g.sections):
                g.sections.insert(int(position), new_section)
            else:
                g.sections.append(new_section)
            _renumber_steps(g)
            _save_guide(g)
            q.put({"type": "done",
                   "section_id": new_section.id,
                   "section_title": new_section.title,
                   "steps": len(new_section.steps)})
        except Exception as e:
            q.put({"type": "error", "message": str(e)})
        finally:
            q.put(None)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/ingest/document", methods=["POST"])
def api_ingest_document():
    """Ingest an existing lab guide document (PDF, DOCX, MD, HTML)."""
    data = request.json or {}
    doc_path = data.get("document_path", "").strip()
    session_id = data.get("session_id") or str(uuid.uuid4())[:8]

    if not doc_path:
        return jsonify({"error": "document_path is required"}), 400
    p = Path(doc_path)
    if not p.exists():
        return jsonify({"error": f"File not found: {doc_path}"}), 400

    allowed = {".pdf", ".docx", ".md", ".markdown", ".html", ".htm", ".txt"}
    if p.suffix.lower() not in allowed:
        return jsonify({"error": f"Unsupported format: {p.suffix}. Supported: PDF, DOCX, MD, HTML, TXT"}), 400

    job_id = str(uuid.uuid4())[:8]
    q: queue.Queue = queue.Queue()
    _progress_queues[job_id] = q

    def run():
        from lab_guide_automator.ingest_document import ingest_document
        session_dir = _data_dir() / "sessions" / session_id

        def progress(msg, *args):
            q.put({"type": "progress", "message": str(msg)})

        try:
            guide = _run_async(ingest_document(settings, p, session_dir, progress))
            _save_guide(guide)
            q.put({"type": "done", "guide_id": guide.id, "title": guide.metadata.title,
                   "sections": len(guide.sections), "steps": guide.step_count()})
        except Exception as e:
            q.put({"type": "error", "message": str(e)})
        finally:
            q.put(None)

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/api/ingest/progress/<job_id>")
def api_ingest_progress(job_id):
    """SSE stream for ingestion progress."""
    q = _progress_queues.get(job_id)
    if not q:
        return jsonify({"error": "Job not found"}), 404

    @stream_with_context
    def generate():
        while True:
            try:
                event = q.get(timeout=30)
                if event is None:
                    yield _sse_event({"type": "end"})
                    break
                yield _sse_event(event)
                if event.get("type") in ("done", "error"):
                    yield _sse_event({"type": "end"})
                    break
            except queue.Empty:
                yield _sse_event({"type": "ping"})

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ─────────────────────────────────────────────────────────────
# API — Export
# ─────────────────────────────────────────────────────────────

@app.route("/api/guides/<guide_id>/export/<fmt>", methods=["POST"])
def api_export(guide_id, fmt):
    try:
        g = _load_guide(guide_id)
        exports_dir = _data_dir() / "exports"
        exports_dir.mkdir(exist_ok=True)

        if fmt == "markdown":
            p = exports_dir / f"{guide_id}.md"
            export_markdown(g, p)
        elif fmt == "pdf":
            p = exports_dir / f"{guide_id}.pdf"
            export_pdf(g, p)
        elif fmt == "html":
            p = exports_dir / f"{guide_id}.html"
            export_html(g, p, embed_screenshots=True)
        elif fmt == "docx":
            p = exports_dir / f"{guide_id}.docx"
            export_docx(g, p)
        elif fmt == "mkdocs":
            p = exports_dir / f"{guide_id}-mkdocs"
            export_mkdocs(g, p)
        else:
            return jsonify({"error": f"Unknown format: {fmt}"}), 400

        return jsonify({"path": str(p), "format": fmt})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/guides/<guide_id>/export/<fmt>/download")
def api_download(guide_id, fmt):
    """Download an already-exported file."""
    ext = {"markdown": "md", "pdf": "pdf", "html": "html", "docx": "docx"}.get(fmt)
    if not ext:
        return jsonify({"error": "Unknown format"}), 400
    exports_dir = _data_dir() / "exports"
    filename = f"{guide_id}.{ext}"
    return send_from_directory(exports_dir, filename, as_attachment=True)


# ─────────────────────────────────────────────────────────────
# Markdown preview
# ─────────────────────────────────────────────────────────────

def _preview_html(g) -> str:
    """Render a full in-dashboard HTML preview with live screenshot URLs."""
    import markdown as md_lib
    import os

    def _md(text: str) -> str:
        if not text:
            return ""
        return md_lib.markdown(text.strip(), extensions=["tables", "fenced_code", "nl2br"])

    def _img(path: str, caption: str = "") -> str:
        fname = os.path.basename(path)
        cap_attr = caption.replace('"', '&quot;')
        cap_html = f'<p class="prev-caption">{caption}</p>' if caption else ""
        return (
            f'<figure class="prev-figure">'
            f'<img src="/api/screenshots/file/{fname}" alt="{cap_attr}" '
            f'onerror="this.closest(\'figure\').style.display=\'none\'">'
            f'{cap_html}</figure>'
        )

    parts = []

    # ── Title / meta ──────────────────────────────────────────
    m = g.metadata
    parts.append(f'<h1>{m.title}</h1>')
    if m.subtitle:
        parts.append(f'<p style="font-size:1.05rem;color:#005073;margin-top:-.5rem">{m.subtitle}</p>')
    meta_bits = []
    if m.author:
        meta_bits.append(f"Author: {m.author}")
    if m.version:
        meta_bits.append(f"v{m.version}")
    if m.tags:
        tags = " ".join(f'<span class="prev-tag">{t}</span>' for t in m.tags)
        meta_bits.append(tags)
    if meta_bits:
        parts.append(f'<p class="prev-meta">{" &nbsp;·&nbsp; ".join(meta_bits)}</p>')

    if g.introduction:
        parts.append('<h2>Introduction</h2>')
        parts.append(_md(g.introduction))

    if g.learning_objectives:
        parts.append('<h2>Learning Objectives</h2><ul>')
        for obj in g.learning_objectives:
            parts.append(f'<li>{obj.text}</li>')
        parts.append('</ul>')

    # ── Sections ──────────────────────────────────────────────
    for sec in g.sections:
        parts.append(f'<h2>{sec.title}</h2>')
        if sec.overview:
            parts.append(_md(sec.overview))

        blocks = getattr(sec, "blocks", []) or []
        for blk in blocks:
            if blk.type == "text":
                parts.append(_md(blk.content or ""))
            elif blk.type == "screenshot" and blk.path:
                parts.append(_img(blk.path, blk.caption or ""))

        if not blocks:
            for ss in getattr(sec, "screenshots", []):
                parts.append(_img(ss.path, ss.caption or ""))

        # Steps — numbered per section
        for i, step in enumerate(sec.steps, 1):
            parts.append(
                f'<div class="prev-step">'
                f'<h3><span class="prev-step-num">{i}</span> {step.title}</h3>'
            )
            parts.append(_md(step.instruction))

            if step.code_blocks:
                for cb in step.code_blocks:
                    parts.append(f'<pre><code>{cb}</code></pre>')

            for ss in step.screenshots:
                parts.append(_img(ss.path, ss.caption or ""))

            if step.expected_result:
                parts.append(
                    f'<div class="prev-expected">'
                    f'<strong>Expected Result:</strong> {step.expected_result}'
                    f'</div>'
                )
            if step.notes:
                parts.append(
                    f'<div class="prev-note"><strong>Note:</strong> {step.notes}</div>'
                )
            parts.append('</div>')

    if g.conclusion:
        parts.append('<h2>Conclusion</h2>')
        parts.append(_md(g.conclusion))

    return "\n".join(parts)


@app.route("/api/guides/<guide_id>/preview")
def api_preview(guide_id):
    try:
        g = _load_guide(guide_id)
        return jsonify({"html": _preview_html(g)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─────────────────────────────────────────────────────────────
# HTML — Single-page app
# ─────────────────────────────────────────────────────────────

def _render_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lab Guide Automator</title>
<!-- Quill rich-text editor -->
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/quill@2.0.3/dist/quill.snow.css">
<script src="https://cdn.jsdelivr.net/npm/quill@2.0.3/dist/quill.js"></script>
<style>
  :root {
    --bg: #0d1117; --surface: #161b22; --surface2: #1e2530;
    --border: #30363d; --text: #c9d1d9; --text2: #8b949e;
    --accent: #00bceb; --accent2: #0075a2; --green: #3fb950;
    --red: #f85149; --yellow: #d29922; --purple: #a371f7;
    --radius: 8px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; font-size: 14px; height: 100vh; display: flex; flex-direction: column; }

  /* ── Top nav ── */
  .topnav { background: var(--surface); border-bottom: 1px solid var(--border); padding: 0 1.5rem; height: 52px; display: flex; align-items: center; gap: 1.5rem; flex-shrink: 0; }
  .topnav .logo { color: var(--accent); font-weight: 700; font-size: 1rem; letter-spacing: .02em; white-space: nowrap; }
  .nav-tabs { display: flex; gap: 0; }
  .nav-tab { background: none; border: none; border-bottom: 2px solid transparent; color: var(--text2); cursor: pointer; padding: .6rem 1rem; font-size: .85rem; transition: color .15s, border-color .15s; }
  .nav-tab:hover { color: var(--text); }
  .nav-tab.active { color: var(--accent); border-bottom-color: var(--accent); }
  .topnav-right { margin-left: auto; display: flex; align-items: center; gap: .75rem; }
  .pill { background: var(--surface2); border: 1px solid var(--border); border-radius: 12px; padding: .25rem .75rem; font-size: .78rem; color: var(--text2); }
  .pill.green { background: #1a2e1a; border-color: var(--green); color: var(--green); }

  /* ── Layout ── */
  .main { display: flex; flex: 1; overflow: hidden; }
  .sidebar { width: 280px; flex-shrink: 0; background: var(--surface); border-right: 1px solid var(--border); display: flex; flex-direction: column; overflow: hidden; }
  .content { flex: 1; overflow: auto; padding: 1.5rem; }

  /* ── Sidebar ── */
  .sidebar-header { padding: .75rem 1rem; border-bottom: 1px solid var(--border); display: flex; align-items: center; justify-content: space-between; }
  .sidebar-header span { font-size: .8rem; font-weight: 600; color: var(--text2); text-transform: uppercase; letter-spacing: .06em; }
  .guide-list { overflow-y: auto; flex: 1; }
  .guide-item { padding: .75rem 1rem; border-bottom: 1px solid var(--border); cursor: pointer; transition: background .12s; }
  .guide-item:hover { background: var(--surface2); }
  .guide-item.active { background: var(--surface2); border-left: 3px solid var(--accent); }
  .guide-item .gtitle { font-weight: 500; font-size: .88rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .guide-item .gmeta { font-size: .75rem; color: var(--text2); margin-top: .2rem; }
  .empty-state { padding: 2rem 1rem; text-align: center; color: var(--text2); font-size: .85rem; }

  /* ── Buttons ── */
  .btn { border: none; border-radius: var(--radius); cursor: pointer; font-size: .82rem; font-weight: 500; padding: .45rem .9rem; transition: opacity .15s, transform .1s; }
  .btn:active { transform: scale(.97); }
  .btn-primary { background: var(--accent); color: #000; }
  .btn-primary:hover { opacity: .85; }
  .btn-secondary { background: var(--surface2); border: 1px solid var(--border); color: var(--text); }
  .btn-secondary:hover { border-color: var(--accent); color: var(--accent); }
  .btn-danger { background: #2d1616; border: 1px solid var(--red); color: var(--red); }
  .btn-danger:hover { background: var(--red); color: #fff; }
  .btn-sm { padding: .3rem .6rem; font-size: .76rem; }
  .btn-icon { background: none; border: none; cursor: pointer; color: var(--text2); padding: .25rem; border-radius: 4px; font-size: .9rem; }
  .btn-icon:hover { color: var(--accent); background: var(--surface2); }
  .btn-ai { background: linear-gradient(135deg, #0d3b6e, #1a1a4e); border: 1px solid var(--purple); color: var(--purple); }
  .btn-ai:hover { background: linear-gradient(135deg, #1a4d8c, #2a2a6e); }
  .btn-rec { background: #2d1616; border: 1px solid var(--red); color: var(--red); }
  .btn-rec:hover { background: var(--red); color: #fff; }
  .btn-rec.recording { background: var(--red); color: #fff; animation: pulse-rec 1s infinite; }
  @keyframes pulse-rec { 0%,100%{opacity:1} 50%{opacity:.7} }

  /* ── Cards ── */
  .card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 1.25rem; margin-bottom: 1rem; }
  .card-title { font-weight: 600; font-size: .95rem; margin-bottom: 1rem; display: flex; align-items: center; gap: .5rem; }
  .card-title .icon { font-size: 1.1rem; }

  /* ── Tabs (inner) ── */
  .tab-bar { display: flex; gap: 0; border-bottom: 1px solid var(--border); margin-bottom: 1.25rem; }
  .tab-btn { background: none; border: none; border-bottom: 2px solid transparent; color: var(--text2); cursor: pointer; padding: .5rem 1rem; font-size: .82rem; transition: color .15s, border-color .15s; }
  .tab-btn:hover { color: var(--text); }
  .tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }
  .tab-panel { display: none; }
  .tab-panel.active { display: block; }

  /* ── Forms ── */
  .form-row { display: flex; gap: .75rem; align-items: flex-end; margin-bottom: .75rem; flex-wrap: wrap; }
  .form-group { display: flex; flex-direction: column; gap: .3rem; flex: 1; min-width: 140px; }
  .form-group label { font-size: .75rem; color: var(--text2); }
  input, select, textarea { background: var(--surface2); border: 1px solid var(--border); border-radius: 6px; color: var(--text); font-size: .85rem; padding: .45rem .7rem; width: 100%; transition: border-color .15s; }
  input:focus, select:focus, textarea:focus { outline: none; border-color: var(--accent); }
  textarea { resize: vertical; min-height: 80px; font-family: inherit; }

  /* ── Guide editor ── */
  .guide-header { margin-bottom: 1.25rem; }
  .guide-title-row { display: flex; align-items: center; gap: .75rem; margin-bottom: .5rem; }
  .guide-title-row h2 { font-size: 1.3rem; font-weight: 700; flex: 1; }
  .badge { border-radius: 10px; font-size: .72rem; padding: .15rem .55rem; font-weight: 600; }
  .badge-blue { background: #0d2a3b; border: 1px solid var(--accent); color: var(--accent); }
  .badge-green { background: #1a2e1a; border: 1px solid var(--green); color: var(--green); }
  .badge-yellow { background: #2d2600; border: 1px solid var(--yellow); color: var(--yellow); }
  .meta-row { display: flex; gap: .75rem; flex-wrap: wrap; font-size: .78rem; color: var(--text2); }
  .meta-row span { display: flex; align-items: center; gap: .3rem; }

  /* ── Sections + Steps ── */
  .section-block { margin-bottom: 1.5rem; }
  .section-header { display: flex; align-items: center; gap: .5rem; padding: .6rem .75rem; background: var(--surface2); border: 1px solid var(--border); border-radius: var(--radius); margin-bottom: .5rem; cursor: pointer; }
  .section-header:hover { border-color: var(--accent); }
  .sec-drag-handle { color: var(--text2); cursor: grab; font-size: 1rem; padding: 0 .15rem; flex-shrink: 0; }
  .sec-drag-handle:active { cursor: grabbing; }
  .section-num { background: var(--accent2); color: var(--accent); border-radius: 4px; padding: 1px 7px; font-size: .75rem; font-weight: 700; flex-shrink: 0; }
  .section-title { flex: 1; }
  .sec-title-input { flex: 1; font-size: .92rem; font-weight: 600; background: var(--surface); border: 1px solid var(--accent); border-radius: 4px; color: var(--text); padding: 2px 6px; outline: none; }
  .section-count { font-size: .75rem; color: var(--text2); }
  .section-block.sec-drag-over { outline: 2px dashed var(--accent); outline-offset: 2px; border-radius: var(--radius); }
  .sec-overview-row { display: flex; align-items: center; gap: .5rem; padding: .3rem .75rem; font-size: .8rem; color: var(--text2); border-bottom: 1px solid var(--border); min-height: 2rem; }
  .sec-overview-text { flex: 1; font-style: italic; }
  .steps-container { padding-left: 1rem; }
  .step-card { background: var(--surface); border: 1px solid var(--border); border-radius: 6px; padding: .85rem 1rem; margin-bottom: .5rem; transition: border-color .15s; }
  .step-card:hover { border-color: var(--border); }
  .step-card.editing { border-color: var(--accent); }
  .step-header { display: flex; align-items: center; gap: .5rem; margin-bottom: .4rem; }
  .step-num { background: var(--accent2); color: var(--accent); border-radius: 50%; width: 22px; height: 22px; display: flex; align-items: center; justify-content: center; font-size: .72rem; font-weight: 700; flex-shrink: 0; }
  .step-title-text { font-weight: 500; font-size: .88rem; flex: 1; }
  .step-body { font-size: .83rem; color: var(--text2); line-height: 1.55; margin-bottom: .4rem; white-space: pre-wrap; }
  .expected { background: #1a2e1a; border-left: 3px solid var(--green); padding: .35rem .6rem; border-radius: 0 4px 4px 0; font-size: .78rem; color: #7ee787; margin-top: .4rem; }

  /* ── Step screenshot panel ── */
  .step-screenshots { margin-top: .6rem; border-top: 1px solid var(--border); padding-top: .6rem; }
  .step-screenshots-label { font-size: .75rem; font-weight: 600; color: var(--text2); margin-bottom: .4rem; display: flex; align-items: center; gap: .5rem; }
  .ss-thumb-row { display: flex; flex-wrap: wrap; gap: .5rem; margin-bottom: .4rem; }
  .ss-thumb { position: relative; width: 110px; cursor: pointer; border-radius: 5px; overflow: hidden; border: 1px solid var(--border); background: var(--surface2); flex-shrink: 0; }
  .ss-thumb img { width: 110px; height: 72px; object-fit: cover; display: block; }
  .ss-thumb-caption { font-size: .65rem; color: var(--text2); padding: 2px 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .ss-thumb-actions { position: absolute; top: 2px; right: 2px; display: flex; gap: 2px; }
  .ss-thumb-btn { background: rgba(0,0,0,.7); color: #fff; border: none; border-radius: 3px; font-size: .65rem; padding: 1px 4px; cursor: pointer; line-height: 1.4; }
  .ss-thumb-btn:hover { background: var(--accent); }
  .ss-thumb.drag-over { border-color: var(--accent); }

  /* ── Section content blocks ── */
  .block-list { display: flex; flex-direction: column; gap: 0; margin: .5rem .75rem .25rem; }
  .block-item { border: 1px solid var(--border); border-radius: 6px; background: var(--surface); margin-bottom: .4rem; }
  .block-item.block-text { }
  .block-item.block-screenshot { display: flex; align-items: flex-start; gap: .75rem; padding: .6rem; }
  .block-text-body { padding: .5rem .75rem; font-size: .83rem; color: var(--text2); white-space: pre-wrap; line-height: 1.55; cursor: pointer; min-height: 2rem; }
  .block-text-body:hover { background: var(--surface2); border-radius: 5px; }
  .block-text-edit { display: none; padding: .5rem .75rem; }
  .block-text-edit textarea { width: 100%; box-sizing: border-box; min-height: 80px; resize: vertical; }
  .block-ss-thumb { width: 160px; flex-shrink: 0; border-radius: 5px; overflow: hidden; border: 1px solid var(--border); cursor: pointer; }
  .block-ss-thumb img { width: 160px; height: 100px; object-fit: cover; display: block; }
  .block-ss-caption { font-size: .72rem; color: var(--text2); padding: 2px 5px; }
  .block-ss-meta { flex: 1; display: flex; flex-direction: column; gap: .35rem; }
  .block-ss-cap-input { font-size: .8rem; width: 100%; box-sizing: border-box; }
  .block-actions { display: flex; gap: .3rem; align-items: center; flex-wrap: wrap; }
  .block-divider { display: flex; align-items: center; gap: .4rem; margin: .15rem 0; }
  .block-divider-line { flex: 1; height: 1px; background: var(--border); }
  .block-add-btn { font-size: .7rem; padding: 2px 8px; white-space: nowrap; opacity: .7; }
  .block-add-btn:hover { opacity: 1; }
  .block-drag-handle { cursor: grab; color: var(--text2); font-size: .9rem; padding: 0 4px; user-select: none; }

  /* ── Screenshot Repository modal ── */
  .repo-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px,1fr)); gap: .75rem; max-height: 55vh; overflow-y: auto; padding-right: 4px; }
  .repo-thumb { border: 2px solid var(--border); border-radius: 6px; overflow: hidden; cursor: pointer; background: var(--surface2); transition: border-color .15s; position: relative; }
  .repo-thumb:hover, .repo-thumb.selected { border-color: var(--accent); }
  .repo-thumb img { width: 100%; height: 90px; object-fit: cover; display: block; }
  .repo-thumb-cap { font-size: .68rem; padding: 4px 6px; color: var(--text2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; background: var(--surface); }
  .repo-thumb-actions { position: absolute; top: 3px; right: 3px; display: none; gap: 2px; }
  .repo-thumb:hover .repo-thumb-actions { display: flex; }
  .repo-ai-badge { position: absolute; top: 3px; left: 3px; background: rgba(0,0,0,.7); color: #00bdeb; font-size: .6rem; padding: 1px 4px; border-radius: 3px; }
  .repo-filter { display: flex; gap: .5rem; align-items: center; margin-bottom: .75rem; }
  .repo-filter input { flex: 1; }

  /* ── Objectives ── */
  .obj-list { display: flex; flex-direction: column; gap: .4rem; }
  .obj-item { display: flex; align-items: flex-start; gap: .5rem; padding: .5rem .75rem; background: var(--surface2); border-radius: 6px; border: 1px solid var(--border); }
  .bloom-badge { font-size: .68rem; padding: .1rem .4rem; border-radius: 8px; flex-shrink: 0; margin-top: 1px; font-weight: 600; text-transform: uppercase; }
  .bloom-apply { background: #0d3b2b; color: #3fb950; border: 1px solid #3fb950; }
  .bloom-understand { background: #0d2a3b; color: var(--accent); border: 1px solid var(--accent); }
  .bloom-analyze { background: #2d2600; color: var(--yellow); border: 1px solid var(--yellow); }
  .bloom-create { background: #2d1650; color: var(--purple); border: 1px solid var(--purple); }
  .bloom-evaluate { background: #2d1616; color: var(--red); border: 1px solid var(--red); }
  .bloom-remember { background: #1e2530; color: var(--text2); border: 1px solid var(--border); }

  /* ── Export panel ── */
  .export-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: .75rem; }
  .export-card { background: var(--surface2); border: 1px solid var(--border); border-radius: var(--radius); padding: 1rem; text-align: center; cursor: pointer; transition: border-color .15s, transform .1s; }
  .export-card:hover { border-color: var(--accent); transform: translateY(-2px); }
  .export-card .export-icon { font-size: 2rem; margin-bottom: .5rem; }
  .export-card .export-label { font-size: .82rem; font-weight: 600; }
  .export-card .export-desc { font-size: .72rem; color: var(--text2); margin-top: .2rem; }
  .export-card.loading { opacity: .6; pointer-events: none; }

  /* ── Record panel ── */
  .record-status { display: flex; align-items: center; gap: .75rem; padding: .75rem; background: var(--surface2); border-radius: var(--radius); border: 1px solid var(--border); margin-bottom: .75rem; }
  .rec-dot { width: 10px; height: 10px; border-radius: 50%; background: var(--text2); flex-shrink: 0; }
  .rec-dot.active { background: var(--red); animation: pulse-rec 1s infinite; }

  /* ── Progress / logs ── */
  .progress-box { background: #0a0e13; border: 1px solid var(--border); border-radius: var(--radius); padding: .75rem 1rem; font-family: monospace; font-size: .78rem; color: var(--green); min-height: 120px; max-height: 260px; overflow-y: auto; }
  .progress-line { margin-bottom: .2rem; }
  .progress-line.error { color: var(--red); }
  .progress-line.done { color: var(--accent); }
  .drop-zone { border: 2px dashed var(--border); border-radius: var(--radius); padding: 2rem 1.5rem; text-align: center; cursor: pointer; transition: border-color .2s, background .2s; user-select: none; }
  .drop-zone:hover, .drop-zone.drag-over { border-color: var(--accent); background: rgba(0,168,255,.06); }
  .drop-icon { font-size: 2.2rem; line-height: 1; margin-bottom: .5rem; }
  .drop-label { font-size: .92rem; color: var(--text); font-weight: 600; margin-bottom: .3rem; }
  .drop-sub { font-size: .78rem; color: var(--text2); }
  .drop-selected { font-size: .8rem; color: var(--accent); margin-top: .6rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

  /* ── Suggestions ── */
  .suggestion-item { padding: .5rem .75rem; background: var(--surface2); border-left: 3px solid var(--yellow); border-radius: 0 6px 6px 0; font-size: .83rem; margin-bottom: .4rem; }

  /* ── AI feedback modal ── */
  .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,.7); z-index: 100; align-items: center; justify-content: center; }
  .modal-overlay.open { display: flex; }
  .modal { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: 1.5rem; width: 520px; max-width: 95vw; }
  .modal h3 { margin-bottom: 1rem; font-size: 1rem; }
  .modal-footer { display: flex; gap: .5rem; justify-content: flex-end; margin-top: 1rem; }

  /* ── Preview pane ── */
  .preview-pane { background: #fff; color: #1a1a1a; border-radius: var(--radius); padding: 2rem; font-family: 'Segoe UI', sans-serif; line-height: 1.7; }
  .preview-pane h1 { color: #005073; border-bottom: 3px solid #00bceb; padding-bottom: .4rem; margin-bottom: 1rem; }
  .preview-pane h2 { color: #005073; margin-top: 1.5rem; border-bottom: 1px solid #d0eaf5; padding-bottom: .3rem; }
  .preview-pane h3 { color: #1f7a8c; margin-top: 1.2rem; }
  .preview-pane blockquote { border-left: 4px solid #4caf50; background: #f0faf0; padding: .5rem 1rem; margin: .5rem 0; border-radius: 0 4px 4px 0; }
  .preview-pane code { background: #f4f4f4; padding: .1rem .3rem; border-radius: 3px; font-family: monospace; }
  .preview-pane pre { background: #f4f4f4; padding: 1rem; border-radius: 4px; overflow-x: auto; }
  .preview-pane p { margin: .6rem 0; }
  .preview-pane ul, .preview-pane ol { padding-left: 1.5rem; margin: .5rem 0; }
  .preview-pane .prev-meta { color: #666; font-size: .85rem; margin-bottom: 1.2rem; }
  .preview-pane .prev-tag { background: #e8f7fc; color: #005073; border-radius: 4px; padding: 2px 7px; font-size: .78rem; margin: 0 2px; }
  .preview-pane .prev-step { border-left: 4px solid #00bceb; padding: .5rem 1rem; margin: 1.2rem 0; background: #f8fdff; border-radius: 0 6px 6px 0; }
  .preview-pane .prev-step h3 { display: flex; align-items: center; gap: .6rem; margin-top: .3rem; }
  .preview-pane .prev-step-num { background: #00bceb; color: #fff; border-radius: 50%; width: 26px; height: 26px; display: inline-flex; align-items: center; justify-content: center; font-size: .8rem; font-weight: 700; flex-shrink: 0; }
  .preview-pane .prev-expected { background: #f0faf0; border-left: 4px solid #4caf50; padding: .5rem 1rem; border-radius: 0 4px 4px 0; margin-top: .6rem; font-size: .9rem; }
  .preview-pane .prev-note { background: #fff8e1; border-left: 4px solid #ffc107; padding: .5rem 1rem; border-radius: 0 4px 4px 0; margin-top: .6rem; font-size: .9rem; }
  .preview-pane .prev-figure { margin: 1rem 0; text-align: center; }
  .preview-pane .prev-figure img { max-width: 100%; border: 1px solid #ddd; border-radius: 6px; box-shadow: 0 2px 8px rgba(0,0,0,.08); cursor: zoom-in; }
  .preview-pane .prev-caption { font-size: .8rem; color: #666; margin-top: .35rem; font-style: italic; }

  /* ── Misc ── */
  .spinner { display: inline-block; width: 14px; height: 14px; border: 2px solid var(--border); border-top-color: var(--accent); border-radius: 50%; animation: spin .7s linear infinite; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .divider { border: none; border-top: 1px solid var(--border); margin: 1rem 0; }
  .text-muted { color: var(--text2); }
  .gap-row { display: flex; gap: .5rem; flex-wrap: wrap; align-items: center; }
  .section-collapsed .steps-container { display: none; }
  #no-guide { display: flex; flex-direction: column; align-items: center; justify-content: center; height: 100%; gap: 1rem; color: var(--text2); text-align: center; }
  #no-guide .big-icon { font-size: 4rem; }

  /* ── Session picker ── */
  .session-row {
    display: flex; align-items: center; gap: .6rem;
    padding: .45rem .6rem; border-radius: 6px; cursor: pointer;
    border: 1px solid var(--border); background: var(--surface2);
    transition: border-color .15s, background .15s;
  }
  .session-row:hover  { border-color: var(--accent); background: #0d1f2d; }
  .session-row.active { border-color: var(--accent); background: #0d1f2d; box-shadow: 0 0 0 2px rgba(0,188,235,.2); }
  .session-row .ss-count { font-size:.75rem;color:var(--text2); flex-shrink:0; }
  .session-row .ss-date  { font-size:.7rem; color:var(--text2); margin-left:auto; flex-shrink:0; }
  .session-row .ss-id    { font-size:.8rem; font-family:monospace; color:var(--text); }

  /* ── Annotation tool buttons ── */
  .ann-tool-btn {
    background: none; border: none; cursor: pointer;
    font-size: 15px; width: 30px; height: 28px;
    border-radius: 4px; color: var(--text2); display:flex;align-items:center;justify-content:center;
  }
  .ann-tool-btn:hover  { background: var(--border); color: var(--text); }
  .ann-tool-btn.active { background: var(--accent2); color: #fff; }

  /* ── Quill rich-text editor dark theme ── */
  .ql-toolbar.ql-snow {
    background: #1e2530; border: 1px solid var(--border);
    border-radius: 6px 6px 0 0; padding: 4px 6px; flex-wrap: wrap;
  }
  .ql-container.ql-snow {
    background: #161b22; border: 1px solid var(--border);
    border-top: none; border-radius: 0 0 6px 6px;
    font-family: inherit; font-size: 13px; color: var(--text); min-height: 80px;
  }
  .ql-editor { min-height: 80px; padding: 8px 10px; color: var(--text); }
  .ql-editor.ql-blank::before { color: var(--text2); font-style: normal; }
  .ql-snow .ql-stroke { stroke: var(--text2); }
  .ql-snow .ql-fill  { fill:   var(--text2); }
  .ql-snow .ql-picker-label { color: var(--text2); }
  .ql-snow .ql-picker-options {
    background: #1e2530; border: 1px solid var(--border); border-radius: 4px;
  }
  .ql-snow .ql-picker-item { color: var(--text); }
  .ql-snow.ql-toolbar button:hover .ql-stroke,
  .ql-snow .ql-toolbar button:hover .ql-stroke { stroke: var(--accent); }
  .ql-snow.ql-toolbar button.ql-active .ql-stroke { stroke: var(--accent); }
  .ql-snow.ql-toolbar button:hover .ql-fill,
  .ql-snow .ql-toolbar button:hover .ql-fill   { fill: var(--accent); }
  .ql-snow.ql-toolbar button.ql-active .ql-fill { fill: var(--accent); }
  .ql-snow .ql-picker-label:hover,
  .ql-snow .ql-picker-label.ql-active { color: var(--accent); }
  .ql-snow .ql-picker-item:hover { color: var(--accent); }
  /* Emoji picker button in toolbar */
  .ql-emoji-btn {
    background: none; border: none; cursor: pointer;
    color: var(--text2); font-size: 14px; padding: 3px 5px;
    line-height: 1; border-radius: 3px;
  }
  .ql-emoji-btn:hover { color: var(--accent); }
  /* Emoji grid popup */
  .emoji-picker-popup {
    position: absolute; z-index: 9999;
    background: #1e2530; border: 1px solid var(--border);
    border-radius: 8px; padding: 8px;
    display: grid; grid-template-columns: repeat(8, 28px);
    gap: 2px; max-height: 200px; overflow-y: auto;
    box-shadow: 0 4px 20px rgba(0,0,0,.5);
  }
  .emoji-picker-popup button {
    background: none; border: none; cursor: pointer;
    font-size: 16px; width: 28px; height: 28px;
    border-radius: 4px; display: flex; align-items: center; justify-content: center;
  }
  .emoji-picker-popup button:hover { background: var(--border); }
</style>
</head>
<body>

<!-- Top nav -->
<div class="topnav">
  <div class="logo">📋 Lab Guide Automator</div>
  <div class="nav-tabs">
    <button class="nav-tab active" onclick="showMainTab('guides')">Guides</button>
    <button class="nav-tab" onclick="showMainTab('record')">Record</button>
    <button class="nav-tab" onclick="showMainTab('ingest')">Ingest</button>
    <button class="nav-tab" onclick="showMainTab('sessions')">Sessions</button>
  </div>
  <div class="topnav-right">
    <span class="pill green" id="ai-status">✓ Copilot AI</span>
    <span class="pill" id="guide-count">0 guides</span>
  </div>
</div>

<!-- Main layout -->
<div class="main">

  <!-- Sidebar: guide library -->
  <div class="sidebar">
    <div class="sidebar-header">
      <span>Library</span>
      <button class="btn btn-primary btn-sm" onclick="openNewGuideModal()">+ New</button>
    </div>
    <div class="guide-list" id="guide-list">
      <div class="empty-state">No guides yet.<br>Record or ingest to get started.</div>
    </div>
  </div>

  <!-- Content area -->
  <div class="content">

    <!-- TAB: Guides (editor) -->
    <div id="tab-guides">
      <div id="no-guide">
        <div class="big-icon">📄</div>
        <div><strong>Select a guide</strong> from the library<br>or create a new one to get started.</div>
        <button class="btn btn-primary" onclick="openNewGuideModal()">Create Blank Guide</button>
      </div>
      <div id="guide-editor" style="display:none">

        <!-- Guide header -->
        <div class="guide-header card">
          <div class="guide-title-row">
            <h2 id="ed-title">—</h2>
            <span class="badge badge-blue" id="ed-version">v1.0</span>
            <span class="badge badge-yellow" id="ed-difficulty">Intermediate</span>
            <button class="btn btn-secondary btn-sm" onclick="openMetaModal()">Edit Info</button>
            <button class="btn btn-danger btn-sm" onclick="deleteCurrentGuide()">Delete</button>
          </div>
          <div class="meta-row">
            <span>👤 <span id="ed-author">—</span></span>
            <span>⏱ <span id="ed-duration">60</span> min</span>
            <span>📅 <span id="ed-date">—</span></span>
            <span id="ed-tags"></span>
          </div>
        </div>

        <!-- Inner tabs -->
        <div class="tab-bar">
          <button class="tab-btn active" onclick="showEditorTab('content')">Content</button>
          <button class="tab-btn" onclick="showEditorTab('objectives')">Objectives</button>
          <button class="tab-btn" onclick="showEditorTab('preview')">Preview</button>
          <button class="tab-btn" onclick="showEditorTab('export')">Export</button>
          <button class="tab-btn" onclick="showEditorTab('ai')">AI Tools</button>
        </div>

        <!-- Content tab -->
        <div class="tab-panel active" id="epanel-content">
          <div class="card">
            <div class="card-title"><span class="icon">📖</span> Introduction
              <button class="btn btn-secondary btn-sm" style="margin-left:auto" onclick="editIntro()">Edit</button>
              <button class="btn btn-ai btn-sm" onclick="rewriteIntro()">✦ AI</button>
            </div>
            <div id="ed-intro" class="step-body" style="margin-bottom:.5rem;white-space:pre-wrap"></div>
            <div id="ed-intro-edit" style="display:none">
              <textarea id="ed-intro-ta" rows="4" style="width:100%;margin-bottom:.4rem"></textarea>
              <div class="gap-row">
                <button class="btn btn-primary btn-sm" onclick="saveIntro()">Save</button>
                <button class="btn btn-secondary btn-sm" onclick="cancelIntro()">Cancel</button>
              </div>
            </div>
          </div>

          <div style="margin-bottom:.6rem;display:flex;gap:.5rem;align-items:center">
            <strong style="font-size:.85rem;color:var(--text2)">SECTIONS</strong>
            <button class="btn btn-primary btn-sm" onclick="addSection()" style="margin-left:auto">+ Add Section</button>
            <button class="btn btn-ai btn-sm" onclick="openAddAISection()">✦ AI Section</button>
          </div>
          <div id="ed-sections"></div>

          <div class="card" style="margin-top:.75rem">
            <div class="card-title"><span class="icon">🏁</span> Conclusion
              <button class="btn btn-secondary btn-sm" style="margin-left:auto" onclick="editConclusion()">Edit</button>
              <button class="btn btn-ai btn-sm" onclick="rewriteConclusion()">✦ AI</button>
            </div>
            <div id="ed-conclusion" class="step-body" style="margin-bottom:.5rem;white-space:pre-wrap"></div>
            <div id="ed-conclusion-edit" style="display:none">
              <textarea id="ed-conclusion-ta" rows="4" style="width:100%;margin-bottom:.4rem"></textarea>
              <div class="gap-row">
                <button class="btn btn-primary btn-sm" onclick="saveConclusion()">Save</button>
                <button class="btn btn-secondary btn-sm" onclick="cancelConclusion()">Cancel</button>
              </div>
            </div>
          </div>
        </div>

        <!-- Objectives tab -->
        <div class="tab-panel" id="epanel-objectives">
          <div class="card">
            <div class="card-title"><span class="icon">🎯</span> Learning Objectives</div>
            <div class="obj-list" id="ed-objectives"></div>
            <hr class="divider">
            <div class="form-row" style="margin-bottom:0">
              <div class="form-group">
                <label>Describe a new objective (plain English)</label>
                <input type="text" id="new-obj-input" placeholder="e.g. Students should be able to verify OSPF neighbor adjacency">
              </div>
              <button class="btn btn-ai" onclick="addObjective()">✦ Add Objective</button>
            </div>
          </div>
        </div>

        <!-- Preview tab -->
        <div class="tab-panel" id="epanel-preview">
          <div class="preview-pane" id="ed-preview">Loading preview...</div>
        </div>

        <!-- Export tab -->
         <div class="tab-panel" id="epanel-export">
           <div class="card">
             <div class="card-title"><span class="icon">📤</span> Export Guide</div>
             <div class="export-grid">
               <div class="export-card" onclick="exportGuide('markdown')">
                 <div class="export-icon">📝</div>
                 <div class="export-label">Markdown</div>
                 <div class="export-desc">.md file</div>
               </div>
               <div class="export-card" onclick="exportGuide('pdf')">
                 <div class="export-icon">📕</div>
                 <div class="export-label">PDF</div>
                 <div class="export-desc">Print-ready</div>
               </div>
               <div class="export-card" onclick="exportGuide('html')">
                 <div class="export-icon">🌐</div>
                 <div class="export-label">HTML</div>
                 <div class="export-desc">Moodle-ready</div>
               </div>
               <div class="export-card" onclick="exportGuide('docx')">
                 <div class="export-icon">📘</div>
                 <div class="export-label">Word</div>
                 <div class="export-desc">.docx</div>
               </div>
               <div class="export-card" onclick="exportGuide('mkdocs')">
                 <div class="export-icon">🏗️</div>
                 <div class="export-label">MkDocs Site</div>
                 <div class="export-desc">Material theme</div>
               </div>
             </div>
             <div id="export-log" class="progress-box" style="margin-top:1rem;min-height:60px;display:none"></div>
           </div>

           <!-- GitHub Sync / Publish card -->
           <div class="card" style="border-color:#238636;background:linear-gradient(135deg,#0d1f14,#161b22)">
             <div class="card-title"><span class="icon">🐙</span> Publish to GitHub
               <span id="pub-last" style="margin-left:auto;font-size:.73rem;color:var(--text2);font-weight:400"></span>
             </div>
             <div style="font-size:.8rem;color:var(--text2);margin-bottom:.9rem;line-height:1.6">
               Builds the MkDocs site and pushes to your GitHub repo. If the repo is connected to
               AWS Amplify / GitHub Pages, it will publish automatically on push.
             </div>
             <div class="form-row" style="gap:.75rem">
               <div class="form-group" style="flex:3">
                 <label>GitHub repo URL</label>
                 <input type="text" id="pub-repo" placeholder="https://github.com/org/repo.git"
                   style="font-family:monospace;font-size:.82rem">
               </div>
               <div class="form-group" style="flex:1;min-width:110px">
                 <label>Branch</label>
                 <input type="text" id="pub-branch" value="main" placeholder="main">
               </div>
             </div>
             <div style="display:flex;gap:.75rem;align-items:center;flex-wrap:wrap">
               <button class="btn" style="background:#238636;color:#fff;border-color:#238636"
                 onclick="saveAndPublish()">🚀 Save &amp; Publish</button>
               <button class="btn btn-secondary" onclick="saveSyncConfig()">💾 Save Config Only</button>
               <span id="pub-config-status" style="font-size:.75rem;color:var(--text2)"></span>
             </div>
             <div id="pub-log" class="progress-box" style="margin-top:1rem;display:none"></div>
           </div>
         </div>
              <div class="export-card" onclick="exportGuide('pdf')">
                <div class="export-icon">📕</div>
                <div class="export-label">PDF</div>
                <div class="export-desc">Print-ready</div>
              </div>
              <div class="export-card" onclick="exportGuide('html')">
                <div class="export-icon">🌐</div>
                <div class="export-label">HTML</div>
                <div class="export-desc">Moodle-ready</div>
              </div>
              <div class="export-card" onclick="exportGuide('docx')">
                <div class="export-icon">📘</div>
                <div class="export-label">Word</div>
                <div class="export-desc">.docx</div>
              </div>
              <div class="export-card" onclick="exportGuide('mkdocs')">
                <div class="export-icon">🏗️</div>
                <div class="export-label">MkDocs Site</div>
                <div class="export-desc">Material theme</div>
              </div>
            </div>
            <div id="export-log" class="progress-box" style="margin-top:1rem;min-height:60px;display:none"></div>
          </div>
        </div>

        <!-- AI Tools tab -->
        <div class="tab-panel" id="epanel-ai">

          <!-- Model selector -->
          <div class="card" style="margin-bottom:1rem">
            <div class="card-title"><span class="icon">🤖</span> AI Model</div>
            <div style="font-size:.8rem;color:var(--text2);margin-bottom:.75rem">
              All models run via your GitHub Copilot access — no extra API key needed.
            </div>
            <div style="display:flex;align-items:center;gap:.75rem;flex-wrap:wrap">
              <select id="model-select" style="flex:1;min-width:220px" onchange="setModel(this.value)">
                <option value="">Loading models…</option>
              </select>
              <span id="model-tip" style="font-size:.75rem;display:none;padding:.3rem .65rem;border-radius:20px;background:rgba(0,188,235,.12);border:1px solid var(--accent);color:var(--accent);white-space:nowrap"></span>
            </div>
            <div id="model-status" style="font-size:.75rem;color:var(--text2);margin-top:.5rem"></div>
          </div>

          <!-- AI Review -->
          <div class="card">
            <div class="card-title"><span class="icon">✦</span> AI Review</div>
            <p class="text-muted" style="margin-bottom:.75rem;font-size:.83rem">Ask the AI to review the entire guide and suggest improvements.</p>
            <button class="btn btn-ai" onclick="suggestImprovements()">✦ Suggest Improvements</button>
            <div id="suggestion-list" style="margin-top:1rem"></div>
          </div>
        </div>

      </div><!-- /guide-editor -->
    </div><!-- /tab-guides -->

    <!-- TAB: Record -->
    <div id="tab-record" style="display:none">
      <div class="card">
        <div class="card-title"><span class="icon">📸</span> Capture Session</div>

        <div style="background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:.75rem 1rem;margin-bottom:1rem;font-size:.82rem;color:var(--text2);line-height:1.7">
          <strong style="color:var(--text)">How it works:</strong><br>
          1. Click <strong>Open Capture Panel</strong> below — a small floating window opens<br>
          2. Give your session a name (e.g. "Configure SD-WAN Policy")<br>
          3. Pick the browser tab to capture, then click <strong>📸 Start Session</strong><br>
          4. Hit <strong>📸 Capture</strong> (or <kbd style="background:var(--bg2);border:1px solid var(--border);border-radius:3px;padding:1px 4px;font-size:.7rem">⌘⇧S</kbd>) at each key step<br>
          5. Use <strong>⏸ Pause</strong> to skip steps you don't want captured<br>
          6. Click <strong>⏹ End</strong> when done — your session appears in the <strong>Sessions</strong> and <strong>Ingest</strong> tabs
        </div>

        <div style="background:var(--surface2);border:1px solid var(--border);border-radius:6px;padding:.6rem 1rem;margin-bottom:1rem;font-size:.8rem;color:var(--text2);line-height:1.6">
          <strong style="color:var(--text)">What is — and isn't — recorded:</strong><br>
          ✅ &nbsp;The specific browser tab you pin (only that window)<br>
          ✅ &nbsp;Your microphone — test it inside the panel before starting<br>
          ❌ &nbsp;The capture panel itself is never captured<br>
          ❌ &nbsp;No webcam, no other windows
        </div>

        <button class="btn btn-rec" onclick="openCapturePanel()" style="font-size:1rem;padding:.75rem">
          📸 &nbsp;Open Capture Panel
        </button>
        <div style="margin-top:.6rem;font-size:.75rem;color:var(--text2)">
          The panel floats above your other windows — keep it visible while you work.
        </div>
      </div>
    </div><!-- /tab-record -->

    <!-- TAB: Ingest -->
    <div id="tab-ingest" style="display:none">

      <!-- Import existing document -->
      <div class="card" style="border-color:var(--accent);background:linear-gradient(135deg,#0d1f2d,#161b22)">
        <div class="card-title"><span class="icon">📄</span> Import Existing Lab Guide</div>
        <div style="font-size:.82rem;color:var(--text2);margin-bottom:.9rem;line-height:1.6">
          Browse to an existing lab guide — AI will extract sections, steps, objectives and structure automatically.<br>
          <strong style="color:var(--text)">Supported:</strong>
          <span class="badge badge-blue" style="margin:0 .2rem">PDF</span>
          <span class="badge badge-blue" style="margin:0 .2rem">Word (.docx)</span>
          <span class="badge badge-blue" style="margin:0 .2rem">Markdown (.md)</span>
          <span class="badge badge-blue" style="margin:0 .2rem">HTML</span>
          <span class="badge badge-blue" style="margin:0 .2rem">Plain text</span>
        </div>
        <div class="drop-zone" id="doc-drop-zone" onclick="document.getElementById('doc-file-input').click()" ondragover="onDragOver(event,'doc-drop-zone')" ondragleave="onDragLeave('doc-drop-zone')" ondrop="onDropDoc(event)">
          <div class="drop-icon">📄</div>
          <div class="drop-label">Click to browse or drag & drop your lab guide here</div>
          <div class="drop-sub">PDF, Word, Markdown, HTML, or plain text</div>
          <div class="drop-selected" id="doc-selected"></div>
        </div>
        <input type="file" id="doc-file-input" style="display:none" accept=".pdf,.docx,.md,.markdown,.html,.htm,.txt" onchange="onDocFileSelected(this)">
        <div style="margin-top:.75rem">
          <button class="btn btn-primary" id="doc-ingest-btn" onclick="ingestDocument()" disabled>📄 Import & Parse Document</button>
        </div>
      </div>

      <!-- Ingest video recording -->
      <div class="card">
        <div class="card-title"><span class="icon">⚡</span> Ingest Screen Recording</div>
        <div class="drop-zone" id="video-drop-zone" onclick="document.getElementById('video-file-input').click()" ondragover="onDragOver(event,'video-drop-zone')" ondragleave="onDragLeave('video-drop-zone')" ondrop="onDropVideo(event)">
          <div class="drop-icon">🎬</div>
          <div class="drop-label">Click to browse or drag & drop your recording</div>
          <div class="drop-sub">.mp4, .mov, .mkv</div>
          <div class="drop-selected" id="video-selected"></div>
        </div>
        <input type="file" id="video-file-input" style="display:none" accept=".mp4,.mov,.mkv,.avi,.webm" onchange="onVideoFileSelected(this)">
        <div class="form-row" style="margin-top:.75rem">
          <div class="form-group">
            <label>Lab title <span style="color:var(--red)">*</span></label>
            <input type="text" id="ingest-video-title" placeholder="e.g. OSPF Configuration Lab">
          </div>
          <div class="form-group" style="flex:0 0 140px">
            <label>Frame interval (s)</label>
            <input type="number" id="ingest-interval" value="5" min="1" max="60">
          </div>
          <div style="display:flex;align-items:flex-end">
            <button class="btn btn-primary" id="video-ingest-btn" onclick="ingestVideo()" disabled>⚡ Ingest Video</button>
          </div>
        </div>
      </div>

      <!-- Ingest screenshots — recent sessions picker + file upload fallback -->
      <div class="card">
        <div class="card-title"><span class="icon">🖼️</span> Ingest Screenshots into a Guide</div>

        <!-- Recent sessions -->
        <div style="margin-bottom:.75rem">
          <div style="font-size:.75rem;color:var(--text2);margin-bottom:.5rem;text-transform:uppercase;letter-spacing:.4px">
            Recent capture sessions
            <button onclick="loadSessions()" style="background:none;border:none;color:var(--accent);font-size:.75rem;cursor:pointer;margin-left:.5rem">↺ Refresh</button>
          </div>
          <div id="session-list" style="display:flex;flex-direction:column;gap:.4rem;max-height:220px;overflow-y:auto">
            <div style="color:var(--text2);font-size:.8rem">Loading sessions…</div>
          </div>
        </div>

        <div style="font-size:.75rem;color:var(--text2);margin:.5rem 0;text-align:center">— or upload screenshots directly —</div>

        <!-- File upload fallback -->
        <div class="drop-zone" id="ss-drop-zone" onclick="document.getElementById('ss-file-input').click()" ondragover="onDragOver(event,'ss-drop-zone')" ondragleave="onDragLeave('ss-drop-zone')" ondrop="onDropScreenshots(event)" style="padding:.75rem">
          <div class="drop-icon" style="font-size:1.5rem">🖼️</div>
          <div class="drop-label" style="font-size:.8rem">Click or drag & drop screenshots</div>
          <div class="drop-sub">PNG / JPG — ordered by filename</div>
          <div class="drop-selected" id="ss-selected"></div>
        </div>
        <input type="file" id="ss-file-input" style="display:none" accept=".png,.jpg,.jpeg" multiple onchange="onScreenshotFilesSelected(this)">

        <!-- Title + ingest button (shared by session picker and file upload) -->
        <div class="form-row" style="margin-top:.75rem">
          <div class="form-group">
            <label>Lab title <span style="color:var(--red)">*</span></label>
            <input type="text" id="ingest-folder-title" placeholder="e.g. BGP Lab">
          </div>
          <div style="display:flex;align-items:flex-end">
            <button class="btn btn-primary" id="ss-ingest-btn" onclick="ingestScreenshots()" disabled>⚡ Generate Guide with AI</button>
          </div>
        </div>
      </div>

      <div class="card" id="ingest-progress-card" style="display:none">
        <div class="card-title"><span class="icon">⏳</span> Ingestion Progress</div>
        <div class="progress-box" id="ingest-log"></div>
      </div>
    </div><!-- /tab-ingest -->

    <!-- ═══════════════════════════════════════════════════════ -->
    <!-- Sessions tab                                            -->
    <!-- ═══════════════════════════════════════════════════════ -->
    <div id="tab-sessions" style="display:none">
      <div style="display:flex;align-items:center;gap:.75rem;margin-bottom:1rem">
        <h2 style="font-size:1.1rem;font-weight:700;margin:0">Capture Sessions</h2>
        <button class="btn btn-secondary btn-sm" onclick="loadSessionsTab()">↺ Refresh</button>
      </div>
      <div id="sessions-grid" style="display:grid;gap:.75rem">
        <div style="color:var(--text2)">Loading…</div>
      </div>
    </div><!-- /tab-sessions -->

  </div><!-- /content -->
</div><!-- /main -->

<!-- ── Modals ── -->

<!-- New guide modal -->
<div class="modal-overlay" id="modal-new-guide">
  <div class="modal">
    <h3>Create New Guide</h3>
    <div class="form-group" style="margin-bottom:.75rem">
      <label>Title</label>
      <input type="text" id="ng-title" placeholder="Lab guide title">
    </div>
    <div class="form-row">
      <div class="form-group">
        <label>Author</label>
        <input type="text" id="ng-author" placeholder="Your name">
      </div>
      <div class="form-group">
        <label>Difficulty</label>
        <select id="ng-difficulty">
          <option value="beginner">Beginner</option>
          <option value="intermediate" selected>Intermediate</option>
          <option value="advanced">Advanced</option>
        </select>
      </div>
      <div class="form-group" style="flex:0 0 100px">
        <label>Duration (min)</label>
        <input type="number" id="ng-duration" value="60">
      </div>
    </div>
    <div class="modal-footer">
      <button class="btn btn-secondary" onclick="closeModal('modal-new-guide')">Cancel</button>
      <button class="btn btn-primary" onclick="createGuide()">Create</button>
    </div>
  </div>
</div>

<!-- Edit metadata modal -->
<div class="modal-overlay" id="modal-meta">
  <div class="modal">
    <h3>Edit Guide Info</h3>
    <div class="form-group" style="margin-bottom:.75rem">
      <label>Title</label>
      <input type="text" id="meta-title">
    </div>
    <div class="form-row">
      <div class="form-group">
        <label>Author</label>
        <input type="text" id="meta-author">
      </div>
      <div class="form-group">
        <label>Version</label>
        <input type="text" id="meta-version">
      </div>
    </div>
    <div class="form-row">
      <div class="form-group">
        <label>Difficulty</label>
        <select id="meta-difficulty">
          <option value="beginner">Beginner</option>
          <option value="intermediate">Intermediate</option>
          <option value="advanced">Advanced</option>
        </select>
      </div>
      <div class="form-group">
        <label>Duration (min)</label>
        <input type="number" id="meta-duration">
      </div>
    </div>
    <div class="form-group" style="margin-bottom:.75rem">
      <label>Tags (comma-separated)</label>
      <input type="text" id="meta-tags" placeholder="ospf, routing, cisco">
    </div>
    <div class="modal-footer">
      <button class="btn btn-secondary" onclick="closeModal('modal-meta')">Cancel</button>
      <button class="btn btn-primary" onclick="saveMetadata()">Save</button>
    </div>
  </div>
</div>

<!-- AI feedback modal -->
<div class="modal-overlay" id="modal-ai">
  <div class="modal">
    <h3 id="modal-ai-title">Rewrite with AI</h3>
    <div class="form-group" style="margin-bottom:.75rem">
      <label>Feedback / instructions for the AI</label>
      <textarea id="modal-ai-feedback" rows="4" placeholder="e.g. Make it more specific. Include the exact CLI command shown. Add a note about common mistakes."></textarea>
    </div>
    <div class="modal-footer">
      <button class="btn btn-secondary" onclick="closeModal('modal-ai')">Cancel</button>
      <button class="btn btn-ai" id="modal-ai-confirm" onclick="">✦ Rewrite</button>
    </div>
  </div>
</div>

<!-- Screenshot label modal -->

<!-- Screenshot Repository Modal -->
<div class="modal-overlay" id="modal-ss-repo">
  <div class="modal" style="max-width:780px;width:95vw">
    <h3>📷 Screenshot Repository</h3>
    <div class="repo-filter">
      <input type="text" id="repo-search" placeholder="Filter by caption…" oninput="repoFilter()">
      <label class="btn btn-secondary" style="cursor:pointer;white-space:nowrap">
        ⬆ Upload
        <input type="file" accept=".png,.jpg,.jpeg" multiple style="display:none" onchange="repoUpload(this)">
      </label>
    </div>
    <div class="repo-grid" id="repo-grid"></div>
    <div style="margin-top:1rem;font-size:.8rem;color:var(--text2)">
      Click a screenshot to select it, then click <strong>Attach to Step</strong>.
      Hover for AI caption and delete options.
    </div>
    <div class="gap-row" style="margin-top:1rem">
      <button class="btn btn-secondary" onclick="closeModal('modal-ss-repo')">Cancel</button>
      <button class="btn btn-primary" id="repo-attach-btn" onclick="repoAttach()" disabled>📎 Attach to Step</button>
    </div>
  </div>
</div>

<!-- Annotation Modal -->
<div class="modal-overlay" id="modal-annotate" style="background:rgba(0,0,0,.85)">
  <div style="display:flex;flex-direction:column;width:98vw;height:96vh;background:var(--surface);border-radius:10px;overflow:hidden">
    <!-- Toolbar -->
    <div id="ann-toolbar" style="display:flex;align-items:center;gap:6px;padding:6px 10px;background:var(--surface2);border-bottom:1px solid var(--border);flex-wrap:wrap">
      <span style="font-weight:600;font-size:.85rem;color:var(--text2);margin-right:4px">✏️ Annotate</span>

      <!-- Tools -->
      <div style="display:flex;gap:3px;background:var(--bg);border-radius:5px;padding:2px">
        <button class="ann-tool-btn active" id="ann-tool-pen"   onclick="annSetTool('pen')"   title="Pen (P)">🖊</button>
        <button class="ann-tool-btn" id="ann-tool-arrow" onclick="annSetTool('arrow')" title="Arrow (A)">➡️</button>
        <button class="ann-tool-btn" id="ann-tool-rect"  onclick="annSetTool('rect')"  title="Rectangle (R)">▭</button>
        <button class="ann-tool-btn" id="ann-tool-text"  onclick="annSetTool('text')"  title="Text (T)">T</button>
        <button class="ann-tool-btn" id="ann-tool-highlight" onclick="annSetTool('highlight')" title="Highlight (H)">🖍</button>
      </div>

      <!-- Colour -->
      <div style="display:flex;gap:3px;align-items:center">
        <span style="font-size:.7rem;color:var(--text2)">Color</span>
        <input type="color" id="ann-color" value="#FF3B30" title="Stroke color"
          style="width:28px;height:26px;border:1px solid var(--border);border-radius:4px;background:none;cursor:pointer;padding:1px">
      </div>

      <!-- Stroke size -->
      <div style="display:flex;gap:4px;align-items:center">
        <span style="font-size:.7rem;color:var(--text2)">Size</span>
        <input type="range" id="ann-size" min="1" max="20" value="3"
          style="width:70px;accent-color:var(--accent)">
        <span id="ann-size-label" style="font-size:.7rem;color:var(--text2);width:18px">3</span>
      </div>

      <!-- Font size (text tool) -->
      <div id="ann-font-row" style="display:none;gap:4px;align-items:center">
        <span style="font-size:.7rem;color:var(--text2)">Font</span>
        <select id="ann-font-size" style="background:var(--bg);border:1px solid var(--border);color:var(--text);border-radius:4px;padding:2px 4px;font-size:.75rem">
          <option value="14">14</option><option value="18">18</option>
          <option value="24" selected>24</option><option value="32">32</option>
          <option value="40">40</option><option value="56">56</option>
        </select>
      </div>

      <!-- Opacity (highlight) -->
      <div id="ann-opacity-row" style="display:none;gap:4px;align-items:center">
        <span style="font-size:.7rem;color:var(--text2)">Opacity</span>
        <input type="range" id="ann-opacity" min="10" max="80" value="35"
          style="width:60px;accent-color:var(--accent)">
      </div>

      <div style="flex:1"></div>
      <button class="btn btn-secondary btn-sm" onclick="annUndo()" title="Undo (Cmd+Z)">↩ Undo</button>
      <button class="btn btn-secondary btn-sm" onclick="annClear()" title="Clear all annotations">🗑 Clear</button>
      <button class="btn btn-primary  btn-sm" onclick="annSave()" title="Save & overwrite">💾 Save</button>
      <button class="btn btn-secondary btn-sm" onclick="closeModal('modal-annotate')">✕ Close</button>
    </div>

    <!-- Canvas area -->
    <div id="ann-canvas-wrap" style="flex:1;overflow:auto;display:flex;align-items:flex-start;justify-content:center;padding:12px;background:#111">
      <div style="position:relative;display:inline-block">
        <canvas id="ann-canvas" style="display:block;cursor:crosshair;border-radius:4px;box-shadow:0 2px 20px rgba(0,0,0,.6)"></canvas>
        <canvas id="ann-overlay" style="position:absolute;top:0;left:0;cursor:crosshair;border-radius:4px"></canvas>
        <!-- Floating text input for text tool -->
        <textarea id="ann-text-input" style="display:none;position:absolute;background:transparent;border:1px dashed #fff;color:#fff;font-weight:bold;resize:none;outline:none;padding:2px 4px;min-width:80px;min-height:28px;overflow:hidden"></textarea>
      </div>
    </div>
  </div>
</div>

<!-- Screenshot Preview Modal -->
<div class="modal-overlay" id="modal-ss-preview">
  <div class="modal" style="max-width:90vw;width:auto;text-align:center">
    <img id="ss-preview-img" src="" style="max-width:85vw;max-height:70vh;border-radius:6px;display:block;margin:0 auto">
    <div id="ss-preview-cap" style="margin-top:.75rem;font-size:.85rem;color:var(--text2)"></div>
    <div style="display:flex;justify-content:center;gap:.5rem;margin-top:1rem">
      <button class="btn btn-primary btn-sm" onclick="sessPreviewAnnotate()">✏️ Annotate</button>
      <button class="btn btn-secondary" onclick="closeModal('modal-ss-preview')">Close</button>
    </div>
  </div>
</div>

<!-- Add Section Modal -->
<div class="modal-overlay" id="modal-add-section">
  <div class="modal" style="width:440px">
    <h3>New Section</h3>
    <div class="form-group" style="margin-bottom:.75rem">
      <label>Title <span style="color:var(--accent)">*</span></label>
      <input type="text" id="new-sec-title" placeholder="e.g. Browse the Integration Catalog"
             onkeydown="if(event.key==='Enter')submitAddSection()">
    </div>
    <div class="form-group" style="margin-bottom:1rem">
      <label>Overview <span style="font-weight:400;color:var(--text2)">(optional)</span></label>
      <input type="text" id="new-sec-overview" placeholder="One-line description shown under the heading"
             onkeydown="if(event.key==='Enter')submitAddSection()">
    </div>
    <div class="modal-footer">
      <button class="btn btn-secondary" onclick="closeModal('modal-add-section')">Cancel</button>
      <button class="btn btn-primary" onclick="submitAddSection()">Add Section</button>
    </div>
  </div>
</div>

<!-- Add AI Section Modal -->
<div class="modal-overlay" id="modal-add-ai-section">
  <div class="modal" style="width:520px">
    <h3>✦ Generate Section with AI</h3>
    <p style="color:var(--text2);font-size:.85rem;margin-bottom:1rem">
      Capture screenshots for one section, then let AI write the steps and add the section to this guide.
    </p>
    <div class="form-group" style="margin-bottom:.75rem">
      <label>Section Title <span style="color:var(--accent)">*</span></label>
      <input type="text" id="ai-sec-title" placeholder="e.g. Configure the SD-WAN Policy">
    </div>
    <div class="form-group" style="margin-bottom:.75rem">
      <label style="display:flex;align-items:center;gap:.5rem">
        Screenshots Session
        <button onclick="loadAISectionSessions()" style="background:none;border:none;color:var(--accent);font-size:.75rem;cursor:pointer">↺ Refresh</button>
      </label>
      <div id="ai-sec-session-list" style="max-height:160px;overflow-y:auto;border:1px solid var(--border);border-radius:6px;padding:.4rem;margin-top:.3rem">
        <div style="color:var(--text2);font-size:.8rem">Loading…</div>
      </div>
      <div id="ai-sec-selected-label" style="font-size:.75rem;color:var(--accent);margin-top:.3rem"></div>
    </div>
    <div id="ai-sec-progress-log" style="display:none;max-height:120px;overflow-y:auto;background:var(--bg2);border-radius:6px;padding:.5rem;font-size:.78rem;font-family:monospace;margin-bottom:.75rem"></div>
    <div class="modal-footer">
      <button class="btn btn-secondary" onclick="closeModal('modal-add-ai-section')">Cancel</button>
      <button class="btn btn-ai" id="ai-sec-submit-btn" onclick="submitAddAISection()">✦ Generate &amp; Add Section</button>
    </div>
  </div>
</div>

<script>
// ── State ─────────────────────────────────────────────────────
let currentGuideId = null;
let currentGuide = null;
let activeRecordingSession = null;
let recTimerInterval = null;
let recStartTime = null;
let lastVideoPath = null;

// Screenshot repository state
let _repoItems = [];           // all screenshots from API
let _repoStepTarget = null;    // step ID we're attaching to
let _repoSectionTarget = null; // section ID (legacy filmstrip attach)
let _repoBlockTarget = null;   // {secId, blockId} for block-level attach
let _repoSelected = null;      // selected filename in repo

// ── Main tab switching ────────────────────────────────────────
function showMainTab(tab) {
  document.querySelectorAll('.nav-tab').forEach((t,i) => {
    const tabs = ['guides','record','ingest','sessions'];
    t.classList.toggle('active', tabs[i] === tab);
  });
  ['guides','record','ingest','sessions'].forEach(t => {
    document.getElementById('tab-' + t).style.display = t === tab ? 'block' : 'none';
  });
  if (tab === 'guides' && currentGuideId) loadGuide(currentGuideId);
  if (tab === 'ingest') loadSessions();
  if (tab === 'sessions') loadSessionsTab();
}

// ── Editor tab switching ──────────────────────────────────────
function showEditorTab(tab) {
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.tab-panel').forEach(p => p.classList.remove('active'));
  event.target.classList.add('active');
  document.getElementById('epanel-' + tab).classList.add('active');
  if (tab === 'preview') loadPreview();
  if (tab === 'ai') loadModels();
}

// ── Guide library ─────────────────────────────────────────────
async function loadLibrary() {
  const res = await fetch('/api/guides');
  const guides = await res.json();
  const list = document.getElementById('guide-list');
  document.getElementById('guide-count').textContent = guides.length + ' guide' + (guides.length !== 1 ? 's' : '');

  if (!guides.length) {
    list.innerHTML = '<div class="empty-state">No guides yet.<br>Record or ingest to get started.</div>';
    return;
  }
  list.innerHTML = guides.map(g => `
    <div class="guide-item ${g.id === currentGuideId ? 'active' : ''}" onclick="loadGuide('${g.id}')">
      <div class="gtitle">${g.title}</div>
      <div class="gmeta">${g.sections} sections · ${g.steps} steps · v${g.version}</div>
    </div>
  `).join('');
}

async function loadGuide(id) {
  currentGuideId = id;
  const res = await fetch('/api/guides/' + id);
  currentGuide = await res.json();
  renderGuide();
  loadLibrary();
  loadSyncConfig(id);
  document.getElementById('no-guide').style.display = 'none';
  document.getElementById('guide-editor').style.display = 'block';
}

function renderGuide() {
  const g = currentGuide;
  const m = g.metadata;
  document.getElementById('ed-title').textContent = m.title;
  document.getElementById('ed-version').textContent = 'v' + m.version;
  document.getElementById('ed-difficulty').textContent = m.difficulty.charAt(0).toUpperCase() + m.difficulty.slice(1);
  document.getElementById('ed-author').textContent = m.author || 'Unknown';
  document.getElementById('ed-duration').textContent = m.lab_duration_minutes;
  document.getElementById('ed-date').textContent = m.date;
  document.getElementById('ed-tags').innerHTML = (m.tags || []).map(t => `<span class="badge badge-blue">${t}</span>`).join(' ');
  document.getElementById('ed-intro').textContent = g.introduction || '(No introduction yet)';
  document.getElementById('ed-conclusion').textContent = g.conclusion || '(No conclusion yet)';
  renderSections();
  renderObjectives();
}

function renderSections() {
  const container = document.getElementById('ed-sections');
  const sections = currentGuide.sections || [];
  if (!sections.length) {
    container.innerHTML = '<div class="card"><p class="text-muted">No sections yet. Click <strong>+ Add Section</strong> below, or ingest a recording to auto-generate.</p></div>';
    return;
  }
  container.innerHTML = sections.map((sec, secIdx) => `
    <div class="section-block" id="secblock-${sec.id}"
         draggable="true"
         ondragstart="secDragStart(event,'${sec.id}')"
         ondragover="secDragOver(event,this)"
         ondragleave="secDragLeave(this)"
         ondrop="secDrop(event,'${sec.id}')">
      <div class="section-header" onclick="toggleSection(this)">
        <span class="sec-drag-handle" title="Drag to reorder" onclick="event.stopPropagation()">⠿</span>
        <span class="section-num">${secIdx + 1}</span>
        <span class="section-title" id="sec-title-${sec.id}">${sec.title}</span>
        <input class="sec-title-input" id="sec-title-input-${sec.id}"
               style="display:none" value="${sec.title.replace(/"/g,'&quot;')}"
               onblur="secTitleSave('${sec.id}')"
               onkeydown="if(event.key==='Enter'){event.preventDefault();secTitleSave('${sec.id}');}if(event.key==='Escape'){secTitleCancel('${sec.id}');}">
        <span class="section-count">${sec.steps.length} step${sec.steps.length !== 1 ? 's' : ''}</span>
        <button class="btn btn-secondary btn-sm" onclick="event.stopPropagation();secTitleEdit('${sec.id}')">✎ Rename</button>
        <button class="btn btn-ai btn-sm" onclick="event.stopPropagation();rewriteSection('${sec.id}')">✦ AI</button>
        <button class="btn btn-danger btn-sm" onclick="event.stopPropagation();deleteSection('${sec.id}')">✕</button>
      </div>

      <!-- Overview -->
      <div class="sec-overview-row" id="sec-overview-row-${sec.id}">
        <span class="sec-overview-text" id="sec-overview-text-${sec.id}">${sec.overview || '<em style="color:var(--text2)">No overview — click Edit to add one</em>'}</span>
        <button class="btn btn-secondary btn-sm" style="flex-shrink:0" onclick="secOverviewEdit('${sec.id}','${(sec.overview||'').replace(/'/g,"\\'")}')">Edit</button>
      </div>
      <div id="sec-overview-edit-${sec.id}" style="display:none;padding:.4rem .75rem;border-bottom:1px solid var(--border)">
        <textarea id="sec-overview-ta-${sec.id}" rows="2" style="width:100%;margin-bottom:.35rem;font-size:.83rem"></textarea>
        <div class="gap-row">
          <button class="btn btn-primary btn-sm" onclick="secOverviewSave('${sec.id}')">Save</button>
          <button class="btn btn-secondary btn-sm" onclick="secOverviewCancel('${sec.id}')">Cancel</button>
        </div>
      </div>

      <!-- Content blocks -->
      <div class="block-list" id="block-list-${sec.id}">
        ${renderBlockDivider(sec.id, null)}
        ${(sec.blocks || []).map(b => renderBlock(sec.id, b)).join('')}
      </div>

      <!-- Steps -->
      <div class="steps-container" style="margin-top:.5rem">
        ${sec.steps.map((step, i) => renderStep(step, i + 1)).join('')}
        <button class="btn btn-secondary btn-sm" style="margin-top:.4rem;border-style:dashed" onclick="addStepToSection('${sec.id}')">+ Add Step</button>
      </div>
    </div>
  `).join('');
}

function renderBlock(secId, block) {
  if (block.type === 'text') {
    return `
      <div class="block-item block-text" id="block-${block.id}" draggable="true"
           ondragstart="blockDragStart(event,'${secId}','${block.id}')"
           ondragover="blockDragOver(event,this)" ondragleave="blockDragLeave(this)"
           ondrop="blockDrop(event,'${secId}','${block.id}')">
        <div class="block-text-body" onclick="blockTextEdit('${block.id}',true)" title="Click to edit">
          <span class="block-drag-handle" title="Drag to reorder">⠿</span>
          ${block.content || '<span style="color:var(--text2);font-style:italic">Click to add text…</span>'}
        </div>
        <div class="block-text-edit" id="block-edit-${block.id}">
          <textarea id="block-ta-${block.id}">${block.content}</textarea>
          <div class="gap-row" style="margin-top:.35rem">
            <button class="btn btn-primary btn-sm" onclick="blockTextSave('${secId}','${block.id}')">Save</button>
            <button class="btn btn-secondary btn-sm" onclick="blockTextEdit('${block.id}',false)">Cancel</button>
            <button class="btn btn-danger btn-sm" style="margin-left:auto" onclick="blockDelete('${secId}','${block.id}')">Delete block</button>
          </div>
        </div>
      </div>
      ${renderBlockDivider(secId, block.id)}`;
  } else {
    const fname = block.path ? block.path.split('/').pop() : '';
    const cap = block.caption || '';
    const imgHtml = fname
      ? `<div class="block-ss-thumb" onclick="ssPreview('/api/screenshots/file/${fname}','${cap}')"><img src="/api/screenshots/file/${fname}" alt="${cap}"><div class="block-ss-caption">${cap || fname}</div></div>`
      : `<div class="block-ss-thumb" style="display:flex;align-items:center;justify-content:center;height:100px;color:var(--text2);font-size:.8rem;cursor:default">No image yet</div>`;
    return `
      <div class="block-item block-screenshot" id="block-${block.id}" draggable="true"
           ondragstart="blockDragStart(event,'${secId}','${block.id}')"
           ondragover="blockDragOver(event,this)" ondragleave="blockDragLeave(this)"
           ondrop="blockDrop(event,'${secId}','${block.id}')">
        <span class="block-drag-handle" title="Drag to reorder" style="margin-top:.5rem">⠿</span>
        ${imgHtml}
        <div class="block-ss-meta">
          <div style="font-size:.75rem;font-weight:600;color:var(--text2)">Screenshot</div>
          <input class="block-ss-cap-input" type="text" value="${cap}" placeholder="Caption…"
            onchange="blockSsCaption('${secId}','${block.id}',this.value)">
          <div class="block-actions">
            <button class="btn btn-secondary btn-sm" onclick="blockSsPick('${secId}','${block.id}')">📂 Pick from Repository</button>
            <label class="btn btn-secondary btn-sm" style="cursor:pointer">
              ⬆ Upload<input type="file" accept=".png,.jpg,.jpeg" style="display:none" onchange="blockSsUpload(this,'${secId}','${block.id}')">
            </label>
            <button class="btn btn-ai btn-sm" onclick="blockSsAiCaption('${secId}','${block.id}','${fname}')">✦ AI Caption</button>
            <button class="btn btn-danger btn-sm" onclick="blockDelete('${secId}','${block.id}')">✕</button>
          </div>
        </div>
      </div>
      ${renderBlockDivider(secId, block.id)}`;
  }
}

function renderBlockDivider(secId, afterBlockId) {
  const aid = afterBlockId ? `'${afterBlockId}'` : 'null';
  return `<div class="block-divider">
    <div class="block-divider-line"></div>
    <button class="btn btn-secondary block-add-btn" onclick="blockAddText('${secId}',${aid})">+ Text</button>
    <button class="btn btn-secondary block-add-btn" onclick="blockAddScreenshot('${secId}',${aid})">🖼 Screenshot</button>
    <div class="block-divider-line"></div>
  </div>`;
}

function addSection() {
  document.getElementById('new-sec-title').value = '';
  document.getElementById('new-sec-overview').value = '';
  document.getElementById('modal-add-section').classList.add('open');
  setTimeout(() => document.getElementById('new-sec-title').focus(), 50);
}

async function submitAddSection() {
  const title = document.getElementById('new-sec-title').value.trim();
  if (!title) { document.getElementById('new-sec-title').focus(); return; }
  const overview = document.getElementById('new-sec-overview').value.trim();
  closeModal('modal-add-section');
  const res = await fetch(`/api/guides/${currentGuideId}/section`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({title, overview}),
  });
  if (res.ok) await loadGuide(currentGuideId);
  else alert('Failed to add section: ' + (await res.json()).error);
}

async function editSection(sectionId) {
  // kept for any legacy callers — delegates to inline edit
  secTitleEdit(sectionId);
}

function secTitleEdit(sectionId) {
  const span = document.getElementById('sec-title-' + sectionId);
  const inp = document.getElementById('sec-title-input-' + sectionId);
  if (!span || !inp) return;
  span.style.display = 'none';
  inp.style.display = 'inline-block';
  inp.focus();
  inp.select();
}
function secTitleCancel(sectionId) {
  const span = document.getElementById('sec-title-' + sectionId);
  const inp = document.getElementById('sec-title-input-' + sectionId);
  inp.value = span.textContent;
  inp.style.display = 'none';
  span.style.display = '';
}
async function secTitleSave(sectionId) {
  const span = document.getElementById('sec-title-' + sectionId);
  const inp = document.getElementById('sec-title-input-' + sectionId);
  const newTitle = inp.value.trim();
  inp.style.display = 'none';
  span.style.display = '';
  if (!newTitle || newTitle === span.textContent) return;
  span.textContent = newTitle;
  await fetch(`/api/guides/${currentGuideId}/section/${sectionId}`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({title: newTitle}),
  });
  const sec = currentGuide.sections.find(s => s.id === sectionId);
  if (sec) sec.title = newTitle;
}

function secOverviewEdit(sectionId, current) {
  const ta = document.getElementById('sec-overview-ta-' + sectionId);
  ta.value = current;
  document.getElementById('sec-overview-row-' + sectionId).style.display = 'none';
  document.getElementById('sec-overview-edit-' + sectionId).style.display = 'block';
  ta.focus();
}
function secOverviewCancel(sectionId) {
  document.getElementById('sec-overview-edit-' + sectionId).style.display = 'none';
  document.getElementById('sec-overview-row-' + sectionId).style.display = 'flex';
}
async function secOverviewSave(sectionId) {
  const text = document.getElementById('sec-overview-ta-' + sectionId).value.trim();
  await fetch(`/api/guides/${currentGuideId}/section/${sectionId}`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({overview: text}),
  });
  const sec = currentGuide.sections.find(s => s.id === sectionId);
  if (sec) sec.overview = text;
  const textEl = document.getElementById('sec-overview-text-' + sectionId);
  textEl.innerHTML = text || '<em style="color:var(--text2)">No overview — click Edit to add one</em>';
  secOverviewCancel(sectionId);
}

async function deleteSection(sectionId) {
  const titleEl = document.getElementById('sec-title-' + sectionId);
  const title = titleEl ? titleEl.textContent : 'this section';
  if (!confirm(`Delete section "${title}" and all its steps? This cannot be undone.`)) return;
  const res = await fetch(`/api/guides/${currentGuideId}/section/${sectionId}`, {method:'DELETE'});
  if (res.ok) await loadGuide(currentGuideId);
}

function renderStep(step, globalNum) {
  const shots = step.screenshots || [];
  const thumbsHtml = shots.map((ss, idx) => {
    const fname = ss.path.split('/').pop();
    const cap = ss.caption || fname;
    return `<div class="ss-thumb" draggable="true"
        ondragstart="ssDragStart(event,'${step.id}',${idx})"
        ondragover="ssDragOver(event,this)"
        ondrop="ssDrop(event,'${step.id}',${idx})"
        ondragleave="ssDragLeave(this)">
      <img src="/api/screenshots/file/${fname}" alt="${cap}" onclick="ssPreview('/api/screenshots/file/${fname}','${cap}')">
      <div class="ss-thumb-caption" title="${cap}">${cap}</div>
      <div class="ss-thumb-actions">
        <button class="ss-thumb-btn" title="Edit caption" onclick="ssEditCaption(event,'${step.id}',${idx},'${cap.replace(/'/g,"\\'")}')">✎</button>
        <button class="ss-thumb-btn" title="Remove" onclick="ssRemove(event,'${step.id}',${idx})">✕</button>
      </div>
    </div>`;
  }).join('');

  return `
    <div class="step-card" id="step-${step.id}">
      <div class="step-header">
        <div class="step-num">${globalNum != null ? globalNum : step.order}</div>
        <div class="step-title-text" id="step-title-text-${step.id}"
             ondblclick="editStepInline('${step.id}')"
             title="Double-click to edit">${step.title}</div>
        <button class="btn btn-ai btn-sm" onclick="rewriteStep('${step.id}')">✦ AI</button>
        <button class="btn btn-secondary btn-sm" onclick="editStepInline('${step.id}')">Edit</button>
      </div>
      <div class="step-body" id="step-body-${step.id}">${step.instruction}</div>
      ${step.expected_result ? `<div class="expected">✓ ${step.expected_result}</div>` : ''}

      <!-- Screenshot panel -->
      <div class="step-screenshots">
        <div class="step-screenshots-label">
          🖼 Screenshots (${shots.length})
          <button class="btn btn-secondary btn-sm" style="padding:1px 8px;font-size:.72rem" onclick="openSsRepo('${step.id}')">+ Add from Repository</button>
          <label class="btn btn-secondary btn-sm" style="padding:1px 8px;font-size:.72rem;cursor:pointer">
            ⬆ Upload
            <input type="file" accept=".png,.jpg,.jpeg" multiple style="display:none" onchange="ssUploadAndAttach(this,'${step.id}')">
          </label>
        </div>
        <div class="ss-thumb-row" id="ss-row-${step.id}">${thumbsHtml}</div>
        ${shots.length === 0 ? '<div style="font-size:.75rem;color:var(--text2)">No screenshots attached. Upload or pick from the repository.</div>' : ''}
      </div>

       <div id="step-edit-${step.id}" style="display:none;margin-top:.75rem">
         <div class="form-group" style="margin-bottom:.5rem">
           <label>Title</label>
           <input type="text" id="step-title-${step.id}" value="${step.title.replace(/"/g,'&quot;')}">
         </div>
         <div class="form-group" style="margin-bottom:.5rem">
           <label>Instruction</label>
           <div id="step-instr-editor-${step.id}" class="quill-host"></div>
         </div>
         <div class="form-group" style="margin-bottom:.5rem">
           <label>Expected Result</label>
           <input type="text" id="step-exp-${step.id}" value="${step.expected_result || ''}">
         </div>
         <div class="gap-row">
           <button class="btn btn-primary btn-sm" onclick="saveStepEdit('${step.id}')">Save</button>
           <button class="btn btn-secondary btn-sm" onclick="cancelStepEdit('${step.id}')">Cancel</button>
         </div>
       </div>
    </div>
  `;
}

function renderObjectives() {
  const container = document.getElementById('ed-objectives');
  const objs = currentGuide.learning_objectives || [];
  if (!objs.length) {
    container.innerHTML = '<p class="text-muted" style="font-size:.83rem">No objectives yet.</p>';
    return;
  }
  container.innerHTML = objs.map(o => `
    <div class="obj-item">
      <span class="bloom-badge bloom-${o.bloom_level}">${o.bloom_level}</span>
      <span style="flex:1;font-size:.83rem">${o.text}</span>
      <button class="btn-icon" onclick="deleteObjective('${o.id}')" title="Remove">✕</button>
    </div>
  `).join('');
}

function toggleSection(header) {
  header.closest('.section-block').classList.toggle('section-collapsed');
  header.querySelector('span').textContent =
    header.closest('.section-block').classList.contains('section-collapsed') ? '▶' : '▼';
}

// ── Quill editor registry ─────────────────────────────────────
const _quillEditors = {};   // stepId → Quill instance

// Common emoji set shown in the picker
const _EMOJIS = [
  '✅','❌','⚠️','ℹ️','📝','📌','🔑','💡','🚀','🎯','🔧','⚙️',
  '🖥️','💻','📱','🌐','🔒','🔓','📂','📁','📊','📈','📉','🗂️',
  '✔️','➡️','⬆️','⬇️','🔄','♻️','🛑','✋','👉','👆','👇','👋',
  '1️⃣','2️⃣','3️⃣','4️⃣','5️⃣','6️⃣','7️⃣','8️⃣','9️⃣','🔟',
  '🌟','⭐','💥','🎉','🎊','🏁','🔔','📣','💬','🗣️','📧','📞',
];

let _emojiPickerTarget = null;   // active Quill instance
let _emojiPickerEl = null;

function _getOrCreateEmojiPicker() {
  if (!_emojiPickerEl) {
    _emojiPickerEl = document.createElement('div');
    _emojiPickerEl.className = 'emoji-picker-popup';
    _emojiPickerEl.style.display = 'none';
    _EMOJIS.forEach(em => {
      const btn = document.createElement('button');
      btn.textContent = em;
      btn.title = em;
      btn.onclick = (e) => {
        e.stopPropagation();
        if (_emojiPickerTarget) {
          const range = _emojiPickerTarget.getSelection(true);
          const idx = range ? range.index : _emojiPickerTarget.getLength() - 1;
          _emojiPickerTarget.insertText(idx, em, 'user');
          _emojiPickerTarget.setSelection(idx + em.length, 0, 'silent');
        }
        _hideEmojiPicker();
      };
      _emojiPickerEl.appendChild(btn);
    });
    document.body.appendChild(_emojiPickerEl);
    document.addEventListener('click', (e) => {
      if (_emojiPickerEl && !_emojiPickerEl.contains(e.target)) _hideEmojiPicker();
    });
  }
  return _emojiPickerEl;
}

function _showEmojiPicker(btn, quill) {
  const picker = _getOrCreateEmojiPicker();
  _emojiPickerTarget = quill;
  const rect = btn.getBoundingClientRect();
  picker.style.display = 'grid';
  picker.style.top  = (rect.bottom + window.scrollY + 4) + 'px';
  picker.style.left = (rect.left  + window.scrollX)      + 'px';
}
function _hideEmojiPicker() {
  if (_emojiPickerEl) _emojiPickerEl.style.display = 'none';
  _emojiPickerTarget = null;
}

function _initQuill(stepId, htmlContent) {
  const host = document.getElementById('step-instr-editor-' + stepId);
  if (!host) return null;

  // Destroy previous instance if any
  if (_quillEditors[stepId]) {
    try { _quillEditors[stepId] = null; } catch(e) {}
    host.innerHTML = '';
  }

  const quill = new Quill(host, {
    theme: 'snow',
    placeholder: 'Write instruction…',
    modules: {
      toolbar: {
        container: [
          [{ header: [1, 2, 3, false] }],
          [{ size: ['small', false, 'large', 'huge'] }],
          ['bold', 'italic', 'underline', 'strike'],
          [{ color: [] }, { background: [] }],
          [{ list: 'ordered' }, { list: 'bullet' }],
          ['code-block', 'blockquote'],
          ['link'],
          ['clean'],
        ],
      },
    },
  });

  // Inject initial HTML content
  if (htmlContent && htmlContent.trim()) {
    const delta = quill.clipboard.convert({ html: htmlContent });
    quill.setContents(delta, 'silent');
  }

  // Append custom emoji button to toolbar
  const toolbar = host.querySelector('.ql-toolbar');
  if (toolbar) {
    const emojiBtn = document.createElement('button');
    emojiBtn.className = 'ql-emoji-btn';
    emojiBtn.title = 'Insert emoji';
    emojiBtn.textContent = '😊';
    emojiBtn.onclick = (e) => {
      e.preventDefault();
      e.stopPropagation();
      _showEmojiPicker(emojiBtn, quill);
    };
    toolbar.appendChild(emojiBtn);
  }

  _quillEditors[stepId] = quill;
  return quill;
}

// ── Inline step editing ───────────────────────────────────────
function editStepInline(stepId) {
  document.getElementById('step-edit-' + stepId).style.display = 'block';
  document.getElementById('step-' + stepId).classList.add('editing');
  // Find the current instruction for this step and init the editor
  const step = currentGuide && currentGuide.sections
    ? currentGuide.sections.flatMap(s => s.steps).find(s => s.id === stepId)
    : null;
  setTimeout(() => _initQuill(stepId, step ? (step.instruction || '') : ''), 30);
}
function cancelStepEdit(stepId) {
  document.getElementById('step-edit-' + stepId).style.display = 'none';
  document.getElementById('step-' + stepId).classList.remove('editing');
}
async function saveStepEdit(stepId) {
  const title = document.getElementById('step-title-' + stepId).value.trim();
  const exp   = document.getElementById('step-exp-' + stepId).value;
  // Get HTML from Quill; fall back to empty string
  const quill = _quillEditors[stepId];
  const instr = quill ? quill.getSemanticHTML() : '';
  await fetch(`/api/guides/${currentGuideId}/step/${stepId}`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({title, instruction: instr, expected_result: exp}),
  });
  await loadGuide(currentGuideId);
}

// ── Annotation editor ────────────────────────────────────────
let _annFilename = '';
let _annContext  = {type: 'repo', sid: null};  // 'repo' | 'session'
let _annHistory = [];      // array of ImageData snapshots for undo
let _annDrawing = false;
let _annStart = {x:0, y:0};
let _annTextEl = null;

function annSetTool(tool) {
  _annTool = tool;
  document.querySelectorAll('.ann-tool-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('ann-tool-' + tool).classList.add('active');
  document.getElementById('ann-font-row').style.display    = tool === 'text'      ? 'flex' : 'none';
  document.getElementById('ann-opacity-row').style.display = tool === 'highlight' ? 'flex' : 'none';
  // Commit any open text box
  _annCommitText();
}

function _annCtx()     { return document.getElementById('ann-canvas').getContext('2d'); }
function _annOvCtx()   { return document.getElementById('ann-overlay').getContext('2d'); }
function _annCanvas()  { return document.getElementById('ann-canvas'); }
function _annOverlay() { return document.getElementById('ann-overlay'); }

function _annPos(e) {
  const cv = _annOverlay();
  const rect = cv.getBoundingClientRect();
  const clientX = e.touches ? e.touches[0].clientX : e.clientX;
  const clientY = e.touches ? e.touches[0].clientY : e.clientY;
  return { x: (clientX - rect.left) * (cv.width / rect.width),
           y: (clientY - rect.top)  * (cv.height / rect.height) };
}

function _annPushHistory() {
  const cv = _annCanvas();
  _annHistory.push(_annCtx().getImageData(0, 0, cv.width, cv.height));
  if (_annHistory.length > 40) _annHistory.shift();
}

function annUndo() {
  if (!_annHistory.length) return;
  const cv = _annCanvas();
  _annCtx().putImageData(_annHistory.pop(), 0, 0);
}

function annClear() {
  _annPushHistory();
  const cv = _annCanvas();
  // Redraw only the base image
  const img = new Image();
  img.onload = () => { _annCtx().drawImage(img, 0, 0, cv.width, cv.height); };
  img.src = _annImgSrc;
}

let _annImgSrc = '';

function repoAnnotate(ev, filename, url) {
  ev.stopPropagation();
  _annFilename = filename;
  _annImgSrc   = url;
  _annHistory  = [];
  const modal = document.getElementById('modal-annotate');
  modal.classList.add('open');

  const img = new Image();
  img.crossOrigin = 'anonymous';
  img.onload = () => {
    const cv  = _annCanvas();
    const ov  = _annOverlay();
    // Scale to fit viewport (max 90vw / 85vh)
    const maxW = Math.floor(window.innerWidth  * 0.90);
    const maxH = Math.floor(window.innerHeight * 0.82);
    const scale = Math.min(1, maxW / img.naturalWidth, maxH / img.naturalHeight);
    const w = Math.round(img.naturalWidth  * scale);
    const h = Math.round(img.naturalHeight * scale);
    cv.width = ov.width = w;
    cv.height = ov.height = h;
    _annCtx().drawImage(img, 0, 0, w, h);
    _annBindEvents(ov);
  };
  img.onerror = () => {
    // Fallback: try direct URL without cache-busting
    img.src = url;
  };
  img.src = url + '?_=' + Date.now();
}

function _annBindEvents(ov) {
  // Remove old listeners by cloning
  const fresh = ov.cloneNode(true);
  ov.parentNode.replaceChild(fresh, ov);

  fresh.addEventListener('mousedown',  _annOnDown);
  fresh.addEventListener('mousemove',  _annOnMove);
  fresh.addEventListener('mouseup',    _annOnUp);
  fresh.addEventListener('mouseleave', _annOnUp);
  fresh.addEventListener('click',      _annOnClick);
}

function _annOnDown(e) {
  if (_annTool === 'text') return;
  _annCommitText();
  _annDrawing = true;
  _annStart = _annPos(e);
  if (_annTool === 'pen' || _annTool === 'highlight') {
    _annPushHistory();
    const ctx = _annCtx();
    const color = document.getElementById('ann-color').value;
    const size  = parseInt(document.getElementById('ann-size').value);
    if (_annTool === 'highlight') {
      const opacity = parseInt(document.getElementById('ann-opacity').value) / 100;
      ctx.globalAlpha = opacity;
      ctx.strokeStyle = color;
      ctx.lineWidth   = size * 6;
      ctx.lineCap     = 'square';
    } else {
      ctx.globalAlpha = 1;
      ctx.strokeStyle = color;
      ctx.lineWidth   = size;
      ctx.lineCap     = 'round';
    }
    ctx.beginPath();
    ctx.moveTo(_annStart.x, _annStart.y);
  }
}

function _annOnMove(e) {
  if (!_annDrawing) return;
  const pos = _annPos(e);
  if (_annTool === 'pen' || _annTool === 'highlight') {
    const ctx = _annCtx();
    ctx.lineTo(pos.x, pos.y);
    ctx.stroke();
  } else {
    // Preview shape on overlay
    const ov  = document.getElementById('ann-overlay');
    const oct = _annOvCtx();
    oct.clearRect(0, 0, ov.width, ov.height);
    const color = document.getElementById('ann-color').value;
    const size  = parseInt(document.getElementById('ann-size').value);
    oct.strokeStyle = color;
    oct.lineWidth   = size;
    oct.globalAlpha = 1;
    if (_annTool === 'rect') {
      oct.strokeRect(_annStart.x, _annStart.y,
                     pos.x - _annStart.x, pos.y - _annStart.y);
    } else if (_annTool === 'arrow') {
      _annDrawArrow(oct, _annStart.x, _annStart.y, pos.x, pos.y, color, size);
    }
  }
}

function _annOnUp(e) {
  if (!_annDrawing) return;
  _annDrawing = false;
  const pos = _annPos(e);
  if (_annTool === 'rect' || _annTool === 'arrow') {
    _annPushHistory();
    const ctx   = _annCtx();
    const color = document.getElementById('ann-color').value;
    const size  = parseInt(document.getElementById('ann-size').value);
    ctx.strokeStyle = color;
    ctx.lineWidth   = size;
    ctx.globalAlpha = 1;
    if (_annTool === 'rect') {
      ctx.strokeRect(_annStart.x, _annStart.y,
                     pos.x - _annStart.x, pos.y - _annStart.y);
    } else {
      _annDrawArrow(ctx, _annStart.x, _annStart.y, pos.x, pos.y, color, size);
    }
    // Clear overlay
    const ov = document.getElementById('ann-overlay');
    _annOvCtx().clearRect(0, 0, ov.width, ov.height);
  }
  if (_annTool === 'pen' || _annTool === 'highlight') {
    _annCtx().globalAlpha = 1;
  }
}

function _annOnClick(e) {
  if (_annTool !== 'text') return;
  _annCommitText();
  const pos  = _annPos(e);
  const size = parseInt(document.getElementById('ann-font-size').value);
  const color= document.getElementById('ann-color').value;
  const ti   = document.getElementById('ann-text-input');
  _annTextEl = { x: pos.x, y: pos.y, size, color };
  // Position the floating textarea
  const ov   = document.getElementById('ann-overlay');
  const rect = ov.getBoundingClientRect();
  const scaleX = rect.width  / ov.width;
  const scaleY = rect.height / ov.height;
  ti.style.display  = 'block';
  ti.style.left     = (pos.x * scaleX) + 'px';
  ti.style.top      = ((pos.y - size) * scaleY) + 'px';
  ti.style.fontSize = (size * scaleX) + 'px';
  ti.style.color    = color;
  ti.value = '';
  ti.rows  = 1;
  ti.focus();
}

function _annCommitText() {
  const ti = document.getElementById('ann-text-input');
  if (ti.style.display === 'none' || !_annTextEl) return;
  const text = ti.value.trim();
  if (text) {
    _annPushHistory();
    const ctx = _annCtx();
    ctx.globalAlpha = 1;
    ctx.fillStyle   = _annTextEl.color;
    ctx.font        = `bold ${_annTextEl.size}px -apple-system, sans-serif`;
    ctx.textBaseline = 'top';
    // Multiline support
    text.split('\\n').forEach((line, i) => {
      ctx.fillText(line, _annTextEl.x, _annTextEl.y + i * (_annTextEl.size * 1.2));
    });
  }
  ti.style.display = 'none';
  ti.value = '';
  _annTextEl = null;
}

function _annDrawArrow(ctx, x1, y1, x2, y2, color, size) {
  const angle   = Math.atan2(y2 - y1, x2 - x1);
  const headLen = Math.max(12, size * 4);
  ctx.save();
  ctx.strokeStyle = color;
  ctx.fillStyle   = color;
  ctx.lineWidth   = size;
  ctx.lineCap     = 'round';
  ctx.beginPath();
  ctx.moveTo(x1, y1);
  ctx.lineTo(x2, y2);
  ctx.stroke();
  // Arrowhead
  ctx.beginPath();
  ctx.moveTo(x2, y2);
  ctx.lineTo(x2 - headLen * Math.cos(angle - Math.PI/6),
             y2 - headLen * Math.sin(angle - Math.PI/6));
  ctx.lineTo(x2 - headLen * Math.cos(angle + Math.PI/6),
             y2 - headLen * Math.sin(angle + Math.PI/6));
  ctx.closePath();
  ctx.fill();
  ctx.restore();
}

async function annSave() {
  _annCommitText();
  const cv  = _annCanvas();
  const b64 = cv.toDataURL('image/png');
  const url = _annContext.type === 'session'
    ? '/api/sessions/' + _annContext.sid + '/screenshots/' + _annFilename + '/annotate'
    : '/api/screenshots/' + _annFilename + '/annotate';
  const res = await fetch(url, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({image: b64}),
  });
  const d = await res.json();
  if (d.error) { alert('Save failed: ' + d.error); return; }
  closeModal('modal-annotate');
  // Refresh the right view after save
  if (_annContext.type === 'session') {
    // reload thumbnails for this session if visible
    const wrap = document.getElementById('sess-thumbs-' + _annContext.sid);
    if (wrap && wrap.querySelector('img')) loadSessionThumbs(_annContext.sid);
  } else {
    await repoLoad();
  }
}

// Stroke size label
document.addEventListener('DOMContentLoaded', () => {
  const slider = document.getElementById('ann-size');
  if (slider) {
    slider.addEventListener('input', () => {
      document.getElementById('ann-size-label').textContent = slider.value;
    });
  }
  // Commit text on Enter (Shift+Enter = newline)
  const ti = document.getElementById('ann-text-input');
  if (ti) {
    ti.addEventListener('keydown', e => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); _annCommitText(); }
    });
  }
  // Keyboard shortcuts
  document.addEventListener('keydown', e => {
    const modal = document.getElementById('modal-annotate');
    if (!modal || !modal.classList.contains('open')) return;
    if ((e.metaKey || e.ctrlKey) && e.key === 'z') { e.preventDefault(); annUndo(); return; }
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    if (e.key === 'p' || e.key === 'P') annSetTool('pen');
    if (e.key === 'a' || e.key === 'A') annSetTool('arrow');
    if (e.key === 'r' || e.key === 'R') annSetTool('rect');
    if (e.key === 't' || e.key === 'T') annSetTool('text');
    if (e.key === 'h' || e.key === 'H') annSetTool('highlight');
  });
});

// ── Screenshot Repository ─────────────────────────────────────

async function openSsRepo(stepId) {
  _repoStepTarget = stepId;
  _repoSectionTarget = null;
  _repoBlockTarget = null;
  _repoSelected = null;
  document.getElementById('repo-attach-btn').disabled = true;
  document.getElementById('repo-search').value = '';
  await repoLoad();
  document.getElementById('modal-ss-repo').classList.add('open');
}

async function repoLoad() {
  const resp = await fetch('/api/screenshots');
  _repoItems = await resp.json();
  repoRender(_repoItems);
}

function repoRender(items) {
  const grid = document.getElementById('repo-grid');
  if (!items.length) {
    grid.innerHTML = '<div style="color:var(--text2);font-size:.83rem;grid-column:1/-1;padding:2rem;text-align:center">No screenshots yet. Upload some above.</div>';
    return;
  }
  grid.innerHTML = items.map(it => {
    const sel = _repoSelected === it.filename ? ' selected' : '';
    const cap = it.caption || it.filename;
    return `<div class="repo-thumb${sel}" onclick="repoSelect('${it.filename}',this)">
      <img src="${it.url}" alt="${cap}" loading="lazy">
      <div class="repo-ai-badge" title="AI caption">✦</div>
      <div class="repo-thumb-actions">
        <button class="ss-thumb-btn" title="Annotate" onclick="repoAnnotate(event,'${it.filename}','${it.url}')">✏️</button>
        <button class="ss-thumb-btn" title="AI caption" onclick="repoAiCaption(event,'${it.filename}')">✦ AI</button>
        <button class="ss-thumb-btn" title="Delete" onclick="repoDelete(event,'${it.filename}')">🗑</button>
      </div>
      <div class="repo-thumb-cap" title="${cap}">${cap}</div>
    </div>`;
  }).join('');
}

function repoFilter() {
  const q = document.getElementById('repo-search').value.toLowerCase();
  const filtered = q ? _repoItems.filter(it => (it.caption || it.filename).toLowerCase().includes(q)) : _repoItems;
  repoRender(filtered);
}

function repoSelect(filename, el) {
  _repoSelected = filename;
  document.querySelectorAll('.repo-thumb').forEach(t => t.classList.remove('selected'));
  el.classList.add('selected');
  document.getElementById('repo-attach-btn').disabled = false;
}

async function repoAttach() {
  if (!_repoSelected) return;
  const item = _repoItems.find(i => i.filename === _repoSelected);
  const caption = item ? (item.caption || '') : '';
  if (_repoBlockTarget) {
    const {secId, blockId} = _repoBlockTarget;
    await fetch(`/api/guides/${currentGuideId}/section/${secId}/blocks/${blockId}/attach-screenshot`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({filename: _repoSelected, caption}),
    });
  } else if (_repoStepTarget) {
    await fetch(`/api/guides/${currentGuideId}/step/${_repoStepTarget}/screenshots`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({filename: _repoSelected, caption}),
    });
  } else if (_repoSectionTarget) {
    await fetch(`/api/guides/${currentGuideId}/section/${_repoSectionTarget}/screenshots`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({filename: _repoSelected, caption}),
    });
  }
  _repoBlockTarget = null;
  closeModal('modal-ss-repo');
  await loadGuide(currentGuideId);
  showToast('Screenshot attached');
}

async function repoUpload(input) {
  const fd = new FormData();
  for (const f of input.files) fd.append('files', f);
  const resp = await fetch('/api/screenshots/upload', {method:'POST', body: fd});
  const saved = await resp.json();
  input.value = '';
  await repoLoad();
  showToast(`${saved.length} screenshot(s) uploaded`);
}

async function repoAiCaption(e, filename) {
  e.stopPropagation();
  const btn = e.target;
  btn.textContent = '…';
  const resp = await fetch(`/api/screenshots/${filename}/caption`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({})});
  const data = await resp.json();
  btn.textContent = '✦ AI';
  if (data.caption) {
    await repoLoad();
    showToast('Caption: ' + data.caption);
  } else {
    showToast('AI caption failed: ' + (data.error || 'unknown'), true);
  }
}

async function repoDelete(e, filename) {
  e.stopPropagation();
  if (!confirm('Delete this screenshot from the repository?')) return;
  await fetch(`/api/screenshots/${filename}/delete`, {method:'POST'});
  await repoLoad();
  if (_repoSelected === filename) { _repoSelected = null; document.getElementById('repo-attach-btn').disabled = true; }
}

// ── Step screenshot inline actions ────────────────────────────

async function ssRemove(e, stepId, idx) {
  e.stopPropagation();
  await fetch(`/api/guides/${currentGuideId}/step/${stepId}/screenshots/${idx}`, {method:'DELETE'});
  await loadGuide(currentGuideId);
}

async function ssEditCaption(e, stepId, idx, currentCap) {
  e.stopPropagation();
  const cap = prompt('Edit caption:', currentCap);
  if (cap === null) return;
  await fetch(`/api/guides/${currentGuideId}/step/${stepId}/screenshots/${idx}/caption`, {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({caption: cap}),
  });
  await loadGuide(currentGuideId);
}

async function ssUploadAndAttach(input, stepId) {
  const fd = new FormData();
  for (const f of input.files) fd.append('files', f);
  const resp = await fetch('/api/screenshots/upload', {method:'POST', body: fd});
  const saved = await resp.json();
  input.value = '';
  for (const s of saved) {
    await fetch(`/api/guides/${currentGuideId}/step/${stepId}/screenshots`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({filename: s.filename, caption: ''}),
    });
  }
  await loadGuide(currentGuideId);
  showToast(`${saved.length} screenshot(s) uploaded and attached`);
}

// Preview
function ssPreview(url, caption) {
  document.getElementById('ss-preview-img').src = url;
  document.getElementById('ss-preview-cap').textContent = caption;
  document.getElementById('modal-ss-preview').classList.add('open');
}

// Drag-to-reorder within step
let _dragStepId = null, _dragFromIdx = null;
function ssDragStart(e, stepId, idx) {
  _dragStepId = stepId; _dragFromIdx = idx;
  e.dataTransfer.effectAllowed = 'move';
}
function ssDragOver(e, el) { e.preventDefault(); el.classList.add('drag-over'); }
function ssDragLeave(el) { el.classList.remove('drag-over'); }
async function ssDrop(e, stepId, toIdx) {
  e.preventDefault();
  document.querySelectorAll('.ss-thumb').forEach(t => t.classList.remove('drag-over'));
  if (_dragStepId !== stepId || _dragFromIdx === toIdx) return;
  const step = currentGuide.sections.flatMap(s => s.steps).find(s => s.id === stepId);
  if (!step) return;
  const n = (step.screenshots || []).length;
  const order = Array.from({length: n}, (_, i) => i);
  order.splice(toIdx, 0, order.splice(_dragFromIdx, 1)[0]);
  await fetch(`/api/guides/${currentGuideId}/step/${stepId}/screenshots/reorder`, {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({order}),
  });
  await loadGuide(currentGuideId);
}

// ── Content block actions ─────────────────────────────────────

async function blockAddText(secId, afterId) {
  await fetch(`/api/guides/${currentGuideId}/section/${secId}/blocks`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({type:'text', content:'', after_id: afterId}),
  });
  await loadGuide(currentGuideId);
  // Auto-focus the new text block
  setTimeout(() => {
    const list = document.getElementById('block-list-' + secId);
    if (!list) return;
    const blocks = list.querySelectorAll('.block-text');
    if (blocks.length) {
      const last = blocks[blocks.length - 1];
      const body = last.querySelector('.block-text-body');
      if (body) body.click();
    }
  }, 100);
}

async function blockAddScreenshot(secId, afterId) {
  await fetch(`/api/guides/${currentGuideId}/section/${secId}/blocks`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({type:'screenshot', content:'', after_id: afterId}),
  });
  await loadGuide(currentGuideId);
}

function blockTextEdit(blockId, show) {
  const body = document.querySelector('#block-' + blockId + ' .block-text-body');
  const edit = document.getElementById('block-edit-' + blockId);
  if (!body || !edit) return;
  body.style.display = show ? 'none' : '';
  edit.style.display = show ? 'block' : 'none';
  if (show) {
    const ta = document.getElementById('block-ta-' + blockId);
    if (ta) { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); }
  }
}

async function blockTextSave(secId, blockId) {
  const ta = document.getElementById('block-ta-' + blockId);
  if (!ta) return;
  await fetch(`/api/guides/${currentGuideId}/section/${secId}/blocks/${blockId}`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({content: ta.value}),
  });
  await loadGuide(currentGuideId);
}

async function blockDelete(secId, blockId) {
  if (!confirm('Delete this block?')) return;
  await fetch(`/api/guides/${currentGuideId}/section/${secId}/blocks/${blockId}`, {method:'DELETE'});
  await loadGuide(currentGuideId);
}

async function blockSsCaption(secId, blockId, caption) {
  await fetch(`/api/guides/${currentGuideId}/section/${secId}/blocks/${blockId}`, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({caption}),
  });
}

async function blockSsPick(secId, blockId) {
  _repoStepTarget = null;
  _repoSectionTarget = null;
  _repoBlockTarget = {secId, blockId};
  _repoSelected = null;
  document.getElementById('repo-attach-btn').disabled = true;
  document.getElementById('repo-search').value = '';
  await repoLoad();
  document.getElementById('modal-ss-repo').classList.add('open');
}

async function blockSsUpload(input, secId, blockId) {
  const fd = new FormData();
  for (const f of input.files) fd.append('files', f);
  const resp = await fetch('/api/screenshots/upload', {method:'POST', body: fd});
  const saved = await resp.json();
  input.value = '';
  if (saved.length) {
    const meta = _repoItems.find(i => i.filename === saved[0].filename);
    await fetch(`/api/guides/${currentGuideId}/section/${secId}/blocks/${blockId}/attach-screenshot`, {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({filename: saved[0].filename, caption: ''}),
    });
    await repoLoad();
    await loadGuide(currentGuideId);
    showToast('Screenshot uploaded and attached');
  }
}

async function blockSsAiCaption(secId, blockId, filename) {
  if (!filename) { showToast('Pick a screenshot first', true); return; }
  const resp = await fetch(`/api/screenshots/${filename}/caption`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({})});
  const data = await resp.json();
  if (data.caption) {
    await blockSsCaption(secId, blockId, data.caption);
    await loadGuide(currentGuideId);
    showToast('AI caption: ' + data.caption);
  }
}

// Block drag-to-reorder
let _blockDragSecId = null, _blockDragId = null;
function blockDragStart(e, secId, blockId) {
  _blockDragSecId = secId; _blockDragId = blockId;
  e.dataTransfer.effectAllowed = 'move';
}
function blockDragOver(e, el) { e.preventDefault(); el.classList.add('drag-over'); }
function blockDragLeave(el) { el.classList.remove('drag-over'); }
async function blockDrop(e, secId, targetBlockId) {
  e.preventDefault();
  document.querySelectorAll('.block-item').forEach(b => b.classList.remove('drag-over'));
  if (_blockDragSecId !== secId || _blockDragId === targetBlockId) return;
  const sec = currentGuide.sections.find(s => s.id === secId);
  if (!sec) return;
  const ids = (sec.blocks || []).map(b => b.id);
  const fromIdx = ids.indexOf(_blockDragId);
  const toIdx = ids.indexOf(targetBlockId);
  if (fromIdx === -1 || toIdx === -1) return;
  ids.splice(toIdx, 0, ids.splice(fromIdx, 1)[0]);
  await fetch(`/api/guides/${currentGuideId}/section/${secId}/blocks/reorder`, {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({order: ids}),
  });
  await loadGuide(currentGuideId);
}

// ── Section drag-to-reorder ────────────────────────────────────
let _secDragId = null;
function secDragStart(e, secId) {
  _secDragId = secId;
  e.dataTransfer.effectAllowed = 'move';
  e.stopPropagation();
}
function secDragOver(e, el) {
  e.preventDefault();
  e.stopPropagation();
  if (el.id !== 'secblock-' + _secDragId) el.classList.add('sec-drag-over');
}
function secDragLeave(el) { el.classList.remove('sec-drag-over'); }
async function secDrop(e, targetSecId) {
  e.preventDefault();
  e.stopPropagation();
  document.querySelectorAll('.section-block').forEach(b => b.classList.remove('sec-drag-over'));
  if (!_secDragId || _secDragId === targetSecId) return;
  const ids = currentGuide.sections.map(s => s.id);
  const fromIdx = ids.indexOf(_secDragId);
  const toIdx = ids.indexOf(targetSecId);
  if (fromIdx === -1 || toIdx === -1) return;
  ids.splice(toIdx, 0, ids.splice(fromIdx, 1)[0]);
  const res = await fetch(`/api/guides/${currentGuideId}/sections/reorder`, {
    method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({order: ids}),
  });
  const data = await res.json();
  if (data.guide) { currentGuide = data.guide; renderGuide(); }
  else await loadGuide(currentGuideId);
}

// ── AI rewrites ───────────────────────────────────────────────
let _aiCallback = null;

function openAiModal(title, callback) {
  document.getElementById('modal-ai-title').textContent = title;
  document.getElementById('modal-ai-feedback').value = '';
  _aiCallback = callback;
  document.getElementById('modal-ai-confirm').onclick = () => {
    closeModal('modal-ai');
    callback(document.getElementById('modal-ai-feedback').value);
  };
  document.getElementById('modal-ai').classList.add('open');
}

async function rewriteStep(stepId) {
  openAiModal('Rewrite Step with AI', async (feedback) => {
    const card = document.getElementById('step-' + stepId);
    card.style.opacity = '.5';
    const res = await fetch(`/api/guides/${currentGuideId}/step/${stepId}/rewrite`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({feedback}),
    });
    card.style.opacity = '1';
    if (res.ok) { await loadGuide(currentGuideId); }
    else { alert('AI rewrite failed: ' + (await res.json()).error); }
  });
}

async function rewriteSection(sectionId) {
  openAiModal('Rewrite Section Overview with AI', async (feedback) => {
    await fetch(`/api/guides/${currentGuideId}/section/${sectionId}/rewrite`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({feedback}),
    });
    await loadGuide(currentGuideId);
  });
}

async function rewriteIntro() {
  openAiModal('Rewrite Introduction with AI', async (feedback) => {
    await fetch(`/api/guides/${currentGuideId}/introduction/rewrite`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({feedback}),
    });
    await loadGuide(currentGuideId);
  });
}

function editIntro() {
  const ta = document.getElementById('ed-intro-ta');
  ta.value = currentGuide.introduction || '';
  document.getElementById('ed-intro').style.display = 'none';
  document.getElementById('ed-intro-edit').style.display = 'block';
  ta.focus();
}
function cancelIntro() {
  document.getElementById('ed-intro-edit').style.display = 'none';
  document.getElementById('ed-intro').style.display = '';
}
async function saveIntro() {
  const text = document.getElementById('ed-intro-ta').value;
  await fetch(`/api/guides/${currentGuideId}/introduction`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({text}),
  });
  currentGuide.introduction = text;
  document.getElementById('ed-intro').textContent = text || '(No introduction yet)';
  cancelIntro();
}

async function rewriteConclusion() {
  openAiModal('Rewrite Conclusion with AI', async (feedback) => {
    await fetch(`/api/guides/${currentGuideId}/conclusion/rewrite`, {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify({feedback}),
    });
    await loadGuide(currentGuideId);
  });
}

function editConclusion() {
  const ta = document.getElementById('ed-conclusion-ta');
  ta.value = currentGuide.conclusion || '';
  document.getElementById('ed-conclusion').style.display = 'none';
  document.getElementById('ed-conclusion-edit').style.display = 'block';
  ta.focus();
}
function cancelConclusion() {
  document.getElementById('ed-conclusion-edit').style.display = 'none';
  document.getElementById('ed-conclusion').style.display = '';
}
async function saveConclusion() {
  const text = document.getElementById('ed-conclusion-ta').value;
  await fetch(`/api/guides/${currentGuideId}/conclusion`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({text}),
  });
  currentGuide.conclusion = text;
  document.getElementById('ed-conclusion').textContent = text || '(No conclusion yet)';
  cancelConclusion();
}

async function addObjective() {
  const desc = document.getElementById('new-obj-input').value.trim();
  if (!desc) return;
  document.getElementById('new-obj-input').value = '';
  await fetch(`/api/guides/${currentGuideId}/objective`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({description: desc}),
  });
  await loadGuide(currentGuideId);
}

async function deleteObjective(objId) {
  await fetch(`/api/guides/${currentGuideId}/objective/${objId}`, {method:'DELETE'});
  await loadGuide(currentGuideId);
}

async function addStepToSection(sectionId) {
  const title = prompt('Step title:');
  if (!title) return;
  const desc = prompt('What should this step cover?');
  if (!desc) return;
  await fetch(`/api/guides/${currentGuideId}/step`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({section_id: sectionId, title, description: desc}),
  });
  await loadGuide(currentGuideId);
}

// ── Model selector ─────────────────────────────────────────────────────────

// Models that get a recommended tip (id → tip text)
const MODEL_TIPS = {
  'claude-sonnet-4.6':  '★ Best balance of quality & speed for lab guides',
  'claude-sonnet-5':    '★ Recommended — sharpest reasoning, best for complex guides',
  'claude-opus-4.8':    '⚡ Most powerful Claude — slower, great for full rewrites',
  'gpt-4o':             '★ Strong all-rounder from OpenAI',
  'gpt-4o-2024-11-20':  'Latest GPT-4o snapshot — very reliable',
  'gpt-5.5':            '⚡ Cutting-edge GPT — best for nuanced suggestions',
  'claude-haiku-4.5':   '⚡ Fastest & cheapest — good for quick edits',
  'gpt-5-mini':         '⚡ Fast & lightweight — good for quick edits',
};

const RECOMMENDED_MODEL = 'claude-sonnet-5';

async function loadModels() {
  const sel = document.getElementById('model-select');
  const tip = document.getElementById('model-tip');
  if (!sel) return;
  try {
    const res = await fetch('/api/models');
    const data = await res.json();
    if (data.error) { sel.innerHTML = `<option>${data.error}</option>`; return; }
    sel.innerHTML = data.models.map(m => {
      const isRec = m.id === RECOMMENDED_MODEL;
      const label = isRec ? `${m.name} ✦` : m.name;
      return `<option value="${m.id}" ${m.id === data.current ? 'selected' : ''}>${label}</option>`;
    }).join('');
    _updateModelTip(data.current, tip);
  } catch(e) {
    sel.innerHTML = '<option value="claude-sonnet-4.6">claude-sonnet-4.6</option>';
  }
}

function _updateModelTip(modelId, tipEl) {
  const t = tipEl || document.getElementById('model-tip');
  const msg = MODEL_TIPS[modelId];
  if (msg) {
    t.textContent = msg;
    t.style.display = 'inline-block';
  } else {
    t.style.display = 'none';
  }
}

async function setModel(modelId) {
  const status = document.getElementById('model-status');
  const tip = document.getElementById('model-tip');
  _updateModelTip(modelId, tip);
  status.textContent = 'Switching…';
  const res = await fetch('/api/settings/model', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({model: modelId}),
  });
  const data = await res.json();
  status.textContent = data.ok ? `Active model: ${modelId}` : `Error: ${data.error}`;
  setTimeout(() => { status.textContent = ''; }, 3000);
}

// Load models when AI Tools tab is opened

async function suggestImprovements() {
  const container = document.getElementById('suggestion-list');
  container.innerHTML = '<div class="spinner"></div> Reviewing guide…';
  const res = await fetch(`/api/guides/${currentGuideId}/suggest`, {method:'POST'});
  const data = await res.json();
  if (data.error) { container.innerHTML = `<div style="color:var(--red)">${data.error}</div>`; return; }
  if (!data.suggestions || !data.suggestions.length) {
    container.innerHTML = '<div style="color:var(--text2);font-size:.83rem">No suggestions — guide looks great!</div>';
    return;
  }
  container.innerHTML = data.suggestions.map((s, i) => `
    <div class="suggestion-item" id="suggestion-${i}" style="display:flex;align-items:flex-start;gap:.75rem">
      <div style="flex:1">💡 ${s}</div>
      <button class="btn btn-ai" style="flex:0 0 auto;font-size:.75rem;padding:.3rem .75rem;white-space:nowrap"
        onclick="applySuggestion(${i}, ${JSON.stringify(s).replace(/"/g, '&quot;')})">
        ✓ Accept
      </button>
    </div>
  `).join('');
}

async function applySuggestion(idx, suggestion) {
  const row = document.getElementById(`suggestion-${idx}`);
  const btn = row.querySelector('button');
  btn.disabled = true;
  btn.textContent = '⏳ Applying…';
  const res = await fetch(`/api/guides/${currentGuideId}/apply-suggestion`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({suggestion}),
  });
  const data = await res.json();
  if (data.error) {
    btn.disabled = false;
    btn.textContent = '✓ Accept';
    alert('Failed to apply: ' + data.error);
    return;
  }
  // Mark as applied and reload the guide editor
  row.style.borderLeftColor = 'var(--green)';
  row.style.opacity = '.6';
  btn.textContent = '✓ Applied';
  await loadGuide(currentGuideId);
}

// ── Preview ───────────────────────────────────────────────────
async function loadPreview() {
  const pane = document.getElementById('ed-preview');
  pane.innerHTML = '<div class="spinner"></div> Generating preview...';
  const res = await fetch(`/api/guides/${currentGuideId}/preview`);
  const data = await res.json();
  if (data.error) {
    pane.innerHTML = `<p style="color:red">Preview error: ${data.error}</p>`;
    return;
  }
  pane.innerHTML = data.html || '<p>Preview unavailable</p>';
  // Wire up click-to-zoom on all preview figures
  pane.querySelectorAll('.prev-figure img').forEach(img => {
    img.addEventListener('click', () => {
      const cap = img.closest('.prev-figure').querySelector('.prev-caption');
      ssPreview(img.src, cap ? cap.textContent : '');
    });
  });
}

// ── Export ────────────────────────────────────────────────────
// ── GitHub Publish ────────────────────────────────────────────────────────

async function loadSyncConfig(guideId) {
  const res = await fetch(`/api/guides/${guideId}/sync-config`);
  const data = await res.json();
  const repoEl = document.getElementById('pub-repo');
  const branchEl = document.getElementById('pub-branch');
  const lastEl = document.getElementById('pub-last');
  if (repoEl) repoEl.value = data.github_repo || '';
  if (branchEl) branchEl.value = data.github_branch || 'main';
  if (lastEl) lastEl.textContent = data.last_published
    ? 'Last published: ' + new Date(data.last_published + 'Z').toLocaleString()
    : '';
}

async function saveSyncConfig() {
  const repo = document.getElementById('pub-repo').value.trim();
  const branch = document.getElementById('pub-branch').value.trim() || 'main';
  const status = document.getElementById('pub-config-status');
  const res = await fetch(`/api/guides/${currentGuideId}/sync-config`, {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({github_repo: repo, github_branch: branch}),
  });
  const data = await res.json();
  status.textContent = data.ok ? '✓ Saved' : ('Error: ' + data.error);
  setTimeout(() => { status.textContent = ''; }, 2500);
}

async function saveAndPublish() {
  await saveSyncConfig();
  const log = document.getElementById('pub-log');
  const lastEl = document.getElementById('pub-last');
  log.style.display = 'block';
  log.innerHTML = '<div class="progress-line">Starting publish…</div>';

  const es = new EventSource(`/api/guides/${currentGuideId}/publish`);
  es.onmessage = (e) => {
    const evt = JSON.parse(e.data);
    const cls = evt.type === 'error' ? ' error' : evt.type === 'done' ? ' done' : '';
    log.innerHTML += `<div class="progress-line${cls}">${evt.message}</div>`;
    log.scrollTop = log.scrollHeight;
    if (evt.type === 'done') {
      es.close();
      if (lastEl && evt.last_published)
        lastEl.textContent = 'Last published: ' + new Date(evt.last_published + 'Z').toLocaleString();
    }
    if (evt.type === 'error') es.close();
  };
  es.onerror = () => {
    log.innerHTML += '<div class="progress-line error">Connection lost</div>';
    es.close();
  };
}

async function exportGuide(fmt) {
  const card = event.currentTarget;
  card.classList.add('loading');
  card.querySelector('.export-icon').textContent = '⏳';
  const logBox = document.getElementById('export-log');
  logBox.style.display = 'block';
  logBox.innerHTML = `<div class="progress-line">Exporting as ${fmt}...</div>`;

  const res = await fetch(`/api/guides/${currentGuideId}/export/${fmt}`, {method:'POST'});
  const data = await res.json();

  card.classList.remove('loading');
  const icons = {markdown:'📝',pdf:'📕',html:'🌐',docx:'📘',mkdocs:'🏗️'};
  card.querySelector('.export-icon').textContent = icons[fmt] || '📄';

  if (data.path) {
    logBox.innerHTML += `<div class="progress-line done">✓ Saved: ${data.path}</div>`;
    if (fmt !== 'mkdocs') {
      logBox.innerHTML += `<div class="progress-line"><a href="/api/guides/${currentGuideId}/export/${fmt}/download" style="color:var(--accent)">⬇ Download</a></div>`;
    }
  } else {
    logBox.innerHTML += `<div class="progress-line error">✗ ${data.error}</div>`;
  }
}

// ── Metadata ──────────────────────────────────────────────────
function openMetaModal() {
  const m = currentGuide.metadata;
  document.getElementById('meta-title').value = m.title;
  document.getElementById('meta-author').value = m.author || '';
  document.getElementById('meta-version').value = m.version;
  document.getElementById('meta-difficulty').value = m.difficulty;
  document.getElementById('meta-duration').value = m.lab_duration_minutes;
  document.getElementById('meta-tags').value = (m.tags || []).join(', ');
  document.getElementById('modal-meta').classList.add('open');
}

async function saveMetadata() {
  closeModal('modal-meta');
  await fetch(`/api/guides/${currentGuideId}/metadata`, {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      title: document.getElementById('meta-title').value,
      author: document.getElementById('meta-author').value,
      version: document.getElementById('meta-version').value,
      difficulty: document.getElementById('meta-difficulty').value,
      duration: parseInt(document.getElementById('meta-duration').value),
      tags: document.getElementById('meta-tags').value.split(',').map(t=>t.trim()).filter(Boolean),
    }),
  });
  await loadGuide(currentGuideId);
  loadLibrary();
}

// ── New guide ─────────────────────────────────────────────────
function openNewGuideModal() {
  document.getElementById('ng-title').value = '';
  document.getElementById('ng-author').value = '';
  document.getElementById('modal-new-guide').classList.add('open');
  setTimeout(() => document.getElementById('ng-title').focus(), 50);
}

async function createGuide() {
  const title = document.getElementById('ng-title').value.trim();
  if (!title) { alert('Title is required'); return; }
  closeModal('modal-new-guide');
  const res = await fetch('/api/guides', {
    method: 'POST', headers: {'Content-Type':'application/json'},
    body: JSON.stringify({
      title,
      author: document.getElementById('ng-author').value,
      difficulty: document.getElementById('ng-difficulty').value,
      duration: document.getElementById('ng-duration').value,
    }),
  });
  const data = await res.json();
  await loadLibrary();
  loadGuide(data.id);
  showMainTab('guides');
}

async function deleteCurrentGuide() {
  if (!confirm('Delete this guide? This cannot be undone.')) return;
  await fetch('/api/guides/' + currentGuideId, {method:'DELETE'});
  currentGuideId = null;
  currentGuide = null;
  document.getElementById('guide-editor').style.display = 'none';
  document.getElementById('no-guide').style.display = 'flex';
  loadLibrary();
}

// ── Recording ─────────────────────────────────────────────────
async function startRecording() {
  const audio = document.getElementById('rec-audio').checked;
  const res = await fetch('/api/record/start', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({audio}),
  });
  const data = await res.json();
  if (data.error) { alert('Recording error: ' + data.error); return; }
  activeRecordingSession = data.session_id;
  recStartTime = Date.now();
  document.getElementById('rec-dot').classList.add('active');
  document.getElementById('rec-status-text').textContent = 'Recording... (session: ' + data.session_id + ')';
  document.getElementById('rec-start-btn').disabled = true;
  document.getElementById('rec-stop-btn').disabled = false;
  document.getElementById('rec-screenshot-btn').disabled = false;
  recTimerInterval = setInterval(() => {
    const elapsed = Math.floor((Date.now() - recStartTime) / 1000);
    const m = String(Math.floor(elapsed/60)).padStart(2,'0');
    const s = String(elapsed%60).padStart(2,'0');
    document.getElementById('rec-timer').textContent = m + ':' + s;
  }, 1000);
}

async function stopRecording() {
  clearInterval(recTimerInterval);
  const res = await fetch('/api/record/stop', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({session_id: activeRecordingSession}),
  });
  const data = await res.json();
  document.getElementById('rec-dot').classList.remove('active');
  document.getElementById('rec-status-text').textContent = 'Not recording';
  document.getElementById('rec-start-btn').disabled = false;
  document.getElementById('rec-stop-btn').disabled = true;
  document.getElementById('rec-screenshot-btn').disabled = true;

  if (data.video_path) {
    lastVideoPath = data.video_path;
    const box = document.getElementById('rec-result');
    box.style.display = 'block';
    box.innerHTML = `<div class="progress-line done">✓ Saved: ${data.video_path}</div><div class="progress-line">Duration: ${data.duration_s}s · ${data.screenshots} screenshots</div>`;
    document.getElementById('rec-ingest-card').style.display = 'block';
  }
  activeRecordingSession = null;
}

function takeScreenshot() {
  // Fire immediately — no modal. Sequence number is auto-assigned server-side.
  _doTakeScreenshot();
}

async function _doTakeScreenshot() {
  const btn = document.getElementById('rec-screenshot-btn');
  btn.disabled = true;
  btn.textContent = '📸 Capturing…';
  try {
    const res = await fetch('/api/record/screenshot', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({session_id: activeRecordingSession, label: ''}),
    });
    const data = await res.json();
    if (data.error) throw new Error(data.error);
    // Flash toast
    _showScreenshotToast(data.seq, data.elapsed_s);
    const box = document.getElementById('rec-result');
    box.style.display = 'block';
    box.innerHTML += `<div class="progress-line">📸 Step ${data.seq} — ${data.elapsed_s}s into recording</div>`;
    box.scrollTop = box.scrollHeight;
  } catch(err) {
    alert('Screenshot failed: ' + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '📸 Screenshot';
  }
}

function openCapturePanel() {
  window.open('/capture', 'capture-panel',
    'popup=yes,width=340,height=560,resizable=yes,menubar=no,toolbar=no,location=no,status=no,scrollbars=yes');
}

function _showScreenshotToast(seq, elapsed) {
  let toast = document.getElementById('ss-toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'ss-toast';
    toast.style.cssText = [
      'position:fixed', 'bottom:2rem', 'right:2rem', 'z-index:9999',
      'background:var(--accent)', 'color:#fff', 'font-weight:700',
      'font-size:1rem', 'padding:.7rem 1.4rem', 'border-radius:8px',
      'box-shadow:0 4px 20px rgba(0,0,0,.5)', 'opacity:0',
      'transition:opacity .2s', 'pointer-events:none',
    ].join(';');
    document.body.appendChild(toast);
  }
  toast.textContent = `📸 Screenshot ${seq} captured (${elapsed}s)`;
  toast.style.opacity = '1';
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => { toast.style.opacity = '0'; }, 2000);
}

async function ingestLastRecording() {
  const title = document.getElementById('rec-ingest-title').value.trim();
  if (!title) { alert('Enter a lab title'); return; }
  if (!lastVideoPath) return;
  // lastVideoPath is already on the server — pass directly without re-uploading
  document.getElementById('ingest-video-title').value = title;
  showMainTab('ingest');
  // Kick off ingestion directly from the server path
  startIngestJob('/api/ingest/video', {
    video_path: lastVideoPath,
    title,
    frame_interval: parseFloat(document.getElementById('ingest-interval')?.value) || 5,
  });
}

// ── Ingestion ─────────────────────────────────────────────────
// ── Drop-zone helpers ──────────────────────────────────────────────────────
let _docFile = null, _videoFile = null, _ssFiles = null;

function onDragOver(e, zoneId) {
  e.preventDefault();
  document.getElementById(zoneId).classList.add('drag-over');
}
function onDragLeave(zoneId) {
  document.getElementById(zoneId).classList.remove('drag-over');
}

function onDropDoc(e) {
  e.preventDefault();
  onDragLeave('doc-drop-zone');
  const file = e.dataTransfer.files[0];
  if (file) _setDocFile(file);
}
function onDropVideo(e) {
  e.preventDefault();
  onDragLeave('video-drop-zone');
  const file = e.dataTransfer.files[0];
  if (file) _setVideoFile(file);
}
function onDropScreenshots(e) {
  e.preventDefault();
  onDragLeave('ss-drop-zone');
  const files = Array.from(e.dataTransfer.files).filter(f => /[.](png|jpg|jpeg)$/i.test(f.name));
  if (files.length) _setSsFiles(files);
}

function onDocFileSelected(input) { if (input.files[0]) _setDocFile(input.files[0]); }
function onVideoFileSelected(input) { if (input.files[0]) _setVideoFile(input.files[0]); }
function onScreenshotFilesSelected(input) {
  const files = Array.from(input.files);
  if (files.length) _setSsFiles(files);
}

function _setDocFile(file) {
  _docFile = file;
  document.getElementById('doc-selected').textContent = '✓ ' + file.name + ' (' + _fmtSize(file.size) + ')';
  document.getElementById('doc-ingest-btn').disabled = false;
}
function _setVideoFile(file) {
  _videoFile = file;
  document.getElementById('video-selected').textContent = '✓ ' + file.name + ' (' + _fmtSize(file.size) + ')';
  document.getElementById('video-ingest-btn').disabled = false;
}
function _setSsFiles(files) {
  _ssFiles = files;
  document.getElementById('ss-selected').textContent = '✓ ' + files.length + ' file(s) selected';
  document.getElementById('ss-ingest-btn').disabled = false;
}
function _fmtSize(n) {
  if (n < 1024) return n + ' B';
  if (n < 1048576) return (n/1024).toFixed(1) + ' KB';
  return (n/1048576).toFixed(1) + ' MB';
}

async function _uploadFile(endpoint, formData, logEl) {
  logEl.innerHTML += '<div class="progress-line">Uploading file...</div>';
  logEl.scrollTop = logEl.scrollHeight;
  const res = await fetch(endpoint, {method: 'POST', body: formData});
  const data = await res.json();
  if (data.error) throw new Error(data.error);
  logEl.innerHTML += `<div class="progress-line">✓ Uploaded: ${data.filename}</div>`;
  logEl.scrollTop = logEl.scrollHeight;
  return data;
}

// ── Ingest actions ─────────────────────────────────────────────────────────
async function ingestDocument() {
  if (!_docFile) { alert('Please select a document first'); return; }
  const card = document.getElementById('ingest-progress-card');
  const log = document.getElementById('ingest-log');
  card.style.display = 'block';
  log.innerHTML = '<div class="progress-line">Starting...</div>';
  try {
    const fd = new FormData();
    fd.append('file', _docFile);
    const {path} = await _uploadFile('/api/upload/document', fd, log);
    startIngestJob('/api/ingest/document', {document_path: path});
  } catch(err) {
    log.innerHTML += `<div class="progress-line error">✗ Upload failed: ${err.message}</div>`;
  }
}

async function ingestVideo() {
  if (!_videoFile) { alert('Please select a video file first'); return; }
  const title = document.getElementById('ingest-video-title').value.trim();
  if (!title) { alert('Lab title is required'); return; }
  const interval = parseFloat(document.getElementById('ingest-interval').value) || 5;
  const card = document.getElementById('ingest-progress-card');
  const log = document.getElementById('ingest-log');
  card.style.display = 'block';
  log.innerHTML = '<div class="progress-line">Starting...</div>';
  try {
    const fd = new FormData();
    fd.append('file', _videoFile);
    const {path} = await _uploadFile('/api/upload/video', fd, log);
    startIngestJob('/api/ingest/video', {video_path: path, title, frame_interval: interval});
  } catch(err) {
    log.innerHTML += `<div class="progress-line error">✗ Upload failed: ${err.message}</div>`;
  }
}

// ── Session picker ────────────────────────────────────────────
let _selectedSessionPath = null;
let _selectedSessionVideo = null;   // video_path if this session is a video recording

async function loadSessions() {
  const list = document.getElementById('session-list');
  if (!list) return;
  try {
    const res = await fetch('/api/sessions');
    const sessions = await res.json();
    if (!sessions.length) {
      list.innerHTML = '<div style="color:var(--text2);font-size:.8rem">No capture sessions yet. Use the Record tab to capture.</div>';
      return;
    }
    list.innerHTML = sessions.map(s => {
      const badge = s.has_video
        ? '<span style="background:#1d3461;color:#93c5fd;border-radius:3px;padding:1px 5px;font-size:.7rem;margin-left:.3rem">🎬 Video</span>'
        : '<span style="background:#1a2f1a;color:#86efac;border-radius:3px;padding:1px 5px;font-size:.7rem;margin-left:.3rem">📸 Screenshots</span>';
      const detail = s.has_video
        ? `🎬 video · ${s.size_mb} MB`
        : `📸 ${s.screenshot_count} screenshot${s.screenshot_count !== 1 ? 's' : ''} · ${s.size_mb} MB`;
      return '<div class="session-row" id="sr-' + s.session_id + '" onclick="selectSession(' +
        JSON.stringify(s.session_id) + ',' +
        JSON.stringify(s.folder_path) + ',' +
        JSON.stringify(s.video_path || '') + ')">' +
        '<span class="ss-id">' + s.name + badge + '</span>' +
        '<span class="ss-count">' + detail + '</span>' +
        '<span class="ss-date">' + s.recorded_at + '</span>' +
        '</div>';
    }).join('');
  } catch(e) {
    list.innerHTML = '<div style="color:var(--red);font-size:.8rem">Failed to load sessions</div>';
  }
}

function selectSession(sid, folderPath, videoPath) {
  _selectedSessionPath  = folderPath;
  _selectedSessionVideo = videoPath || null;
  _ssFiles = null;
  document.getElementById('ss-selected').textContent = '';
  document.querySelectorAll('.session-row').forEach(r => r.classList.remove('active'));
  const row = document.getElementById('sr-' + sid);
  if (row) row.classList.add('active');
  const btn = document.getElementById('ss-ingest-btn');
  btn.disabled = false;
  if (_selectedSessionVideo) {
    btn.textContent = '🎬 Generate Guide from Video';
    btn.style.background = 'var(--accent)';
  } else {
    btn.textContent = '⚡ Generate Guide with AI';
    btn.style.background = '';
  }
}

async function ingestScreenshots() {
  const title = document.getElementById('ingest-folder-title').value.trim();
  if (!title) { alert('Lab title is required'); return; }
  const card = document.getElementById('ingest-progress-card');
  const log  = document.getElementById('ingest-log');
  card.style.display = 'block';

  // Path A-video: selected session is a video recording
  if (_selectedSessionVideo) {
    log.innerHTML = '<div class="progress-line">Starting video ingestion — extracting frames…</div>';
    startIngestJob('/api/ingest/video', {video_path: _selectedSessionVideo, title});
    return;
  }

  // Path A-screenshots: use a selected session directly (no upload needed)
  if (_selectedSessionPath) {
    log.innerHTML = '<div class="progress-line">Using captured session screenshots…</div>';
    startIngestJob('/api/ingest/screenshots', {folder_path: _selectedSessionPath, title});
    return;
  }

  // Path B: uploaded files
  if (!_ssFiles || !_ssFiles.length) {
    alert('Select a recent session above, or upload screenshot files.');
    return;
  }
  log.innerHTML = '<div class="progress-line">Uploading screenshots...</div>';
  try {
    const fd = new FormData();
    for (const f of _ssFiles) fd.append('files', f);
    const {folder_path, count} = await _uploadFile('/api/upload/screenshots', fd, log);
    log.innerHTML += `<div class="progress-line">✓ ${count} screenshots saved</div>`;
    log.scrollTop = log.scrollHeight;
    startIngestJob('/api/ingest/screenshots', {folder_path, title});
  } catch(err) {
    log.innerHTML += `<div class="progress-line error">✗ Upload failed: ${err.message}</div>`;
  }
}

async function startIngestJob(url, body) {
  const card = document.getElementById('ingest-progress-card');
  const log = document.getElementById('ingest-log');
  card.style.display = 'block';
  log.innerHTML = '<div class="progress-line">Starting...</div>';
  log.scrollTop = log.scrollHeight;

  const res = await fetch(url, {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(body),
  });
  const {job_id, error} = await res.json();
  if (error) { log.innerHTML += `<div class="progress-line error">✗ ${error}</div>`; return; }

  const es = new EventSource(`/api/ingest/progress/${job_id}`);
  es.onmessage = async (e) => {
    const evt = JSON.parse(e.data);
    if (evt.type === 'ping') return;
    if (evt.type === 'progress') {
      log.innerHTML += `<div class="progress-line">${evt.message}</div>`;
      log.scrollTop = log.scrollHeight;
    } else if (evt.type === 'done') {
      log.innerHTML += `<div class="progress-line done">✓ Guide created: "${evt.title}" (${evt.sections} sections, ${evt.steps} steps)</div>`;
      log.scrollTop = log.scrollHeight;
      es.close();
      await loadLibrary();
      loadGuide(evt.guide_id);
      showMainTab('guides');
    } else if (evt.type === 'error') {
      log.innerHTML += `<div class="progress-line error">✗ ${evt.message}</div>`;
      es.close();
    } else if (evt.type === 'end') {
      es.close();
    }
  };
}

// ── Sessions tab ─────────────────────────────────────────────
// Store session data keyed by id so delegated handlers can look it up
const _sessData = {};

async function loadSessionsTab() {
  const grid = document.getElementById('sessions-grid');
  if (!grid) return;
  grid.innerHTML = '<div style="color:var(--text2)">Loading…</div>';
  try {
    const res = await fetch('/api/sessions');
    const sessions = await res.json();
    if (!sessions.length) {
      grid.innerHTML = '<div class="card" style="color:var(--text2)">No sessions yet. Use the Record tab to start capturing.</div>';
      return;
    }
    // cache data for delegated handlers
    sessions.forEach(s => { _sessData[s.session_id] = s; });

    grid.innerHTML = sessions.map(s => {
      const modeLabels = {screenshots:'📸 Screenshots', video:'🎬 Video',
                          combo:'🎬+📸 Combo', audio:'🎙 Audio'};
      const modeBg     = {screenshots:'#1a2f1a', video:'#1d3461', combo:'#2a1f40', audio:'#1a2535'};
      const modeFg     = {screenshots:'#86efac', video:'#93c5fd', combo:'#c4b5fd', audio:'#7dd3fc'};
      const m = s.mode || (s.has_video ? 'video' : 'screenshots');
      const badge = '<span style="background:' + (modeBg[m]||'#333') + ';color:' + (modeFg[m]||'#ccc') +
        ';border-radius:3px;padding:1px 6px;font-size:.7rem;margin-left:.4rem">' + (modeLabels[m]||m) + '</span>';
      const detail = s.screenshot_count + ' shot' + (s.screenshot_count !== 1 ? 's' : '') +
        (s.has_video ? ' · video' : '') + (s.has_audio ? ' · 🎙 audio' : '') +
        ' · ' + s.size_mb + ' MB';

      const videoEl = s.has_video
        ? '<video controls style="width:100%;max-height:140px;border-radius:4px;margin-top:.4rem;background:#000">' +
            '<source src="/api/sessions/' + s.session_id + '/video">' +
          '</video>'
        : '';
      const audioEl = s.has_audio
        ? '<audio controls style="width:100%;margin-top:.4rem">' +
            '<source src="/api/sessions/' + s.session_id + '/audio" type="audio/mp4">' +
          '</audio>'
        : '';
      const videoIngestBtn = s.has_video
        ? '<div style="display:flex;align-items:center;gap:.5rem;margin-top:.4rem;flex-wrap:wrap">' +
            '<span style="font-size:.75rem;color:var(--text2)">Frame every</span>' +
            '<input type="number" id="fi-' + s.session_id + '" value="5" min="1" max="30" step="1"' +
            ' style="width:48px;background:var(--bg2);border:1px solid var(--border);border-radius:4px;color:var(--text);padding:2px 5px;font-size:.75rem">' +
            '<span style="font-size:.75rem;color:var(--text2)">sec</span>' +
            '<button class="btn btn-ai btn-sm" data-action="ingest-video" data-sid="' + s.session_id + '">🎬 Generate Guide</button>' +
          '</div>'
        : '';
      const thumbsRow = '<button class="btn btn-secondary btn-sm" data-action="show-thumbs" data-sid="' + s.session_id + '">Show screenshots ▸</button>';

      return '<div class="card" id="sess-card-' + s.session_id + '" style="padding:.75rem">' +
        '<div style="display:flex;align-items:flex-start;gap:.5rem;margin-bottom:.4rem">' +
          '<div style="flex:1;min-width:0">' +
            '<div style="font-weight:600;font-size:.95rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' +
              '<span class="sess-name-text" id="sess-name-' + s.session_id + '">' + s.name + '</span>' + badge +
            '</div>' +
            '<div style="font-size:.75rem;color:var(--text2);margin-top:.15rem">' +
              s.session_id + ' &nbsp;·&nbsp; ' + detail + ' &nbsp;·&nbsp; ' + s.recorded_at +
            '</div>' +
          '</div>' +
          '<div style="display:flex;gap:.35rem;flex-shrink:0">' +
            '<button class="btn btn-secondary btn-sm" data-action="rename" data-sid="' + s.session_id + '" title="Rename">✏️</button>' +
            '<button class="btn btn-secondary btn-sm" data-action="delete" data-sid="' + s.session_id + '" title="Delete" style="color:#f85149">🗑</button>' +
          '</div>' +
        '</div>' +
        videoEl + audioEl + videoIngestBtn +
        '<div id="sess-thumbs-' + s.session_id + '" style="display:flex;gap:6px;flex-wrap:wrap;margin-top:.4rem">' +
          thumbsRow +
        '</div>' +
      '</div>';
    }).join('');

  } catch(e) {
    grid.innerHTML = '<div style="color:var(--red)">Failed to load sessions: ' + e + '</div>';
  }
}

// Single delegated click handler on the grid — no inline onclick
document.addEventListener('click', function(e) {
  const btn = e.target.closest('[data-action]');
  if (!btn) return;
  const action = btn.dataset.action;
  const sid    = btn.dataset.sid;
  if (!sid) return;
  if (action === 'rename')       renameSession(sid);
  if (action === 'delete')       deleteSession(sid);
  if (action === 'show-thumbs')  loadSessionThumbs(sid);
  if (action === 'hide-thumbs')  hideSessThumb(sid);
  if (action === 'ingest-video') ingestVideoSession(sid, (_sessData[sid] || {}).video_path);
  if (action === 'preview-shot') openSessPreview(sid, btn.dataset.filename);
});

async function loadSessionThumbs(sid) {
  const wrap = document.getElementById('sess-thumbs-' + sid);
  if (!wrap) return;
  wrap.innerHTML = '<span style="color:var(--text2);font-size:.8rem">Loading…</span>';
  try {
    const res = await fetch('/api/sessions/' + sid + '/screenshots');
    const shots = await res.json();
    if (!shots.length) {
      wrap.innerHTML = '<span style="color:var(--text2);font-size:.8rem">No screenshots found</span>';
      return;
    }
    wrap.innerHTML =
      shots.map(sh =>
        '<img src="/api/sessions/' + sid + '/screenshots/' + encodeURIComponent(sh.filename) + '/thumb"' +
        ' title="' + sh.filename + '"' +
        ' data-action="preview-shot" data-sid="' + sid + '" data-filename="' + sh.filename.replace(/"/g,'&quot;') + '"' +
        ' style="height:90px;width:auto;border-radius:4px;border:1px solid var(--border);cursor:pointer;object-fit:cover">'
      ).join('') +
      '<button class="btn btn-secondary btn-sm" data-action="hide-thumbs" data-sid="' + sid + '" style="align-self:flex-start;margin-top:2px">▴ Hide</button>';
  } catch(e) {
    wrap.innerHTML = '<span style="color:var(--red);font-size:.8rem">Failed: ' + e + '</span>';
  }
}

function hideSessThumb(sid) {
  const wrap = document.getElementById('sess-thumbs-' + sid);
  if (wrap) wrap.innerHTML =
    '<button class="btn btn-secondary btn-sm" data-action="show-thumbs" data-sid="' + sid + '">Show screenshots ▸</button>';
}

function openSessPreview(sid, filename) {
  const img = document.getElementById('ss-preview-img');
  const cap = document.getElementById('ss-preview-cap');
  const url = '/api/sessions/' + sid + '/screenshots/' + encodeURIComponent(filename);
  if (img) img.src = url;
  if (cap) cap.textContent = filename;
  // store for Annotate button
  img.dataset.sid      = sid;
  img.dataset.filename = filename;
  document.getElementById('modal-ss-preview').classList.add('open');
}

function sessPreviewAnnotate() {
  const img      = document.getElementById('ss-preview-img');
  const sid      = img.dataset.sid;
  const filename = img.dataset.filename;
  if (!sid || !filename) return;
  closeModal('modal-ss-preview');
  // open annotation modal wired to this session screenshot
  _annFilename = filename;
  _annContext  = {type: 'session', sid};
  _annImgSrc   = img.src;
  _annHistory  = [];
  const modal  = document.getElementById('modal-annotate');
  modal.classList.add('open');
  const canvas  = document.getElementById('ann-canvas');
  const overlay = document.getElementById('ann-overlay');
  const annImg  = new Image();
  annImg.crossOrigin = 'anonymous';
  annImg.onload = () => {
    const maxW = window.innerWidth  * 0.9;
    const maxH = window.innerHeight * 0.82;
    let w = annImg.naturalWidth, h = annImg.naturalHeight;
    const scale = Math.min(1, maxW / w, maxH / h);
    w = Math.round(w * scale); h = Math.round(h * scale);
    canvas.width  = w; canvas.height  = h;
    overlay.width = w; overlay.height = h;
    canvas.style.width  = w + 'px'; canvas.style.height  = h + 'px';
    overlay.style.width = w + 'px'; overlay.style.height = h + 'px';
    _annCtx().drawImage(annImg, 0, 0, w, h);
  };
  annImg.src = img.src + '?nocache=' + Date.now();
}

async function ingestVideoSession(sid, videoPath) {
  const nameEl = document.getElementById('sess-name-' + sid);
  const defaultTitle = nameEl ? nameEl.textContent.trim() : 'Untitled Lab';
  const title = prompt('Lab title for this guide:', defaultTitle);
  if (!title || !title.trim()) return;
  const fiEl = document.getElementById('fi-' + sid);
  const interval = fiEl ? parseFloat(fiEl.value) || 5 : 5;
  showMainTab('ingest');
  document.getElementById('ingest-folder-title').value = title.trim();
  const card = document.getElementById('ingest-progress-card');
  const log  = document.getElementById('ingest-log');
  card.style.display = 'block';
  log.innerHTML = '<div class="progress-line">Starting video ingestion — extracting a frame every ' + interval + 's…</div>';
  startIngestJob('/api/ingest/video', {video_path: videoPath, title: title.trim(), frame_interval: interval});
}

async function renameSession(sid) {
  const cur = document.getElementById('sess-name-' + sid);
  const newName = prompt('Rename session:', cur ? cur.textContent.trim() : sid);
  if (!newName || !newName.trim()) return;
  try {
    const res = await fetch('/api/sessions/' + sid + '/rename', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name: newName.trim()})
    });
    const d = await res.json();
    if (d.error) { alert('Rename failed: ' + d.error); return; }
    if (cur) cur.textContent = newName.trim();
    if (_sessData[sid]) _sessData[sid].name = newName.trim();
    loadSessions();
  } catch(e) { alert('Rename failed: ' + e); }
}

async function deleteSession(sid) {
  if (!confirm('Delete this capture session and all its files? This cannot be undone.')) return;
  try {
    const res = await fetch('/api/sessions/' + sid, {method:'DELETE'});
    const d = await res.json();
    if (d.error) { alert('Delete failed: ' + d.error); return; }
    const card = document.getElementById('sess-card-' + sid);
    if (card) card.remove();
    delete _sessData[sid];
    loadSessions();
  } catch(e) { alert('Delete failed: ' + e); }
}

// ── AI Section (generate one section and append to open guide) ─
let _aiSecSessionPath = null;

function openAddAISection() {
  if (!currentGuideId) return;
  _aiSecSessionPath = null;
  document.getElementById('ai-sec-title').value = '';
  document.getElementById('ai-sec-selected-label').textContent = '';
  document.getElementById('ai-sec-progress-log').style.display = 'none';
  document.getElementById('ai-sec-progress-log').innerHTML = '';
  document.getElementById('ai-sec-submit-btn').disabled = false;
  document.getElementById('modal-add-ai-section').classList.add('open');
  loadAISectionSessions();
}

async function loadAISectionSessions() {
  const list = document.getElementById('ai-sec-session-list');
  list.innerHTML = '<div style="color:var(--text2);font-size:.8rem">Loading…</div>';
  try {
    const res = await fetch('/api/sessions');
    const sessions = await res.json();
    // AI section ingest needs screenshots — filter out video-only sessions
    const ssOnly = sessions.filter(s => s.screenshot_count > 0 && !s.has_video);
    if (!ssOnly.length) {
      list.innerHTML = '<div style="color:var(--text2);font-size:.8rem">No screenshot sessions yet. Use 📸 Screenshots mode in the capture panel.</div>';
      return;
    }
    list.innerHTML = ssOnly.map(s =>
      '<div class="session-row" id="aisr-' + s.session_id + '" onclick="selectAISecSession(' +
        JSON.stringify(s.session_id) + ',' + JSON.stringify(s.folder_path) + ')">' +
        '<span class="ss-id">' + s.name + '</span>' +
        '<span class="ss-count">📸 ' + s.screenshot_count + ' screenshot' + (s.screenshot_count !== 1 ? 's' : '') + ' · ' + s.size_mb + ' MB</span>' +
        '<span class="ss-date">' + s.recorded_at + '</span>' +
      '</div>'
    ).join('');
  } catch(e) {
    list.innerHTML = '<div style="color:var(--red);font-size:.8rem">Failed to load sessions</div>';
  }
}

function selectAISecSession(sid, folderPath) {
  _aiSecSessionPath = folderPath;
  document.querySelectorAll('#ai-sec-session-list .session-row').forEach(r => r.classList.remove('active'));
  const row = document.getElementById('aisr-' + sid);
  if (row) row.classList.add('active');
  document.getElementById('ai-sec-selected-label').textContent = '✓ ' + sid + ' selected';
}

async function submitAddAISection() {
  const title = document.getElementById('ai-sec-title').value.trim();
  if (!title) { document.getElementById('ai-sec-title').focus(); return; }
  if (!_aiSecSessionPath) { alert('Please select a capture session first.'); return; }

  const log = document.getElementById('ai-sec-progress-log');
  log.style.display = 'block';
  log.innerHTML = '<div class="progress-line">Starting…</div>';
  document.getElementById('ai-sec-submit-btn').disabled = true;

  const res = await fetch('/api/guides/' + currentGuideId + '/ingest-section', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({folder_path: _aiSecSessionPath, section_title: title}),
  });
  const {job_id, error} = await res.json();
  if (error) {
    log.innerHTML += '<div class="progress-line error">✗ ' + error + '</div>';
    document.getElementById('ai-sec-submit-btn').disabled = false;
    return;
  }

  const es = new EventSource('/api/ingest/progress/' + job_id);
  es.onmessage = async (e) => {
    const evt = JSON.parse(e.data);
    if (evt.type === 'ping') return;
    if (evt.type === 'progress') {
      log.innerHTML += '<div class="progress-line">' + evt.message + '</div>';
      log.scrollTop = log.scrollHeight;
    } else if (evt.type === 'done') {
      log.innerHTML += '<div class="progress-line done">✓ Section "' + evt.section_title + '" added (' + evt.steps + ' step' + (evt.steps !== 1 ? 's' : '') + ')</div>';
      log.scrollTop = log.scrollHeight;
      es.close();
      await loadGuide(currentGuideId);
      closeModal('modal-add-ai-section');
    } else if (evt.type === 'error') {
      log.innerHTML += '<div class="progress-line error">✗ ' + evt.message + '</div>';
      document.getElementById('ai-sec-submit-btn').disabled = false;
      es.close();
    } else if (evt.type === 'end') {
      es.close();
    }
  };
}

// ── Modals ────────────────────────────────────────────────────
function closeModal(id) {
  document.getElementById(id).classList.remove('open');
}
document.addEventListener('click', e => {
  if (e.target.classList.contains('modal-overlay')) closeModal(e.target.id);
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') document.querySelectorAll('.modal-overlay.open').forEach(m => m.classList.remove('open'));
});

// ── Boot ──────────────────────────────────────────────────────
loadLibrary();
loadModels();
</script>
</body>
</html>"""


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────

def _start_hotkey_listener() -> None:
    """Start a background thread that listens for Cmd+Shift+S (macOS) globally.

    When triggered it fires a screenshot against the most recent active
    recording session — identical to calling POST /api/record/screenshot
    without a session_id.  A macOS notification confirms each capture.
    Falls back silently if pynput is not installed.
    """
    try:
        from pynput import keyboard as _kb
    except ImportError:
        print("[hotkey] pynput not installed — global hotkey disabled. "
              "Run: uv add pynput")
        return

    _combo = {_kb.Key.cmd, _kb.Key.shift}
    _pressed: set = set()

    def _on_press(key):
        _pressed.add(key)
        # Check for Cmd+Shift+S
        s_key = (key == _kb.KeyCode.from_char("s") or
                 getattr(key, "char", None) == "s")
        if s_key and _combo.issubset(_pressed):
            _fire_hotkey_screenshot()

    def _on_release(key):
        _pressed.discard(key)

    def _fire_hotkey_screenshot():
        running = [s for s in _active_sessions.values() if s.is_running()]
        if not running:
            return
        session = running[-1]
        try:
            wid = _session_window.get(session.session_id)
            path, seq = take_screenshot(session, "hotkey", window_id=wid)
            import shutil as _sh
            _sh.copy2(path, _ss_dir() / path.name)
            # macOS notification
            subprocess.Popen([
                "osascript", "-e",
                f'display notification "Screenshot {seq} saved" '
                f'with title "Lab Guide Automator" sound name "Tink"'
            ])
        except Exception as _e:
            print(f"[hotkey] screenshot failed: {_e}")

    import threading
    t = threading.Thread(
        target=lambda: _kb.Listener(
            on_press=_on_press, on_release=_on_release).run(),
        daemon=True, name="hotkey-listener"
    )
    t.start()
    print("[hotkey] Global hotkey Cmd+Shift+S active")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=5051)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    _start_hotkey_listener()
    print(f"Lab Guide Automator dashboard → http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
