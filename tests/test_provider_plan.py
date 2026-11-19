import csv
import io
import json
import re
import time
from types import SimpleNamespace
from urllib import error, request

import pytest


class Reply(io.BytesIO):
    def __init__(self, body, **headers):
        super().__init__(body)
        self.headers = headers


@pytest.fixture
def wire(monkeypatch):
    calls, replies = [], []

    def open_request(opener, req, *args, **kwargs):
        calls.append((opener, req))
        if not replies:
            pytest.fail("Unexpected HTTP request")
        reply = replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, Reply):
            return reply
        return Reply(json.dumps(reply).encode())

    monkeypatch.setattr(request.OpenerDirector, "open", open_request)
    monkeypatch.setenv("ADSHOT_API_KEY", "test-video-key")
    monkeypatch.delenv("ADSHOT_PLAN_KEY", raising=False)
    return calls, replies


def test_video_wire_and_download(wire, tmp_path):
    from adshot.providers import VideoProvider

    calls, replies = wire
    provider = VideoProvider("https://video.example/")
    shot = SimpleNamespace(model="fast", prompt="Bottle turns", seconds=3,
                           resolution="720p", ratio="9:16")
    endpoint = "https://video.example/api/v3/contents/generations/tasks"
    result = {"status": "succeeded", "content": {"video_url": "https://cdn.example/video"},
              "usage": {"total_tokens": 42}}
    replies.extend([{"id": "task_1", "task_id": "fallback"}, result, Reply(b"clip")])
    task = provider.submit(shot, "https://images.example/product.jpg")
    assert task == "task_1"
    opener, post = calls[0]
    assert post.get_method() == "POST"
    assert post.full_url == endpoint
    assert post.get_header("Authorization") == "Bearer test-video-key"
    assert json.loads(post.data) == {
        "model": "fast", "content": [
            {"type": "text", "text": "Bottle turns"},
            {"type": "image_url", "image_url": {"url": "https://images.example/product.jpg"},
             "role": "first_frame"},
        ], "resolution": "720p", "ratio": "9:16", "duration": 3,
        "generate_audio": False, "watermark": False,
    }
    redirect = next(h for h in opener.handlers if isinstance(h, request.HTTPRedirectHandler))
    assert redirect.redirect_request(post, None, 302, "", {}, "https://other.example") is None
    assert provider.poll(task) == result
    assert calls[1][1].full_url == endpoint + "/task_1"
    assert calls[1][1].get_method() == "GET"
    provider.download(result["content"]["video_url"], tmp_path / "clip")
    assert (tmp_path / "clip").read_bytes() == b"clip"
    assert calls[2][1].get_header("Authorization") is None


def test_missing_key_and_http_error_do_not_leak_details(wire, monkeypatch):
    from adshot.providers import VideoProvider

    calls, replies = wire
    monkeypatch.delenv("ADSHOT_API_KEY")
    provider = VideoProvider("https://video.example")
    with pytest.raises(ValueError, match="ADSHOT_API_KEY"):
        provider.check_credentials()
    assert not calls
    monkeypatch.setenv("ADSHOT_API_KEY", "api-test-value")
    replies.append(error.HTTPError("https://video.example", 403, "api-test-value", {},
                                   io.BytesIO(b"api-test-value")))
    with pytest.raises(RuntimeError, match="403") as exc:
        provider.poll("task_1")
    assert "api-test-value" not in str(exc.value)


def test_truncated_download_reports_failure(wire, tmp_path):
    from adshot.providers import VideoProvider

    wire[1].append(Reply(b"x", **{"Content-Length": "12"}))
    with pytest.raises(RuntimeError, match="incomplete"):
        VideoProvider("https://video.example").download("https://cdn.example/video", tmp_path / "part")


def test_submit_fallback_id_and_missing_id_never_retries(wire):
    from adshot.providers import VideoProvider

    calls, replies = wire
    replies.extend([{"task_id": "task_alternate"}, {}])
    shot = SimpleNamespace(model="draft", prompt="Bottle on marble", seconds=2,
                           resolution="480p", ratio="1:1")
    provider = VideoProvider("https://video.example")
    assert provider.submit(shot, None) == "task_alternate"
    assert json.loads(calls[0][1].data)["content"] == [{"type": "text", "text": shot.prompt}]
    with pytest.raises(RuntimeError, match="no task ID"):
        provider.submit(shot, None)
    assert len(calls) == 2


@pytest.mark.parametrize("recover", [True, False])
def test_flaky_poll_retries_network_and_5xx_with_bounded_backoff(wire, monkeypatch, recover):
    from adshot.providers import ProviderError, VideoProvider

    calls, replies = wire
    delays = []
    monkeypatch.setattr(time, "sleep", delays.append)
    unavailable = error.HTTPError("https://video.example", 503, "", {}, None)
    replies.extend([error.URLError("offline"), unavailable, ConnectionResetError(),
                    {"status": "running"} if recover else TimeoutError()])
    provider = VideoProvider("https://video.example")
    if recover:
        assert provider.poll("receipt") == {"status": "running"}
    else:
        with pytest.raises(ProviderError):
            provider.poll("receipt")
    assert delays == [1, 3, 9]
    assert [req.get_method() for _, req in calls] == ["GET"] * 4
    assert len({req.full_url for _, req in calls}) == 1


@pytest.mark.parametrize("failure", [TimeoutError(), error.HTTPError(
    "https://video.example", 503, "", {}, None)])
