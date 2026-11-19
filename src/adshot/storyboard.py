"""Read storyboard rows and fingerprint the inputs that affect each video."""

import base64
import csv
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from .config import FAST, MINI, PRO, Config


FIELDS = ("shot_id", "prompt", "seconds", "image", "model", "resolution", "ratio", "notes")
IMAGE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif",
}
ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}")
DEVICE_PATTERN = re.compile(r"(?:con|prn|aux|nul|com[0-9]|lpt[0-9])", re.IGNORECASE)


@dataclass(frozen=True)
class Shot:
    shot_id: str
    prompt: str
    seconds: int
    image: str
    model: str
    resolution: str
    ratio: str
    notes: str
    image_key: str
    signature: str


# Seconds per shot accepted by each model (Volcano Ark model docs).
DURATION_LIMITS = {
    "doubao-seedance-2-0-mini-260615": (4, 15),
    "doubao-seedance-2-0-fast-260128": (4, 15),
    "doubao-seedance-2-0-260128": (4, 15),
    "doubao-seedance-2-5-260628": (4, 15),
}


def _image_file(root: Path, value: str) -> Path | None:
    """Return a local path, or None for an empty or remote reference."""
    if not value:
        return None
    if "://" in value:
        url = urlsplit(value)
        if (url.scheme != "https" or not url.hostname or url.username is not None or
                url.password is not None or any(c.isspace() for c in value)):
            raise ValueError("Remote first frames require an HTTPS image URL.")
        return None
    path = root / value
    if path.suffix.lower() not in IMAGE_TYPES:
        raise ValueError("Choose a PNG, JPEG, WebP or GIF file for the first frame.")
    if not path.is_file():
        raise ValueError(f"Image file not found: {value}")
    return path


def _image_key(root: Path, value: str) -> str:
    path = _image_file(root, value)
    if path is None:
        return value
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def image_reference(root: Path, value: str) -> tuple[str, str | None]:
    """Hash and encode the same bytes, so a file edit cannot split the two."""
    path = _image_file(root, value)
    if path is None:
        return value, value or None
    data = path.read_bytes()
    encoded = base64.b64encode(data).decode("ascii")
    return hashlib.sha256(data).hexdigest(), f"data:{IMAGE_TYPES[path.suffix.lower()]};base64,{encoded}"


def _read_row(root: Path, config: Config, row: dict) -> Shot:
    if None in row or None in row.values():
        raise ValueError("Column count does not match the header. Quote prompts containing commas.")
    row = {name: value.strip() for name, value in row.items()}
    shot_id = row["shot_id"]
    if not ID_PATTERN.fullmatch(shot_id) or DEVICE_PATTERN.fullmatch(shot_id):
        raise ValueError("Invalid shot_id. Use a filename-safe identifier such as S01.")
    prompt = row["prompt"]
    if not prompt:
        raise ValueError("A prompt is required.")
    if not row["seconds"].isascii() or not row["seconds"].isdecimal():
        raise ValueError("seconds must be a positive integer.")
    seconds = int(row["seconds"])
    if seconds < 1:
        raise ValueError("seconds must be a positive integer.")
    image = row.get("image", "")
    model = row.get("model") or config.model
    resolution = row.get("resolution") or config.resolution
    ratio = row.get("ratio") or config.ratio
    if model not in config.prices:
        raise ValueError(f"Set a rate for {model} in the adshot.toml prices table.")
    if resolution not in {"480p", "720p", "1080p"}:
        raise ValueError("resolution must be 480p, 720p or 1080p.")
    if ratio not in {"9:16", "16:9", "1:1", "adaptive"}:
        raise ValueError("ratio must be 9:16, 16:9, 1:1 or adaptive.")
    limits = DURATION_LIMITS.get(model)
    if limits and not limits[0] <= seconds <= limits[1]:
        raise ValueError(
            f"{model} accepts {limits[0]}-{limits[1]} seconds per shot; got {seconds}. "
            "Some gateways report an out-of-range duration as a temporary outage, so it is checked here.")
    if resolution == "1080p" and model in {MINI, FAST}:
        raise ValueError(f"{model} supports resolutions up to 720p.")
    if image and model == PRO and ratio != "adaptive":
        raise ValueError("Set ratio to adaptive when using a first frame with Seedance 2.5.")
    image_key = _image_key(root, image)
    inputs = (prompt, seconds, image_key, model, resolution, ratio)
    signature = hashlib.sha256(json.dumps(inputs, ensure_ascii=False).encode("utf-8")).hexdigest()
    return Shot(shot_id, prompt, seconds, image, model, resolution, ratio, row.get("notes", ""),
                image_key, signature)


def read_storyboard(root: Path, config: Config) -> list[Shot]:
    path = root / "storyboard.csv"
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, strict=True)
        columns = reader.fieldnames or []
        if (len(set(columns)) != len(columns) or set(columns).difference(FIELDS) or
                not {"shot_id", "prompt", "seconds"}.issubset(columns)):
            raise ValueError(f"Invalid storyboard.csv header. Expected columns: {', '.join(FIELDS)}.")
        shots = []
        seen = set()
        try:
            for row in reader:
                shot = _read_row(root, config, row)
                key = shot.shot_id.casefold()
                if key in seen:
                    raise ValueError(f"Duplicate shot_id: {shot.shot_id}")
                seen.add(key)
                shots.append(shot)
        except (ValueError, OSError, csv.Error) as exc:
            raise ValueError(f"storyboard.csv line {reader.line_num}: {exc}") from exc
    if not shots:
        raise ValueError("storyboard.csv contains no shots.")
    return shots
