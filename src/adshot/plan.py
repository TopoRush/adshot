"""Draft a storyboard with a vision-capable chat completions endpoint."""

import csv
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from .config import Config, PRO
from .providers import api_key, json_request
from .storyboard import FIELDS, image_reference


PLANNER_SECONDS = range(4, 7)


def _validated_shots(response: dict, count: int, seconds: int) -> list[dict]:
    try:
        content = response["choices"][0]["message"]["content"].strip()
        if content.startswith("```") and content.endswith("```"):
            content = "\n".join(content.splitlines()[1:-1])
        rows = json.loads(content)["shots"]
    except (KeyError, IndexError, TypeError, AttributeError, ValueError):
        raise ValueError("Planner returned an invalid storyboard JSON response.") from None
    if not isinstance(rows, list) or len(rows) != count:
        raise ValueError(f"Planner must return exactly {count} shots.")
    clean = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"Planner shot {index} must be an object.")
        prompt = row.get("prompt")
        duration = row.get("seconds")
        notes = row.get("notes", "")
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError(f"Planner shot {index} needs a prompt.")
        if type(duration) is not int or duration not in PLANNER_SECONDS:
            raise ValueError(f"Planner shot {index} duration must be {PLANNER_SECONDS[0]} "
                             f"to {PLANNER_SECONDS[-1]} whole seconds.")
        if not isinstance(notes, str):
            raise ValueError(f"Planner shot {index} notes must be text.")
        clean.append({"shot_id": f"S{index:02d}", "prompt": prompt.strip(),
                      "seconds": duration, "notes": notes.strip()})
    if sum(shot["seconds"] for shot in clean) != seconds:
        raise ValueError(f"Planner durations must add up to {seconds} seconds.")
    return clean


def plan(root: Path, config: Config, image: str, brief: str, shots: int, seconds: int) -> None:
    low, high = PLANNER_SECONDS[0], PLANNER_SECONDS[-1]
    if shots < 1 or not low * shots <= seconds <= high * shots:
        raise ValueError(f"Use at least one shot and allow {low} to {high} seconds per shot.")
    if not image.strip() or not brief.strip():
        raise ValueError("Planning requires both an image and a brief.")
    if not config.plan_model:
        raise ValueError("Set plan_model to a vision-capable model in adshot.toml.")
    key = api_key("ADSHOT_PLAN_KEY", "ADSHOT_API_KEY")
    _, image_url = image_reference(root, image)
    response = json_request(config.plan_base_url.rstrip("/") + "/chat/completions", key, {
        "model": config.plan_model,
        "messages": [
            {"role": "system", "content": (
                "Plan a product advertisement using the supplied product reference. Use English "
                "prompts. Keep product proportions, packaging and label placement consistent. "
                "Describe one visible action and camera move per shot. Avoid instructions for "
                "on-screen text. Return only JSON with a shots array of objects, each containing "
                f"prompt (string), seconds (integer, {low} through {high}), and optional notes (string)."
            )},
            {"role": "user", "content": [
                {"type": "text", "text": (
                    f"Draft exactly {shots} shots totaling exactly {seconds} seconds.\nBrief: {brief}"
                )},
                {"type": "image_url", "image_url": {"url": image_url}},
            ]},
        ],
    })
    rows = _validated_shots(response, shots, seconds)
    for row in rows:
        row["image"] = image
        if config.model == PRO:
            row["ratio"] = "adaptive"

    temp_path = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=root,
                                prefix=".storyboard-", suffix=".csv", delete=False) as stream:
            temp_path = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=FIELDS)
            writer.writeheader()
            writer.writerows(rows)
            stream.flush()
            os.fsync(stream.fileno())
        temp_path.replace(root / "storyboard.csv")
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
