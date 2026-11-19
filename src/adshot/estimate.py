"""Quote incremental spend and calculate costs from reported usage."""

from dataclasses import dataclass
from decimal import Decimal

from .config import PRO
from .storyboard import Shot


def token_estimate(shot):
    # Ark's nominal resolutions are pixel buckets; e.g. portrait 480p is
    # 496 x 864 for Seedance 2.0. Adaptive uses the larger supported area.
    landscape, square = {
        "480p": (496 * 864, 640 * 640),
        "720p": (1280 * 720, 960 * 960),
        "1080p": (1920 * 1080, 1440 * 1440),
    }[shot.resolution]
    if shot.model == PRO and shot.resolution == "480p":
        landscape = 480 * 854
    area = landscape
    if shot.ratio == "1:1":
        area = square
    elif shot.ratio == "adaptive":
        area = max(landscape, square)
    return round(shot.seconds * area * 24 / 1024)


def token_cost(tokens, price):
    return Decimal(tokens) * Decimal(str(price)) / 1_000_000


@dataclass(frozen=True)
class Quote:
    shot: Shot
    state: str
    action: str
    attempts: int
    tokens: int
    usd: Decimal
    verified: bool


def quote(root, config, shot, attempts):
    state, action = "pending", "submit"
    if attempts:
        last = attempts[-1]
        state = last["state"]
        path = root / "shots" / f"{shot.shot_id}.mp4"
        if state == "failed":
            # A failed replacement leaves the last published video in place.
            published = next((a for a in reversed(attempts) if a["state"] == "done"), None)
            if (published and published["signature"] == shot.signature
                    and path.is_file() and path.stat().st_size):
                last, state = published, "done"
        if state == "done":
            if last["signature"] != shot.signature:
                state, action = "changed", "submit"
            elif path.is_file() and path.stat().st_size:
                action = "skip"
            else:
                state, action = "running", "resume"
        elif state == "submitting":
            state, action = "running", "busy"
        elif state == "needs_reconcile":
            action = "blocked"
        elif state == "publishing":
            state, action = "running", "publish"
        elif state == "running":
            action = "resume"
    price = config.prices[shot.model]
    tokens = token_estimate(shot) if action == "submit" else 0
    return Quote(shot, state, action, len(attempts), tokens,
                 token_cost(tokens, price.usd_per_million), price.verified)


def show_estimate(quotes):
    print("SHOT       STATE             ACTION      NEW TOKENS    NEW USD  MODEL")
    for item in quotes:
        flag = "" if item.verified else " [unverified price]"
        print(f"{item.shot.shot_id:<10} {item.state:<17} {item.action:<10} "
              f"{item.tokens:>10}  ${item.usd:.4f}  {item.shot.model}{flag}")
    total = sum((item.usd for item in quotes), start=Decimal(0))
    print(f"Total new spend: ${total:.4f}", flush=True)
    return total


def spent(attempts):
    total = Decimal(0)
    unknown = False
    for attempt in attempts:
        if attempt.get("tokens") is None:
            unknown = True
        else:
            total += token_cost(attempt["tokens"], attempt["price"])
    return f"${total:.4f}" + (" + unknown" if unknown else "")
