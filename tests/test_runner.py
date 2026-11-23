"""Exercise the CLI and the on-disk protocol with an offline provider."""

import csv
import io
import json
import multiprocessing
import os
import re
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest


class OfflineVideo:
    """The receipt files stand in for accepted jobs on a separate service."""

    def __init__(self, base_url):
        self.base_url = base_url

    def check_credentials(self):
        pass

    def submit(self, shot, image_url):
        from uuid import uuid4

        task_id = f"{shot.shot_id}-{uuid4().hex}"
        from adshot.manifest import Manifest

        record = Manifest(Path.cwd()).snapshot()[shot.shot_id][-1]
        assert record["state"] == "submitting"
        assert record["signature"] == shot.signature
        Path("receipts", task_id).write_text(shot.prompt)
        if Path("crash-after-accept").exists():
            os._exit(71)
        return task_id

    def poll(self, task_id):
        if Path("poll-interrupted").exists():
            raise RuntimeError("simulated interrupted GET")
        return {"status": "succeeded", "content": {"video_url": task_id},
                "usage": {"total_tokens": 40594}}

    def download(self, url, dest):
        dest.write_bytes(Path("receipts", url).read_bytes())


def invoke(*args):
    from adshot.cli import main

    output = io.StringIO()
    with redirect_stdout(output), redirect_stderr(output):
        code = main(list(args))
    return code, output.getvalue()


def process_run(root, args, entered=None, release=None):
    from adshot import runner

    class HeldSubmission(OfflineVideo):
        def submit(self, shot, image_url):
            task_id = super().submit(shot, image_url)
            if shot.shot_id == "S01":
                entered.set()
                assert release.wait(timeout=20)
            return task_id

    os.chdir(root)
    runner.VideoProvider = OfflineVideo if entered is None else HeldSubmission
    code, _ = invoke("run", "--yes", *args)
    raise SystemExit(code)


@pytest.fixture
def project(tmp_path, monkeypatch):
    from adshot import runner

    monkeypatch.setattr(runner, "VideoProvider", OfflineVideo)
    monkeypatch.chdir(tmp_path)
    assert invoke("init", ".")[0] == 0
    Path("receipts").mkdir()
    return tmp_path


