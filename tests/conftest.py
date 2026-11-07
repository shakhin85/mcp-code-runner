import pytest


@pytest.fixture(autouse=True)
def _isolated_shapes(tmp_path, monkeypatch):
    path = tmp_path.parent / "shapes" / tmp_path.name / "shapes.json"
    monkeypatch.setenv("CODE_RUNNER_SHAPES_PATH", str(path))