def test_transient_post_failure_is_never_retried(wire, failure):
    from adshot.providers import ProviderError, VideoProvider

    calls, replies = wire
    replies.append(failure)
    shot = SimpleNamespace(model="fast", prompt="Bottle turns", seconds=4,
                           resolution="720p", ratio="9:16")
    with pytest.raises(ProviderError):
        VideoProvider("https://video.example").submit(shot, None)
    assert [req.get_method() for _, req in calls] == ["POST"]


def test_cli_never_writes_or_prints_api_key(wire, tmp_path, monkeypatch, capsys):
    from adshot import runner
    from adshot.cli import main

    calls, replies = wire
    sentinel = "audit-test-key-702e9c"
    monkeypatch.setenv("ADSHOT_API_KEY", sentinel)
    monkeypatch.chdir(tmp_path)
    assert main(["init", "."]) == 0
    replies.extend([{"id": "receipt"}, {"status": "succeeded", "usage": {"total_tokens": 87300},
                    "content": {"video_url": "https://cdn.example/clip?signature=" + sentinel}},
                    Reply(b"video")])
    assert main(["run", "--only", "S03", "--yes"]) == 0
    replies.append(error.HTTPError("https://video.example", 403, sentinel, {},
                                   io.BytesIO(sentinel.encode())))
    assert main(["run", "--only", "S04", "--yes"]) == 1
    # A gateway may echo authentication details even in its status field.
    monkeypatch.setattr(runner, "time", SimpleNamespace(monotonic=iter([0, 1800]).__next__))
    replies.extend([{"id": "stalled"}, {"status": "stalled-" + sentinel}])
    assert main(["run", "--only", "S01", "--yes"]) == 1
    assert main(["status"]) == 0
    assert calls[0][1].get_header("Authorization") == "Bearer " + sentinel
    assert calls[2][1].get_header("Authorization") is None
    output = capsys.readouterr()
    assert sentinel not in output.out and sentinel not in output.err
    assert (tmp_path / "manifest.json").is_file()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert sentinel.encode() not in path.read_bytes(), path.name


def chat_reply(rows):
    return {"choices": [{"message": {"content": json.dumps({"shots": rows})}}]}


@pytest.fixture
def planner_config():
    return SimpleNamespace(plan_model="vision-chat", plan_base_url="https://chat.example/v1/",
                           base_url="https://video.example", ratio="9:16", model="fast")


@pytest.mark.parametrize("plan_key", [True, False])
def test_planner_writes_validated_csv(wire, tmp_path, planner_config, monkeypatch, plan_key):
    from adshot.plan import plan

    if plan_key:
        monkeypatch.setenv("ADSHOT_PLAN_KEY", "test-planner-key")
    (tmp_path / "bottle.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    wire[1].append(chat_reply([{"prompt": "Light skims the bottle", "seconds": 4},
                               {"prompt": "Center the cap", "seconds": 5, "notes": "Hero"}]))
    plan(tmp_path, planner_config, "bottle.jpg", "Show the cap", 2, 9)
    post = wire[0][0][1]
    assert post.full_url == "https://chat.example/v1/chat/completions"
    assert post.get_header("Authorization") == "Bearer " + (
        "test-planner-key" if plan_key else "test-video-key"
    )
    payload = json.loads(post.data)
    assert payload["model"] == "vision-chat"
    user_message = payload["messages"][-1]["content"]
    assert any("Show the cap" in part.get("text", "") for part in user_message)
    assert any(part.get("image_url", {}).get("url", "").startswith("data:image/jpeg;base64,")
               for part in user_message)
    with (tmp_path / "storyboard.csv").open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert [row["shot_id"] for row in rows] == ["S01", "S02"]
    assert [row["seconds"] for row in rows] == ["4", "5"]
    assert rows[1]["image"] == "bottle.jpg"
    assert rows[1]["notes"] == "Hero"


def test_planner_preserves_existing_board_on_invalid_response(wire, tmp_path, planner_config):
    from adshot.plan import plan

    board = tmp_path / "storyboard.csv"
    board.write_bytes(b"existing")
    wire[1].append(chat_reply([{"prompt": "product", "seconds": 3}]))
    with pytest.raises(ValueError, match="duration|seconds"):
        plan(tmp_path, planner_config, "https://images.example/b.jpg", "Ad", 1, 4)
    assert board.read_bytes() == b"existing"


def test_impossible_planning_duration_is_rejected_offline(wire, tmp_path, planner_config):
    from adshot.plan import plan

    with pytest.raises(ValueError, match="seconds"):
        plan(tmp_path, planner_config, "https://images.example/b.jpg", "Ad", 6, 10)
    assert not wire[0]


def test_planner_prompt_range_matches_validated_durations(wire, tmp_path, planner_config):
    from adshot.plan import _validated_shots, plan

    def reply(seconds):
        return chat_reply([{"prompt": "Light skims the bottle", "seconds": seconds}])

    wire[1].append(reply(4))
    plan(tmp_path, planner_config, "https://images.example/b.jpg", "Ad", 1, 4)
    prompt = json.loads(wire[0][0][1].data)["messages"][0]["content"]
    low, high = map(int, re.search(r"seconds \(integer, (\d+) through (\d+)\)", prompt).groups())
    assert (low, high) == (4, 6)
    for seconds in range(low, high + 1):
        assert _validated_shots(reply(seconds), 1, seconds)[0]["seconds"] == seconds
    for seconds in (low - 1, high + 1):
        with pytest.raises(ValueError, match="duration"):
            _validated_shots(reply(seconds), 1, seconds)
