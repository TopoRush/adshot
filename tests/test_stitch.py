import os
import shutil
import struct
import subprocess
from pathlib import Path

import pytest

from adshot.stitch import stitch


def test_missing_ffmpeg_explains_dependency(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="(?i)ffmpeg.*PATH"):
        stitch(tmp_path, ["S01"], Path("ad.mp4"))


def test_stitch_cli_reports_missing_ffmpeg_from_project(tmp_path, monkeypatch, capsys):
    from adshot.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert main(["init", "."]) == 0
    assert main(["stitch"]) == 1
    assert "FFmpeg" in capsys.readouterr().err


def test_failed_encode_preserves_previous_output(tmp_path, monkeypatch):
    shots = tmp_path / "shots"
    shots.mkdir()
    (shots / "S01.mp4").write_bytes(b"input")
    out = tmp_path / "ad.mp4"
    out.write_bytes(b"previous ad")
    monkeypatch.setattr(shutil, "which", lambda name: "ffmpeg")

    def fake_ffmpeg(args, **kwargs):
        if "framehash" in args:
            return subprocess.CompletedProcess(args, 0, "#dimensions 0: 120x80\n0, 0, 0, 1, 8, abc\n", "")
        Path(args[-1]).write_bytes(b"incomplete")
        return subprocess.CompletedProcess(args, 1, "", "mock encoder failure")

    monkeypatch.setattr(subprocess, "run", fake_ffmpeg)
    with pytest.raises(ValueError, match="mock encoder failure"):
        stitch(tmp_path, ["S01"], out)
    assert out.read_bytes() == b"previous ad"
    assert {p.name for p in tmp_path.iterdir()} == {"ad.mp4", "shots"}


def test_output_cannot_replace_shot(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "ffmpeg")
    (tmp_path / "shots").mkdir()
    source = tmp_path / "shots/S01.mp4"
    source.write_bytes(b"clip")
    with pytest.raises(ValueError, match="shots"):
        stitch(tmp_path, ["S01"], source)
    assert source.read_bytes() == b"clip"


def test_real_concat_preserves_order_geometry_and_mixed_audio(tmp_path, monkeypatch):
    executable = os.environ.get("ADSHOT_TEST_FFMPEG") or shutil.which("ffmpeg")
    if not executable:
        pytest.skip("Install FFmpeg to run the local media integration test")
    monkeypatch.setattr(shutil, "which", lambda name: executable)

    def ffmpeg(*args):
        return subprocess.check_output(
            [executable, "-nostdin", "-hide_banner", "-loglevel", "error", *args]
        )

    shots = tmp_path / "shots"
    shots.mkdir()
    ffmpeg("-f", "lavfi", "-i", "color=red:s=96x48:r=24:d=0.5", shots / "S02.mp4")
    ffmpeg("-f", "lavfi", "-i", "color=blue:s=48x96:r=30:d=0.5",
           "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5", shots / "S01.mp4")
    stitch(tmp_path, ["S02", "S01"], Path("ad.mp4"))
    out = tmp_path / "ad.mp4"

    def frame(timestamp):
        return ffmpeg("-ss", timestamp, "-i", out, "-map", "0:v:0", "-frames:v", "1",
                      "-pix_fmt", "rgb24", "-f", "rawvideo", "-")

    first, second = frame("0.2"), frame("0.8")
    assert len(first) == len(second) == 96 * 48 * 3
    center = (24 * 96 + 48) * 3
    assert first[center] > 220 and second[center + 2] > 220
    assert max(second[0:3]) < 16  # The portrait shot is centered with pillarboxing.

    audio = ffmpeg("-i", out, "-map", "0:a:0", "-ac", "1", "-ar", "8000", "-f", "s16le", "-")
    samples = struct.unpack(f"<{len(audio) // 2}h", audio)
    assert 7900 <= len(samples) <= 8300
    assert max(map(abs, samples[800:3200])) < 50
    assert max(map(abs, samples[5200:7200])) > 500
