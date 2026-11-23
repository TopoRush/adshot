"""The MCP tools drive the same CLI, offline, without a key."""

import os
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from adshot import mcp_server as tools


@pytest.fixture
def project(tmp_path, monkeypatch):
    source = str(Path(__file__).resolve().parents[1] / "src")
    monkeypatch.setenv("PYTHONPATH", source + os.pathsep + os.environ.get("PYTHONPATH", ""))
    monkeypatch.delenv("ADSHOT_API_KEY", raising=False)
    root = tmp_path / "demo"
    tools.init_project(str(root))
    return str(root)


SHOTS = [
    {"shot_id": "S01", "prompt": "Bottle on wet stone, slow push-in", "seconds": 4},
    {"shot_id": "S02", "prompt": "Cap twists open, hold, soft light", "seconds": 5,
     "notes": "comma, in notes"},
]


def test_storyboard_round_trip_and_quote(project):
    saved = tools.set_storyboard(project, SHOTS)
    assert "Saved 2 shots" in saved and "Total new spend" in saved
    rows = tools.get_storyboard(project)
    assert [row["shot_id"] for row in rows] == ["S01", "S02"]
    assert rows[1]["notes"] == "comma, in notes"
    partial = tools.estimate(project, only="S02")
    assert "S02" in partial and "S01" not in partial
    state = tools.status(project)
    assert "pending" in state and "no render started" in state


def test_invalid_storyboard_restores_previous(project):
    tools.set_storyboard(project, SHOTS)
    with pytest.raises(ValueError, match="4-15 seconds"):
        tools.set_storyboard(project, [{"shot_id": "S01", "prompt": "x", "seconds": 99}])
    with pytest.raises(ValueError, match="unknown keys"):
        tools.set_storyboard(project, [{"shot_id": "S01", "prompt": "x", "seconds": 4, "fps": 24}])
    assert tools.get_storyboard(project)[1]["prompt"].startswith("Cap twists open")


def test_render_and_paths_are_guarded(project):
    with pytest.raises(ValueError, match="ADSHOT_API_KEY"):
        tools.render(project, approved_usd=5)
    for out in ("../escape.mp4", "C:escape.mp4", "sub/ad.mp4"):
        with pytest.raises(ValueError, match="inside the project"):
            tools.stitch(project, out=out)
    with pytest.raises(ValueError, match="absolute path"):
        tools.estimate("relative/path")
    with pytest.raises(ValueError, match="init_project first"):
        tools.estimate(str(Path(project).parent / "missing"))


def test_render_refuses_estimate_above_approval(project, monkeypatch):
    monkeypatch.setenv("ADSHOT_API_KEY", "test-key-not-real")
    with pytest.raises(ValueError, match="exceeds budget"):
        tools.render(project, approved_usd=0.01)
    assert "pending" in tools.status(project)


def test_render_cannot_exceed_the_configured_cap(project, monkeypatch):
    """budget_usd in adshot.toml is the user's cap; the agent cannot raise it."""
    monkeypatch.setenv("ADSHOT_API_KEY", "test-key-not-real")
    monkeypatch.setattr(tools, "_spawn", lambda root, args: pytest.fail("must not start"))
    with pytest.raises(ValueError, match="above budget_usd 5.0"):
        tools.render(project, approved_usd=30)
    with pytest.raises(ValueError, match="above budget_usd"):
        tools.render(project, approved_usd=float("nan"))


def test_render_accepts_the_displayed_total(project, monkeypatch):
    """The four-decimal total shown by estimate must pass as approved_usd."""
    monkeypatch.setenv("ADSHOT_API_KEY", "test-key-not-real")
    shot = {"shot_id": "S01", "prompt": "Bottle on wet stone", "seconds": 4,
            "model": "doubao-seedance-2-0-mini-260615", "resolution": "480p"}
    shown = tools.set_storyboard(project, [shot]).rsplit("$", 1)[1].strip()
    started = []

    class Running:
        def poll(self):
            return None

    monkeypatch.setattr(tools, "_spawn", lambda root, args: started.append(args) or Running())
    monkeypatch.setattr(tools.time, "sleep", lambda seconds: None)
    assert "Render started" in tools.render(project, approved_usd=float(shown))
    budget = float(started[0][started[0].index("--budget") + 1])
    assert float(shown) < budget < float(shown) + 0.0001
    with pytest.raises(ValueError, match="already in progress"):
        tools.render(project, approved_usd=float(shown))
