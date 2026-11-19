"""Project settings and the offline starter storyboard."""

import math
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from urllib.parse import urlsplit

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib


OPENLUX = "https://api.openlux.ai"
ARK = "https://ark.cn-beijing.volces.com"
MINI = "doubao-seedance-2-0-mini-260615"
FAST = "doubao-seedance-2-0-fast-260128"
STD = "doubao-seedance-2-0-260128"
PRO = "doubao-seedance-2-5-260628"

TEMPLATE = f'''# Video generation. An empty base_url selects the provider's default endpoint.
provider = "openlux"
base_url = ""
model = "{FAST}"
resolution = "720p"
ratio = "9:16"
budget_usd = 5.0
concurrency = 2

# Optional vision planner. Defaults to the video origin + /api/v3 for Ark or /v1 for OpenLux.
plan_model = "gpt-4o-mini"
plan_base_url = ""

# USD per million video tokens. Update these values using your provider's rates.
# Set verified = true and base_url to the endpoint whose price you confirmed.
[prices."{MINI}"]
usd_per_million = 2.41
verified = true
base_url = "{OPENLUX}"  # Measured on OpenLux on 2026-11-19.

[prices."{FAST}"]
usd_per_million = 3.61
verified = true
base_url = "{OPENLUX}"  # Measured on OpenLux on 2026-11-19, 720p portrait with a first frame.

# Standard and Pro prices are unverified budget placeholders.
[prices."{STD}"]
usd_per_million = 8.0
verified = false

[prices."{PRO}"]
usd_per_million = 12.0
verified = false
'''

PROJECT_IGNORE = """# Local settings and generated media
.env*
.adshot/
manifest.json
shots/
ad.mp4
"""


@dataclass(frozen=True)
class Price:
    usd_per_million: float
    verified: bool


@dataclass(frozen=True)
class Config:
    provider: str
    base_url: str
    model: str
    resolution: str
    ratio: str
    budget_usd: float
    concurrency: int
    plan_model: str
    plan_base_url: str
    prices: dict[str, Price]


def init_project(root: Path) -> None:
    """Create a standalone project without overwriting existing files."""
    root.mkdir(parents=True, exist_ok=True)
    starter = files("adshot").joinpath("perfume.csv").read_bytes()
    content = {
        "adshot.toml": TEMPLATE.encode("utf-8"),
        "storyboard.csv": starter,
        ".gitignore": PROJECT_IGNORE.encode("utf-8"),
    }
    for name in content:
        if (root / name).exists():
            raise ValueError(f"{name} already exists in {root}. Choose a new project directory.")
    created = []
    try:
        for name, data in content.items():
            path = root / name
            with path.open("xb") as stream:
                created.append(path)
                stream.write(data)
    except OSError:
        for path in created:
            path.unlink(missing_ok=True)
        raise


def validate_base_url(value: str, field: str = "base_url") -> str:
    parts = urlsplit(value)
    local_http = parts.scheme == "http" and parts.hostname in {"localhost", "127.0.0.1"}
    if ((parts.scheme != "https" and not local_http) or not parts.hostname or
            parts.username is not None or parts.password is not None or
            parts.query or parts.fragment or any(c.isspace() for c in value)):
        raise ValueError(f"{field} requires HTTPS without credentials, query or fragment. "
                         "HTTP is allowed only for localhost and 127.0.0.1.")
    return value.rstrip("/")


def _number(value, field: str) -> float:
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        raise ValueError(f"{field} must be a finite, nonnegative number.")
    return float(value)


def _text(doc: dict, field: str, default: str) -> str:
    value = doc.get(field, default)
    if not isinstance(value, str) or (not value.strip() and default):
        raise ValueError(f"{field} must be a string{' containing a value' if default else ''}.")
    return value.strip()


def load_config(root: Path) -> Config:
    path = root / "adshot.toml"
    if not path.is_file():
        raise ValueError(f"No adshot.toml in {root}. Run adshot init or change into a project.")
    with path.open("rb") as stream:
        doc = tomllib.load(stream)
    provider = _text(doc, "provider", "openlux")
    endpoints = {"openlux": OPENLUX, "ark": ARK}
    custom = _text(doc, "base_url", "")
    if not custom and provider not in endpoints:
        raise ValueError("Unknown provider. Choose openlux or ark, or set base_url.")
    base_url = validate_base_url(custom or endpoints[provider])
    plan_path = "/api/v3" if provider == "ark" else "/v1"
    plan_base = _text(doc, "plan_base_url", "") or base_url + plan_path
    concurrency = doc.get("concurrency", 2)
    if type(concurrency) is not int or concurrency < 1:
        raise ValueError("concurrency must be a positive integer.")
    table = doc.get("prices")
    if not isinstance(table, dict) or not table:
        raise ValueError("adshot.toml needs a prices table with a rate for each model.")
    prices = {}
    for model, entry in table.items():
        if not isinstance(entry, dict):
            raise ValueError(f"prices.{model} must be a table.")
        rate = _number(entry.get("usd_per_million"), f"prices.{model}.usd_per_million")
        verified = entry.get("verified", False)
        if type(verified) is not bool:
            raise ValueError(f"prices.{model}.verified must be true or false.")
        scope = _text(entry, "base_url", "")
        if scope and validate_base_url(scope, "price base_url") != base_url:
            verified = False
        prices[model] = Price(rate, verified)
    return Config(
        provider=provider,
        base_url=base_url,
        model=_text(doc, "model", FAST),
        resolution=_text(doc, "resolution", "720p"),
        ratio=_text(doc, "ratio", "9:16"),
        budget_usd=_number(doc.get("budget_usd", 5.0), "budget_usd"),
        concurrency=concurrency,
        plan_model=_text(doc, "plan_model", "gpt-4o-mini"),
        plan_base_url=validate_base_url(plan_base, "plan_base_url"),
        prices=prices,
    )