def board():
    with Path("storyboard.csv").open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def save(rows):
    with Path("storyboard.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)


def receipts():
    return list(Path("receipts").iterdir())


def test_zero_cost_rerun_and_single_row_revision(project):
    assert invoke("run", "--yes", "--budget", "100")[0] == 0
    assert len(receipts()) == 4
    code, estimate = invoke("estimate")
    assert code == 0 and "Total new spend: $0.0000" in estimate
    assert invoke("run", "--yes", "--budget", "0")[0] == 0
    assert len(receipts()) == 4
    old = Path("shots/S03.mp4").read_bytes()
    rows = board()
    rows[2]["prompt"] += " The camera drifts left."
    rows[0]["notes"] += " Approved."
    save(rows)
    assert "changed" in invoke("status")[1]
    assert invoke("run", "--yes", "--budget", "100")[0] == 0
    assert len(receipts()) == 5
    assert Path("shots/S03.v1.mp4").read_bytes() == old
    assert Path("shots/S03.mp4").read_bytes() != old


def test_budget_denies_before_submission(project):
    code, output = invoke("run", "--yes", "--budget", "0.01")
    assert code and "exceeds budget" in output
    assert output.index("Total new spend") < output.index("exceeds budget")
    assert not receipts()


def test_quickstart_from_parent_uses_only_one_unambiguous_child(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert invoke("init", "demo")[0] == 0
    code, output = invoke("estimate")
    assert code == 0 and "S04" in output and "unverified price" not in output
    assert invoke("init", "second")[0] == 0
    assert invoke("estimate")[0] != 0
    assert invoke("run", "--yes")[0] != 0


def test_installed_template_estimate_matches_readme(tmp_path, monkeypatch):
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")
    documented = re.search(r"([\d,]+) tokens and \$([\d.]+)", readme)
    assert documented, "README quickstart must state its estimated tokens and USD"
    monkeypatch.chdir(tmp_path)
    assert invoke("init", "demo")[0] == 0
    code, estimate = invoke("estimate")
    assert code == 0
    rows = re.findall(r"^S\d+\s+pending\s+submit\s+(\d+)", estimate, re.MULTILINE)
    assert sum(map(int, rows)) == int(documented[1].replace(",", ""))
    assert re.search(r"Total new spend: \$([\d.]+)", estimate)[1] == documented[2]


def test_declining_confirmation_spends_nothing(project, monkeypatch):
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert invoke("run", "--budget", "5")[0] != 0
    assert not receipts()


def test_reconcile_retry_needs_specific_consent_and_fresh_budget(project, monkeypatch):
    def uncertain(*_):
        raise RuntimeError("no receipt")

    original = OfflineVideo.submit
    monkeypatch.setattr(OfflineVideo, "submit", uncertain)
    assert invoke("run", "--only", "S02", "--yes")[0] != 0
    monkeypatch.setattr(OfflineVideo, "submit", original)
    monkeypatch.setattr("builtins.input", lambda _: "")
    assert invoke("run", "--reconcile", "S02", "--yes")[0] != 0
    monkeypatch.setattr("builtins.input", lambda _: "RETRY S02")
    code, output = invoke("run", "--reconcile", "S02", "--yes", "--budget", "0")
    assert code != 0 and "exceeds budget" in output and not receipts()
    assert "needs_reconcile" in invoke("status")[1]
    assert invoke("run", "--reconcile", "S02", "--yes")[0] == 0
    assert len(receipts()) == 1


def test_rejected_receipt_does_not_clear_reconciliation(project, monkeypatch):
    def ambiguous(*_):
        raise RuntimeError("connection lost")

    monkeypatch.setattr(OfflineVideo, "submit", ambiguous)
    assert invoke("run", "--only", "S02", "--yes")[0] != 0
    monkeypatch.setattr("builtins.input", lambda _: "unknown-task")
    monkeypatch.setattr(OfflineVideo, "poll", ambiguous)
    assert invoke("run", "--reconcile", "S02", "--yes")[0] != 0
    assert "needs_reconcile" in invoke("status")[1]


def test_provider_switch_and_edit_during_pending_job(project):
    pause = Path("poll-interrupted")
    pause.touch()
    assert invoke("run", "--only", "S01", "--yes")[0] != 0
    pause.unlink()
    file = Path("adshot.toml")
    original = file.read_text()
    file.write_text(original.replace('provider = "openlux"', 'provider = "ark"'))
    code, output = invoke("run", "--only", "S01", "--yes")
    assert code and "original base_url" in output
    file.write_text(original)
    rows = board()
    rows[0]["prompt"] += " Tilt down."
    save(rows)
    code, output = invoke("run", "--only", "S01", "--yes", "--budget", "0")
    assert code == 0 and "recovered previous version" in output
    assert len(receipts()) == 1
    assert "changed" in invoke("status")[1]


def test_restart_during_publish_preserves_both_versions(project, monkeypatch):
    from adshot import runner

    assert invoke("run", "--only", "S01", "--yes")[0] == 0
    old = Path("shots/S01.mp4").read_bytes()
    rows = board()
    rows[0]["prompt"] += " Move forward."
    save(rows)
    with monkeypatch.context() as patch:
        def interrupted(*_):
            raise OSError("simulated power loss before publish")

        patch.setattr(runner, "_publish", interrupted)
        assert invoke("run", "--only", "S01", "--yes")[0] != 0
    assert invoke("run", "--only", "S01", "--yes", "--budget", "0")[0] == 0
    assert Path("shots/S01.v1.mp4").read_bytes() == old
    assert Path("shots/S01.mp4").read_bytes() != old
    assert len(receipts()) == 2
    status = invoke("status")[1]
    assert "$0.2931" in status and "unverified price" not in status


def test_reverting_a_failed_revision_reuses_the_current_download(project, monkeypatch):
    assert invoke("run", "--only", "S01", "--yes")[0] == 0
    before = board()
    edited = board()
    edited[0]["prompt"] += " Zoom out."
    save(edited)
    monkeypatch.setattr(OfflineVideo, "poll", lambda *_: {"status": "failed"})
    assert invoke("run", "--only", "S01", "--yes")[0] != 0
    assert "failed" in invoke("status")[1]
    save(before)
    assert invoke("run", "--only", "S01", "--budget", "0", "--yes")[0] == 0
    assert len(receipts()) == 2


def test_corrupt_task_record_blocks_with_clear_error(project):
    assert invoke("run", "--only", "S01", "--yes")[0] == 0
    path = Path("manifest.json")
    data = json.loads(path.read_text())
    current = data["shots"]["S01"][-1]
    current["state"] = "running"
    del current["task_id"]
    path.write_text(json.dumps(data))
    code, output = invoke("run", "--only", "S01", "--yes")
    assert code != 0 and "manifest" in output
    assert len(receipts()) == 1


def test_measured_mini(project):
    row = board()[0]
    row.update(model="doubao-seedance-2-0-mini-260615", resolution="480p",
               ratio="9:16", seconds="4")
    save([row])
    code, output = invoke("estimate")
    assert code == 0
    line = next(line for line in output.splitlines() if line.startswith("S01"))
    tokens, usd = re.search(r"(\d+)\s+\$([\d.]+)", line).groups()
    assert int(tokens) == pytest.approx(40594, rel=0.1)
    assert float(usd) == pytest.approx(0.098, rel=0.1)


def test_process_death_quarantines_shot_until_receipt_is_attached(project, monkeypatch):
    ctx = multiprocessing.get_context("spawn")
    crash = Path("crash-after-accept")
    crash.touch()
    child = ctx.Process(target=process_run, args=(project, ["--only", "S03"]))
    child.start()
    child.join(20)
    assert child.exitcode == 71
    crash.unlink()
    assert "needs_reconcile" in invoke("status")[1]
    assert invoke("run", "--yes", "--only", "S03")[0] != 0
    assert len(receipts()) == 1
    monkeypatch.setattr("builtins.input", lambda _: receipts()[0].name)
    assert invoke("run", "--reconcile", "S03", "--yes", "--budget", "0")[0] == 0
    assert len(receipts()) == 1
    assert Path("shots/S03.mp4").is_file()


def test_resumes_known_id_and_recovers_missing_output(project):
    interrupted = Path("poll-interrupted")
    interrupted.touch()
    assert invoke("run", "--only", "S01", "--yes")[0] != 0
    assert len(receipts()) == 1
    assert "running" in invoke("status")[1]
    interrupted.unlink()
    assert invoke("run", "--only", "S01", "--yes", "--budget", "0")[0] == 0
    Path("shots/S01.mp4").unlink()
    assert invoke("run", "--only", "S01", "--yes", "--budget", "0")[0] == 0
    assert Path("shots/S01.mp4").stat().st_size > 0 and len(receipts()) == 1


def test_two_processes_submit_each_shot_once(project):
    ctx = multiprocessing.get_context("spawn")
    entered, release = ctx.Event(), ctx.Event()
    owner = ctx.Process(target=process_run, args=(project, [], entered, release))
    rival = ctx.Process(target=process_run, args=(project, ["--only", "S01"]))
    owner.start()
    try:
        assert entered.wait(timeout=15)
        rival.start()
        rival.join(10)
        assert rival.exitcode == 0
        assert len(list(Path("receipts").glob("S01-*"))) == 1
        assert not Path("shots/S01.mp4").exists()
    finally:
        release.set()
        owner.join(15)
        if rival.pid is not None:
            rival.join(15)
    assert owner.exitcode == 0
    assert sorted(p.name.split("-")[0] for p in receipts()) == [r["shot_id"] for r in board()]
    assert len(list(Path("shots").glob("*.mp4"))) == 4


def latest(shot_id):
    return json.loads(Path("manifest.json").read_text())["shots"][shot_id][-1]


@pytest.mark.parametrize("window", ["poll", "next_intent"])
def test_interrupt_after_first_submission_stops_further_spend(project, monkeypatch, window):
    from _thread import interrupt_main
    from adshot.manifest import Manifest

    settings = Path("adshot.toml")
    settings.write_text(settings.read_text().replace("concurrency = 2", "concurrency = 1"))
    shutting_down = Event()
    original_shutdown = ThreadPoolExecutor.shutdown

    def shutdown(pool, *args, **kwargs):
        shutting_down.set()
        return original_shutdown(pool, *args, **kwargs)

    def interrupt():
        interrupt_main()
        assert shutting_down.wait(5), "Main thread did not handle Ctrl+C"

    monkeypatch.setattr(ThreadPoolExecutor, "shutdown", shutdown)
    if window == "poll":
        def interrupt_poll(provider, task_id):
            interrupt()
            return {"status": "succeeded", "content": {"video_url": task_id}}

        monkeypatch.setattr(OfflineVideo, "poll", interrupt_poll)
    else:
        original_record = Manifest.record

        def interrupt_after_intent(manifest, shot_id, **fields):
            saved = original_record(manifest, shot_id, **fields)
            if fields.get("new") and shot_id == "S02":
                interrupt()
            return saved

        monkeypatch.setattr(Manifest, "record", interrupt_after_intent)
    assert invoke("run", "--yes")[0] == 130
    assert len(receipts()) == 1
    assert latest("S01")["task_id"] == receipts()[0].name
    assert len(json.loads(Path("manifest.json").read_text())["shots"]) == 1


def test_unverified_guard_checks_entire_selection_and_only_new_submissions(project):
    from adshot.config import STD

    rows = board()
    rows[2]["model"] = STD
    save(rows)
    code, output = invoke("run", "--yes")
    assert code and "--accept-unverified-prices" in output and not receipts()
    args = ("--only", "S03", "--yes")
    assert invoke("run", *args, "--accept-unverified-prices", "--budget", "0")[0] != 0
    assert not receipts()
    pause = Path("poll-interrupted")
    pause.touch()
    assert invoke("run", *args, "--accept-unverified-prices")[0] != 0
    assert len(receipts()) == 1
    pause.unlink()
    assert invoke("run", *args, "--budget", "0")[0] == 0
    assert invoke("run", "--yes")[0] == 0
    assert len(receipts()) == 4
    assert "unverified price" in invoke("status")[1]


def test_reconcile_retry_also_requires_unverified_price_consent(project, monkeypatch):
    from adshot.config import STD

    rows = board()
    rows[0]["model"] = STD
    save(rows)
    with monkeypatch.context() as patch:
        def no_receipt(*_):
            raise ConnectionError()

        patch.setattr(OfflineVideo, "submit", no_receipt)
        assert invoke("run", "--only", "S01", "--yes", "--accept-unverified-prices")[0] != 0
    monkeypatch.setattr("builtins.input", lambda _: "RETRY S01")
    code, output = invoke("run", "--reconcile", "S01", "--yes")
    assert code and "--accept-unverified-prices" in output and not receipts()
    assert latest("S01")["state"] == "needs_reconcile"


@pytest.mark.parametrize("wrong_owner", ["model", "other_shot"])
def test_reconcile_rejects_foreign_task_and_preserves_hold(project, monkeypatch, wrong_owner):
    from adshot.config import FAST, STD

    assert invoke("run", "--only", "S02", "--yes")[0] == 0
    other_id = latest("S02")["task_id"]
    with monkeypatch.context() as patch:
        def no_receipt(*_):
            raise ConnectionError()

        patch.setattr(OfflineVideo, "submit", no_receipt)
        assert invoke("run", "--only", "S03", "--yes")[0] == 1
    # A later CSV edit must not change the model that received the uncertain POST.
    rows = board()
    rows[2]["model"] = STD
    save(rows)
    task = "foreign-model" if wrong_owner == "model" else other_id
    monkeypatch.setattr("builtins.input", lambda _: task)
    monkeypatch.setattr(OfflineVideo, "poll", lambda *_: {
        "status": "failed", "model": STD if wrong_owner == "model" else FAST})
    code, output = invoke("run", "--reconcile", "S03", "--yes")
    assert code != 0 and ("model" if wrong_owner == "model" else "S02") in output
    assert latest("S03")["state"] == "needs_reconcile"
    assert "task_id" not in latest("S03")
    assert len(receipts()) == 1


def test_new_jobs_keep_receipts_when_different_providers_use_the_same_id(project, monkeypatch):
    def submit(provider, shot, image_url):
        Path("receipts", "job-1").write_text(shot.prompt)
        return "job-1"

    monkeypatch.setattr(OfflineVideo, "submit", submit)
    assert invoke("run", "--only", "S01", "--yes")[0] == 0
    settings = Path("adshot.toml")
    settings.write_text(settings.read_text().replace('provider = "openlux"', 'provider = "ark"'))
    assert invoke("run", "--only", "S02", "--yes", "--accept-unverified-prices")[0] == 0
    assert latest("S01")["task_id"] == latest("S02")["task_id"] == "job-1"
    assert latest("S01")["base_url"] != latest("S02")["base_url"]


def poll_clock(monkeypatch, step=None):
    from adshot import runner

    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(runner, "time", SimpleNamespace(
        monotonic=lambda: len(sleeps) * step if step else sum(sleeps), sleep=sleep))
    return sleeps


def test_poll_transitions_through_known_and_unknown_states(project, monkeypatch):
    sleeps = poll_clock(monkeypatch)
    states = iter(["queued", "pending", "rendering_frames", "running", "succeeded"])
    seen = []

    def poll(provider, task_id):
        seen.append(task_id)
        return {"status": next(states), "content": {"video_url": task_id}}

    monkeypatch.setattr(OfflineVideo, "poll", poll)
    assert invoke("run", "--only", "S03", "--yes")[0] == 0
    assert sleeps == [5] * 4 and seen == [latest("S03")["task_id"]] * 5
    assert latest("S03")["state"] == "done"


@pytest.mark.parametrize("abandon", [False, True], ids=["resume", "abandon"])
def test_poll_timeout_retains_id_and_can_resume_or_abandon(project, monkeypatch, abandon):
    poll_clock(monkeypatch, step=1800)
    args = ("run", "--only", "S03", "--yes")
    with monkeypatch.context() as patch:
        states = iter(["queued", "unknown_final_state"])
        patch.setattr(OfflineVideo, "poll", lambda *_: {"status": next(states)})
        code, output = invoke(*args)
        assert code and "unknown_final_state" in output and "timed out" in output
    task = receipts()[0].name
    assert latest("S03")["state"] == "running" and latest("S03")["task_id"] == task
    if abandon:
        monkeypatch.setattr("builtins.input", lambda _: "yes")
        assert invoke("abandon", "S03")[0] != 0
        assert latest("S03")["state"] == "running"
        monkeypatch.setattr("builtins.input", lambda _: "RETRY")
        assert invoke("abandon", "S03")[0] == 0
        assert latest("S03")["state"] == "failed" and latest("S03")["task_id"] == task
        assert "submit" in invoke("estimate", "--only", "S03")[1]
        assert invoke(*args, "--budget", "0")[0] != 0 and len(receipts()) == 1
        assert invoke(*args)[0] == 0 and len(receipts()) == 2
    else:
        assert invoke(*args, "--budget", "0")[0] == 0 and len(receipts()) == 1
        assert latest("S03")["task_id"] == task


def test_abandon_done_shot_with_missing_local_video_and_expired_url(project, monkeypatch):
    from adshot.manifest import Manifest
    from adshot.providers import ProviderError

    args = ("run", "--only", "S03", "--yes")
    abandon = ("abandon", "S03", "--confirm", "RETRY")
    assert invoke(*args)[0] == 0
    assert invoke(*abandon)[0] != 0  # A healthy download is already available.
    task = latest("S03")["task_id"]
    Path("shots/S03.mp4").unlink()
    with monkeypatch.context() as patch:
        def expired(*_):
            raise ProviderError("Video download returned HTTP 410.")

        patch.setattr(OfflineVideo, "download", expired)
        assert invoke(*args, "--budget", "0")[0] != 0
        assert latest("S03")["task_id"] == task
    with Manifest(project).claim("S03"):
        assert invoke(*abandon)[0] != 0
    assert invoke(*abandon)[0] == 0
    assert latest("S03")["state"] == "failed" and latest("S03")["task_id"] == task
    assert len(receipts()) == 1
    assert invoke(*args)[0] == 0 and len(receipts()) == 2


def test_refused_submission_is_not_held_for_reconcile(project, monkeypatch):
    """A refusal of the key or balance creates no task, so a plain rerun is safe."""
    from adshot.providers import RejectedSubmission

    def refused(*_):
        raise RejectedSubmission("Provider refused the request with HTTP 403 before creating a task.")

    original = OfflineVideo.submit
    monkeypatch.setattr(OfflineVideo, "submit", refused)
    code, output = invoke("run", "--only", "S02", "--yes")
    assert code != 0 and "before creating a task" in output and "reconcile" not in output
    assert not receipts()
    status = invoke("status")[1]
    assert "needs_reconcile" not in status and "unknown" not in status and "failed" not in status
    assert "submit" in invoke("estimate", "--only", "S02")[1]
    monkeypatch.setattr(OfflineVideo, "submit", original)
    assert invoke("run", "--only", "S02", "--yes")[0] == 0
    assert len(receipts()) == 1
