import csv
import hashlib
from pathlib import Path

import pytest

from adshot.config import FAST, STD, init_project, load_config
from adshot.storyboard import FIELDS, image_reference, read_storyboard


MINI = "doubao-seedance-2-0-mini-260615"
PRO = "doubao-seedance-2-5-260628"


def write_rows(root, *rows):
    with (root / "storyboard.csv").open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows({"shot_id": "S01", "prompt": "A bottle turns.", "seconds": 4, **r}
                         for r in rows)


def update_config(root, current, replacement):
    path = root / "adshot.toml"
    data = path.read_text(encoding="utf-8")
    assert current in data
    path.write_text(data.replace(current, replacement), encoding="utf-8")


def test_example_is_installed_template(tmp_path):
    root = tmp_path / "nested" / "demo"
    init_project(root)
    config = load_config(root)
    sample = Path(__file__).resolve().parents[1] / "examples/perfume/storyboard.csv"
    assert (root / "storyboard.csv").read_text() == sample.read_text()
    rows = read_storyboard(root, config)
    assert [s.shot_id for s in rows] == [f"S{i:02d}" for i in range(1, 5)]
    assert sum(s.seconds for s in rows) == 16
    assert all(4 <= s.seconds <= 5 for s in rows)
    assert {s.model for s in rows} == {"doubao-seedance-2-0-fast-260128"}
    assert all(s.image == "" for s in rows)
    assert "shots/" in (root / ".gitignore").read_text()


def test_init_refuses_overwrite_without_partial_changes(tmp_path):
    (tmp_path / "storyboard.csv").write_text("my storyboard")
    with pytest.raises(ValueError, match="already exists"):
        init_project(tmp_path)
    assert (tmp_path / "storyboard.csv").read_text() == "my storyboard"
    assert not (tmp_path / "adshot.toml").exists()


def test_provider_switch_and_price_verification(tmp_path):
    init_project(tmp_path)
    cfg = load_config(tmp_path)
    assert cfg.base_url == "https://api.openlux.ai"
    assert cfg.prices[MINI].usd_per_million == 2.41
    assert cfg.prices[MINI].verified
    assert cfg.prices[FAST].usd_per_million == 3.61
    assert cfg.prices[FAST].verified
    assert not cfg.prices[STD].verified and not cfg.prices[PRO].verified
    assert cfg.plan_base_url == cfg.base_url + "/v1"
    update_config(tmp_path, 'provider = "openlux"', 'provider = "ark"')
    ark = load_config(tmp_path)
    assert ark.base_url == "https://ark.cn-beijing.volces.com"
    assert not ark.prices[MINI].verified
    assert not ark.prices[FAST].verified
    assert ark.plan_base_url == ark.base_url + "/api/v3"
    update_config(tmp_path, 'base_url = ""\n', 'base_url = "https://example.test"\n')
    assert load_config(tmp_path).base_url == "https://example.test"


@pytest.mark.parametrize("field", ["base_url", "plan_base_url"])
@pytest.mark.parametrize("host", ["example.test", "localhost.example.test", "127.0.0.10"])
def test_remote_http_base_urls_are_rejected(tmp_path, field, host):
    init_project(tmp_path)
    update_config(tmp_path, f'\n{field} = ""', f'\n{field} = "http://{host}:8080"')
    with pytest.raises(ValueError, match="HTTPS"):
        load_config(tmp_path)


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_local_http_base_urls_can_have_a_port(tmp_path, host):
    init_project(tmp_path)
    local = f"http://{host}:8080"
    update_config(tmp_path, '\nbase_url = ""', f'\nbase_url = "{local}"')
    update_config(tmp_path, '\nplan_base_url = ""', f'\nplan_base_url = "{local}/chat"')
    cfg = load_config(tmp_path)
    assert (cfg.base_url, cfg.plan_base_url) == (local, local + "/chat")


def test_image_hash_and_notes_do_not_depend_on_filename(tmp_path):
    init_project(tmp_path)
    cfg = load_config(tmp_path)
    (tmp_path / "first.png").write_bytes(b"product")
    (tmp_path / "copy.png").write_bytes(b"product")
    write_rows(tmp_path, {"image": "first.png"})
    original = read_storyboard(tmp_path, cfg)[0]
    key, wire = image_reference(tmp_path, "first.png")
    assert key == original.image_key == hashlib.sha256(b"product").hexdigest()
    assert wire == "data:image/png;base64,cHJvZHVjdA=="
    write_rows(tmp_path, {"image": "copy.png", "notes": "Director's note"})
    assert read_storyboard(tmp_path, cfg)[0].signature == original.signature
    (tmp_path / "copy.png").write_bytes(b"changed")
    assert read_storyboard(tmp_path, cfg)[0].signature != original.signature
    remote = "https://example.test/bottle.jpg?revision=2"
    assert image_reference(tmp_path, remote) == (remote, remote)


@pytest.mark.parametrize("row", [
    {"shot_id": "../escape"}, {"shot_id": "COM1"}, {"shot_id": "S01.v1"},
    {"image": "http://example.test/bottle.png"}, {"image": "missing.jpg"},
    {"model": PRO, "image": "https://example.test/ref.png", "ratio": "16:9"},
    {"model": MINI, "resolution": "1080p"}, {"ratio": "3:4"}, {"seconds": -1},
    {"prompt": " "}, {"model": "custom-with-no-price"},
])
def test_invalid_rows_report_location(tmp_path, row):
    init_project(tmp_path)
    write_rows(tmp_path, row)
    with pytest.raises(ValueError, match="storyboard.csv"):
        read_storyboard(tmp_path, load_config(tmp_path))


def test_case_colliding_shot_ids_refused(tmp_path):
    init_project(tmp_path)
    write_rows(tmp_path, {"shot_id": "S01"}, {"shot_id": "s01"})
    with pytest.raises(ValueError, match="uplicate"):
        read_storyboard(tmp_path, load_config(tmp_path))


@pytest.mark.parametrize(("setting", "bad"), [
    ("budget_usd = 5.0", "budget_usd = nan"),
    ("concurrency = 2", "concurrency = true"),
    ('provider = "openlux"', 'provider = "typo"'),
    ('base_url = ""\n', 'base_url = "https://user:pass@example.test"\n'),
])
def test_invalid_config_fails_before_estimate(tmp_path, setting, bad):
    init_project(tmp_path)
    update_config(tmp_path, setting, bad)
    with pytest.raises(ValueError):
        load_config(tmp_path)


def test_out_of_range_duration_is_refused_before_submission(tmp_path):
    """Some gateways answer a 2-second Seedance request with a misleading 503 outage."""
    from adshot.config import init_project, load_config
    from adshot.storyboard import read_storyboard

    init_project(tmp_path)
    board = tmp_path / "storyboard.csv"
    lines = board.read_text(encoding="utf-8").splitlines()
    header, first = lines[0], lines[1]
    cells = next(csv.reader([first]))
    cells[2] = "2"
    with board.open("w", encoding="utf-8", newline="") as stream:
        stream.write(header + "\n")
        csv.writer(stream).writerow(cells)
    with pytest.raises(ValueError, match="4-15 seconds"):
        read_storyboard(tmp_path, load_config(tmp_path))
