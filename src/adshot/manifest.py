"""Atomic manifest updates and filesystem claims for a local project."""

import errno
import json
import math
import os
import re
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


def _try_lock(handle):
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError as exc:
        if exc.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            raise
        return False


@contextmanager
def file_guard(path, *, wait=False):
    # A stable inode gives each process the same OS lock. Closing the handle
    # (including on process death) releases it. Never unlink these guard files.
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        while not _try_lock(handle):
            if not wait:
                yield False
                return
            time.sleep(0.025)
        yield True


class Manifest:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "manifest.json"
        self.private = root / ".adshot"

    def _read(self):
        try:
            with self.path.open(encoding="utf-8") as handle:
                document = json.load(handle)
        except FileNotFoundError:
            return {"version": 1, "shots": {}}
        if not isinstance(document, dict) or document.get("version") != 1:
            raise ValueError("Unsupported manifest.json version. Restore the project manifest.")
        shots = document.get("shots")
        if not isinstance(shots, dict):
            raise ValueError("Invalid manifest.json shots. Restore the project manifest.")
        for shot_id, attempts in shots.items():
            if not isinstance(attempts, list) or not attempts:
                raise ValueError(f"Invalid manifest history for {shot_id}.")
            for attempt in attempts:
                if (not isinstance(attempt, dict) or not attempt.get("signature")
                        or attempt.get("state") not in {
                            "submitting", "needs_reconcile", "running", "publishing", "done", "failed"
                        }):
                    raise ValueError(f"Invalid manifest attempt for {shot_id}.")
                price = attempt.get("price")
                if (type(price) not in (int, float) or not math.isfinite(price) or price < 0
                        or not isinstance(attempt.get("base_url"), str)):
                    raise ValueError(f"Invalid manifest rate or endpoint for {shot_id}.")
                if attempt["state"] in ("running", "publishing", "done", "failed"):
                    if not isinstance(attempt.get("task_id"), str) or not attempt["task_id"]:
                        raise ValueError(f"Missing task ID in manifest.json for {shot_id}.")
                if attempt["state"] == "publishing":
                    backup = attempt.get("backup")
                    if backup != "" and not (isinstance(backup, str) and re.fullmatch(
                            re.escape(shot_id) + r"\.v[1-9][0-9]*\.mp4", backup)):
                        raise ValueError(f"Invalid backup filename in manifest.json for {shot_id}.")
        return document

    def _write(self, document):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.private,
                                         delete=False, suffix=".tmp") as handle:
            tmp = Path(handle.name)
            try:
                json.dump(document, handle, indent=2, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            except BaseException:
                handle.close()
                tmp.unlink(missing_ok=True)
                raise
        try:
            os.replace(tmp, self.path)
        finally:
            tmp.unlink(missing_ok=True)

    def snapshot(self):
        with file_guard(self.private / "manifest.guard", wait=True):
            return self._read()["shots"]

    def record(self, shot_id, *, new=False, reconcile=False, **fields):
        # Callers hold the per-shot claim. The shorter manifest lock also
        # serializes read/modify/write across workers processing different shots.
        with file_guard(self.private / "manifest.guard", wait=True):
            document = self._read()
            if reconcile:
                task_id = fields["task_id"]
                for owner, history in document["shots"].items():
                    if owner != shot_id and any(a.get("task_id") == task_id for a in history):
                        raise ValueError(f"Task ID already recorded for {owner}. Check provider history.")
            attempts = document["shots"].setdefault(shot_id, [])
            if new:
                attempts.append(fields)
            else:
                attempts[-1].update(fields)
            self._write(document)
            return attempts[-1]

    def discard_unsent_intent(self, shot_id):
        """Undo the caller's intent while it still holds the claim and has sent no POST."""
        with file_guard(self.private / "manifest.guard", wait=True):
            document = self._read()
            attempts = document["shots"][shot_id]
            if attempts[-1]["state"] != "submitting":
                raise ValueError(f"{shot_id} has no unsent intent to discard.")
            attempts.pop()
            if not attempts:
                del document["shots"][shot_id]
            self._write(document)

    @contextmanager
    def claim(self, shot_id):
        folder = self.private / "claims"
        with file_guard(folder / f"{shot_id}.guard") as acquired:
            if not acquired:
                yield False
                return
            path = folder / f"{shot_id}.lock"
            # The OS guard proves that any leftover exclusive claim is orphaned.
            path.unlink(missing_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            try:
                yield True
            finally:
                path.unlink(missing_ok=True)

    def recover(self, ids):
        snapshot = self.snapshot()
        for shot_id in ids:
            attempts = snapshot.get(shot_id)
            if attempts and attempts[-1]["state"] == "submitting":
                with self.claim(shot_id) as acquired:
                    if acquired and self.snapshot()[shot_id][-1]["state"] == "submitting":
                        self.record(shot_id, state="needs_reconcile")
        return self.snapshot()

    @contextmanager
    def slot(self, count, stop):
        while not stop.is_set():
            for slot in range(count):
                with file_guard(self.private / f"worker-{slot}.guard") as acquired:
                    if acquired:
                        yield True
                        return
            stop.wait(0.05)
        yield False
