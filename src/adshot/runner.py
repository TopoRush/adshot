"""Submit once, recover existing jobs, and publish complete media atomically."""

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import replace
from decimal import Decimal
from threading import Event

from .estimate import quote, show_estimate, token_cost, token_estimate
from .providers import ProviderError, VideoProvider
from .storyboard import image_reference


def _publish(manifest, shot_id, attempt):
    folder = manifest.root / "shots"
    stage = folder / f".{shot_id}.part"
    output = folder / f"{shot_id}.mp4"
    if stage.is_file():
        backup = attempt["backup"]
        if backup and not (folder / backup).exists():
            os.replace(output, folder / backup)
        os.replace(stage, output)
    if not output.is_file() or not output.stat().st_size:
        raise ValueError(f"{shot_id}: staged video is missing. Restore the shots directory.")
    manifest.record(shot_id, state="done")


def _download(manifest, shot_id, provider, url):
    folder = manifest.root / "shots"
    folder.mkdir(exist_ok=True)
    stage = folder / f".{shot_id}.part"
    provider.download(url, stage)
    backup = ""
    if (folder / f"{shot_id}.mp4").exists():
        version = 1
        while (folder / f"{shot_id}.v{version}.mp4").exists():
            version += 1
        backup = f"{shot_id}.v{version}.mp4"
    attempt = manifest.record(shot_id, state="publishing", backup=backup)
    _publish(manifest, shot_id, attempt)


TERMINAL = ("succeeded", "failed", "cancelled", "expired")


def _poll(manifest, shot_id, attempt, provider, stop, response=None):
    deadline = time.monotonic() + 1800
    while not stop.is_set():
        if response is None:
            response = provider.poll(attempt["task_id"])
        state = response.get("status")
        # Gateways name in-progress states differently (queued, pending, submitted,
        # processing, running...). Anything that is not a terminal state keeps polling.
        if state in TERMINAL or not isinstance(state, str) or not state:
            break
        if time.monotonic() >= deadline:
            key = os.environ.get("ADSHOT_API_KEY", "").strip()
            if key:
                state = state.replace(key, "[redacted]")
            raise RuntimeError(f"Polling timed out. Last provider status: {state!r}. "
                               f"Run again to resume, or use adshot abandon {shot_id} after "
                               "checking provider history.")
        time.sleep(5)
        response = None
    else:
        raise RuntimeError("Polling interrupted. Task ID retained for resume.")
    usage = response.get("usage") or {}
    tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
    if type(tokens) is int and tokens >= 0:
        manifest.record(shot_id, tokens=tokens)
    if state in ("failed", "cancelled", "expired"):
        manifest.record(shot_id, state="failed", provider_status=state)
        raise RuntimeError(f"The provider marked the task {state}. Check its history before retrying.")
    if state != "succeeded":
        raise RuntimeError("Unexpected task status. Task ID retained for resume.")
    content = response.get("content")
    if not isinstance(content, dict) or not content.get("video_url"):
        raise RuntimeError("Successful task has no video URL. Task ID retained for resume.")
    _download(manifest, shot_id, provider, content["video_url"])


def _submit(manifest, config, shot, provider, stop):
    image_key, image_url = image_reference(manifest.root, shot.image)
    if image_key != shot.image_key:
        raise ValueError("First frame changed since estimate. Run again.")
    price = config.prices[shot.model]
    manifest.record(shot.shot_id, new=True, state="submitting", signature=shot.signature,
                    model=shot.model, price=price.usd_per_million, verified=price.verified,
                    base_url=config.base_url)
    if stop.is_set():
        manifest.discard_unsent_intent(shot.shot_id)
        raise RuntimeError("Interrupted before submission.")
    try:
        task_id = provider.submit(shot, image_url)
    except Exception as exc:
        manifest.record(shot.shot_id, state="needs_reconcile")
        # Only ProviderError is known to omit signed URLs, bodies, and credentials.
        reason = f"{exc} " if isinstance(exc, ProviderError) else ""
        raise RuntimeError(f"{reason}Submission outcome uncertain. "
                           f"Use adshot run --reconcile {shot.shot_id}.") from None
    return manifest.record(shot.shot_id, state="running", task_id=task_id)


