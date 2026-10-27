from export.exporter import _step_heading, render_markdown
from lab_guide_automator.models import LabGuide, LabMetadata, LabSection, LabStep


def _step(order, title):
    return LabStep(order=order, title=title, instruction="")


def test_regular_section_steps_are_numbered():
    sec = LabSection(title="Section 3: Identity", steps=[_step(1, "Explore Users")])
    assert _step_heading(sec, sec.steps[0]) == "Step 1: Explore Users"


def test_appendix_steps_are_unnumbered():
    sec = LabSection(title="Appendix B: Scenarios", steps=[_step(2, "Scenario 2 — Mexico City")])
    assert _step_heading(sec, sec.steps[0]) == "Scenario 2 — Mexico City"


def test_markdown_export_numbers_per_section_and_skips_appendix():
    guide = LabGuide(
        metadata=LabMetadata(title="Lab"),
        sections=[
            LabSection(title="Section 1: Start", steps=[_step(1, "A"), _step(2, "B")]),
            LabSection(title="Section 2: Next", steps=[_step(1, "C")]),
            LabSection(title="Appendix A: Prompts", steps=[_step(1, "Sample Prompts")]),
        ],
    )
    md = render_markdown(guide, include_screenshots=False)
    assert "### Step 1: A" in md and "### Step 2: B" in md
    assert "### Step 1: C" in md
    assert "### Sample Prompts" in md and "Step 1: Sample Prompts" not in md


def test_objectives_render_on_introduction_not_home(tmp_path):
    from export.exporter import export_mkdocs
    from lab_guide_automator.models import LearningObjective
    guide = LabGuide(
        metadata=LabMetadata(title="Lab"),
        introduction="Welcome",
        learning_objectives=[LearningObjective(text="Build an agent")],
    )
    export_mkdocs(guide, tmp_path)
    assert "Build an agent" not in (tmp_path / "docs" / "index.md").read_text()
    assert "Build an agent" in (tmp_path / "docs" / "introduction" / "index.md").read_text()


def test_bold_with_trailing_space_still_renders_bold():
    from export.exporter import _html_to_md
    md = _html_to_md("<p>Search for <strong>Alex&nbsp;Garcia.&nbsp;</strong>Once found</p>")
    assert "**Alex Garcia.**" in md or "**Alex Garcia.**" in md
    assert ". **" not in md
