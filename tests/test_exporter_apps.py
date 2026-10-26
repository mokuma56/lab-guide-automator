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