def _execute(manifest, config, item, resolution, stop):
    shot = item.shot
    shot_id = shot.shot_id
    with manifest.claim(shot_id) as acquired:
        if not acquired:
            return "claimed by another runner"
        attempts = manifest.snapshot().get(shot_id, [])
        current = quote(manifest.root, config, shot, attempts)
        if current.action == "skip":
            return "done (skipped)"
        if current.attempts != item.attempts:
            return "updated by another runner; check status"
        if current.action == "blocked" and not resolution:
            raise ValueError(f"Check provider task history, then use adshot run --reconcile {shot_id}.")
        if current.action == "publish":
            _publish(manifest, shot_id, attempts[-1])
            return "done (recovered download)"
        if current.action == "busy":
            raise ValueError(f"Submission outcome is unknown. Use adshot run --reconcile {shot_id}.")

        submit = current.action == "submit" or (current.action == "blocked" and item.action == "submit")
        if submit and item.action != "submit":
            return "state changed since estimate; run again for a fresh quote"
        if not submit and attempts[-1]["base_url"] != config.base_url:
            raise ValueError("Restore the original base_url and its API key to resume this task.")
        with manifest.slot(config.concurrency, stop) as active:
            if not active:
                return "interrupted"
            provider = VideoProvider(config.base_url)
            provider.check_credentials()
            response = None
            if submit:
                if stop.is_set():
                    return "interrupted before submission"
                attempt = _submit(manifest, config, shot, provider, stop)
            elif current.action == "blocked":
                # A mistyped ID must leave reconciliation available.
                response = provider.poll(resolution)
                if not isinstance(response.get("status"), str) or not response.get("status"):
                    raise ValueError("Unrecognized task response. Check the ID and reconcile again.")
                if "model" in response and response["model"] != attempts[-1].get("model", shot.model):
                    raise ValueError("Task model differs from the model used to submit this shot.")
                attempt = manifest.record(shot_id, reconcile=True, state="running", task_id=resolution)
            else:
                attempt = attempts[-1]
            _poll(manifest, shot_id, attempt, provider, stop, response)
    if attempt["signature"] != shot.signature:
        return "recovered previous version; run again to price your edits"
    return "done"


def abandon(manifest, shot_id, confirm):
    with manifest.claim(shot_id) as acquired:
        if not acquired:
            raise ValueError(f"{shot_id} is claimed by another runner.")
        attempts = manifest.snapshot().get(shot_id, [])
        state = attempts[-1]["state"] if attempts else "pending"
        video = manifest.root / "shots" / f"{shot_id}.mp4"
        if state == "done" and video.is_file() and video.stat().st_size:
            raise ValueError(f"{shot_id} already has a downloaded video.")
        if state not in ("running", "done"):
            raise ValueError(f"{shot_id} has no saved task awaiting recovery.")
        if confirm is None:
            confirm = input(f"Abandon {shot_id} locally and allow a future run to charge again? "
                            "Type RETRY: ")
        if confirm != "RETRY":
            raise ValueError("Abandon cancelled. Type RETRY to confirm.")
        manifest.record(shot_id, state="failed")
    print(f"{shot_id}: marked failed. Run adshot estimate to review a new submission.")


def run(manifest, config, quotes, *, budget, yes, reconcile, accept_unverified_prices=False):
    total = show_estimate(quotes)
    resolution = None
    if reconcile:
        item, = quotes
        if item.state != "needs_reconcile":
            raise ValueError(f"{reconcile} has no uncertain submission to reconcile.")
        print("Check task history at the original provider. Attach the accepted task ID. "
              "Authorize a fresh submission only after checking for an existing charge.")
        answer = input(f"Task ID, or type RETRY {reconcile} to allow another charge: ").strip()
        if answer == f"RETRY {reconcile}":
            resolution = "retry"
            price = config.prices[item.shot.model]
            tokens = token_estimate(item.shot)
            quotes = [replace(item, action="submit", tokens=tokens,
                              usd=token_cost(tokens, price.usd_per_million))]
            total = show_estimate(quotes)
        elif re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", answer):
            resolution = answer
        else:
            raise ValueError("Reconciliation cancelled. The shot remains protected.")
    cap = Decimal(str(budget))
    if not cap.is_finite() or cap < 0:
        raise ValueError("Budget must be a finite, nonnegative USD amount.")
    if total > cap:
        raise ValueError(f"Estimated ${total:.4f} exceeds budget ${cap:.4f}.")
    unverified = [item.shot.shot_id for item in quotes if item.action == "submit" and not item.verified]
    if unverified and not accept_unverified_prices:
        raise ValueError(f"The price for {', '.join(unverified)} is unverified at this endpoint. "
                         "Actual charges may differ. Review the rates and pass --accept-unverified-prices.")
    if not yes:
        answer = input(f"Proceed with this run (estimated ${total:.4f})? [y/N] ")
        if answer.lower().strip() not in ("y", "yes"):
            print("Run cancelled.")
            return 1

    stop = Event()

    def process(item):
        try:
            return True, _execute(manifest, config, item, resolution, stop)
        except KeyboardInterrupt:
            stop.set()
            raise
        except (ValueError, RuntimeError, OSError) as exc:
            return False, str(exc)

    ok = True
    pool = ThreadPoolExecutor(max_workers=config.concurrency)
    try:
        futures = [pool.submit(process, item) for item in quotes]
        for item, future in zip(quotes, futures):
            # A bounded wait lets the main thread handle Ctrl+C on Windows.
            while not future.done():
                wait([future], timeout=0.2)
            success, message = future.result()
            print(f"{item.shot.shot_id}: {message}", flush=True)
            ok = ok and success
    except KeyboardInterrupt:
        stop.set()
        raise
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return 0 if ok else 1
