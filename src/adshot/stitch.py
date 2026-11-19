"""Assemble storyboard shots on a common canvas using the FFmpeg executable."""

import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Clip:
    path: Path
    width: float  # Display width in square pixels.
    height: int
    audio: bool
    frames: int


def _ffmpeg(executable: str, *args: str | Path) -> str:
    result = subprocess.run(
        [executable, "-nostdin", "-hide_banner", "-loglevel", "error", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode:
        raise ValueError(f"FFmpeg failed: {result.stderr.strip() or 'unknown encoder error'}")
    return result.stdout


def _inspect(executable: str, path: Path) -> Clip:
    # Count frames at the output rate to trim audio to the actual picture length.
    # wrapped_avframe hashes a small descriptor rather than each decoded image.
    header = _ffmpeg(
        executable, "-i", path, "-map", "0:v:0", "-map", "0:a:0?", "-vf", "fps=24",
        "-c:v", "wrapped_avframe", "-c:a", "copy", "-f", "framehash", "-hash", "adler32", "-",
    )
    size = re.search(r"^#dimensions 0:\s*(\d+)x(\d+)", header, re.MULTILINE)
    if size is None:
        raise ValueError(f"FFmpeg could not read the video dimensions of {path.name}.")
    width, height = map(int, size.groups())
    sar = re.search(r"^#sar 0:\s*(\d+)/(\d+)", header, re.MULTILINE)
    if sar and all(int(n) for n in sar.groups()):
        width *= int(sar[1]) / int(sar[2])
    audio = bool(re.search(r"^#media_type 1:\s*audio", header, re.MULTILINE))
    frames = sum(line.startswith("0,") for line in header.splitlines())
    if not frames:
        raise ValueError(f"FFmpeg found no video frames in {path.name}.")
    return Clip(path, width, height, audio, frames)


def _even(value: float) -> int:
    return max(2, 2 * round(value / 2))


def stitch(root: Path, shot_ids: list[str], out: Path) -> None:
    """Re-encode clips in the requested order, publishing the MP4 atomically."""
    executable = shutil.which("ffmpeg")
    if not executable:
        raise ValueError("Install FFmpeg and add its ffmpeg executable to PATH to stitch an ad.")
    if not shot_ids:
        raise ValueError("The storyboard has no shots to stitch.")
    shots_dir = (root / "shots").resolve()
    output = (root / out).resolve()
    if shots_dir in output.parents or output.suffix.lower() != ".mp4":
        raise ValueError("Choose an MP4 output file outside the shots directory.")

    clips = []
    for shot_id in shot_ids:
        path = (shots_dir / f"{shot_id}.mp4").resolve()
        if path.parent != shots_dir or not path.is_file():
            raise ValueError(f"Missing shot {shot_id}. Run adshot run to download it.")
        clips.append(_inspect(executable, path))

    width, height = _even(clips[0].width), _even(clips[0].height)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent, prefix=".adshot-stitch-") as directory:
        work = Path(directory)
        segments = []
        for i, clip in enumerate(clips):
            segment = work / f"clip-{i}.mkv"
            scale = min(width / clip.width, height / clip.height)
            filters = (
                f"scale={_even(clip.width * scale)}:{_even(clip.height * scale)},setsar=1,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,fps=24,format=yuv420p"
            )
            inputs = ["-i", clip.path]
            if not clip.audio:
                inputs.extend(["-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo"])
            _ffmpeg(
                executable, *inputs, "-map", "0:v:0", "-map", "0:a:0" if clip.audio else "1:a:0",
                "-vf", filters, "-c:v", "libx264", "-crf", "18",
                "-af", f"aresample=48000,apad,atrim=end_sample={clip.frames * 2000}",
                "-ac", "2", "-ar", "48000", "-c:a", "pcm_s16le", segment,
            )
            segments.append(f"file '{segment.name}'\n")

        playlist = work / "concat.txt"
        playlist.write_text("".join(segments), encoding="ascii")
        staged = work / "ad.mp4"
        # PCM intermediates avoid adding a separate AAC priming delay at each cut.
        _ffmpeg(
            executable, "-f", "concat", "-safe", "1", "-i", playlist, "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", staged,
        )
        staged.replace(output)
