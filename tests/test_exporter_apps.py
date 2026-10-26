from export.exporter import _copy_guide_apps
from lab_guide_automator.models import LabGuide, LabMetadata


def test_guide_apps_are_copied_into_docs(tmp_path):
    guide = LabGuide(metadata=LabMetadata(title="Lab"))
    src = tmp_path / "apps" / guide.id
    src.mkdir(parents=True)
    (src / "prompt-library.html").write_text("<html>app</html>")
    docs = tmp_path / "docs"
    docs.mkdir()
    _copy_guide_apps(guide, docs, apps_root=tmp_path / "apps")
    assert (docs / "apps" / "prompt-library.html").read_text() == "<html>app</html>"


def test_guide_without_apps_creates_nothing(tmp_path):
    guide = LabGuide(metadata=LabMetadata(title="Lab"))
    docs = tmp_path / "docs"
    docs.mkdir()
    _copy_guide_apps(guide, docs, apps_root=tmp_path / "apps")
    assert not (docs / "apps").exists()


def test_nav_divider_css_follows_nav_order():
    from export.exporter import _nav_divider_css
    from lab_guide_automator.models import LabSection
    a, b = LabSection(title="A"), LabSection(title="B")
    guide = LabGuide(metadata=LabMetadata(title="Lab"), introduction="Hi", conclusion="Bye",
                     sections=[a, b], nav_dividers_after=["home", b.id, "missing"])
    css = _nav_divider_css(guide)
    assert "nth-child(1)" in css and "nth-child(4)" in css and "nth-child(5)" not in css
    assert _nav_divider_css(LabGuide(metadata=LabMetadata(title="Lab"))) == ""
