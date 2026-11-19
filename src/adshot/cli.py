"""Command-line entry point for an adshot project."""

import argparse
import csv
import sys
from pathlib import Path

from .config import init_project, load_config
from .estimate import quote, show_estimate, spent
from .manifest import Manifest
from .plan import plan
from .runner import abandon, run
from .stitch import stitch
from .storyboard import read_storyboard


def parser():
    cli = argparse.ArgumentParser(prog="adshot", description="Render product ads shot by shot.")
    commands = cli.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init", help="Create an offline-ready project")
    init.add_argument("dir", type=Path)
    planner = commands.add_parser("plan", help="Draft a storyboard with a vision chat model")
    planner.add_argument("--image", required=True)
    planner.add_argument("--brief", required=True)
    planner.add_argument("--shots", type=int, default=4)
    planner.add_argument("--seconds", type=int, default=16)
    estimate = commands.add_parser("estimate", help="Price the storyboard offline")
    estimate.add_argument("--only", help="Comma-separated shot IDs")
    render = commands.add_parser("run", help="Price and render the selected shots")
    group = render.add_mutually_exclusive_group()
    group.add_argument("--only", help="Comma-separated shot IDs")
    group.add_argument("--reconcile", metavar="SHOT_ID", help="Resolve an uncertain submission")
    render.add_argument("--yes", action="store_true", help="Approve the displayed video estimate")
    render.add_argument("--accept-unverified-prices", action="store_true",
                        help="Allow new submissions at rates unverified for this endpoint")
    render.add_argument("--budget", type=float, metavar="USD", help="Override the per-run cap")
    commands.add_parser("status", help="Show shot states and usage-based spend")
    drop = commands.add_parser("abandon", help="Mark an unrecoverable task failed for a fresh quote")
    drop.add_argument("shot_id", metavar="SHOT_ID")
    drop.add_argument("--confirm", metavar="RETRY", help="Confirm that a future run may charge again")
    join = commands.add_parser("stitch", help="Join the current shots in storyboard order")
    join.add_argument("--out", type=Path, default=Path("ad.mp4"))
    return cli


def _dispatch(args):
    root = Path.cwd()
    if args.command == "init":
        init_project(args.dir)
        print(f"Created {args.dir.resolve()}. Run adshot estimate from that directory.")
        return 0
    if args.command == "estimate" and not (root / "adshot.toml").is_file():
        children = [p for p in root.iterdir() if not p.name.startswith((".", "_"))
                    and p.is_dir() and (p / "adshot.toml").is_file()]
        if len(children) == 1:
            root = children[0]
            print(f"Project: {root}")
        elif len(children) > 1:
            raise ValueError("Several adshot projects found. Change into the one to estimate.")
    config = load_config(root)
    if args.command == "plan":
        plan(root, config, args.image, args.brief, args.shots, args.seconds)
        print(f"Wrote storyboard.csv with {args.shots} shots totaling {args.seconds} seconds.")
        return 0

    shots = read_storyboard(root, config)
    if args.command == "abandon":
        if args.shot_id not in {shot.shot_id for shot in shots}:
            raise ValueError(f"Unknown shot ID: {args.shot_id!r}.")
        abandon(Manifest(root), args.shot_id, args.confirm)
        return 0
    if args.command == "stitch":
        stitch(root, [shot.shot_id for shot in shots], args.out)
        print(f"Wrote {args.out.resolve()}")
        return 0

    reconcile = getattr(args, "reconcile", None)
    selection = getattr(args, "only", None)
    if selection is not None or reconcile is not None:
        wanted = {reconcile} if reconcile is not None else {s.strip() for s in selection.split(",")}
        unknown = wanted.difference(shot.shot_id for shot in shots)
        if unknown:
            raise ValueError(f"Unknown shot ID(s): {', '.join(sorted(unknown))!r}.")
        shots = [shot for shot in shots if shot.shot_id in wanted]
    manifest = Manifest(root)
    history = manifest.recover(shot.shot_id for shot in shots)
    quotes = [quote(root, config, shot, history.get(shot.shot_id, [])) for shot in shots]
    if args.command == "estimate":
        show_estimate(quotes)
    elif args.command == "run":
        budget = config.budget_usd if args.budget is None else args.budget
        return run(manifest, config, quotes, budget=budget, yes=args.yes, reconcile=reconcile,
                   accept_unverified_prices=args.accept_unverified_prices)
    elif args.command == "status":
        print("SHOT       STATE             SPENT USD (usage x saved rate)    TASK ID")
        for item in quotes:
            shot_id = item.shot.shot_id
            attempts = history.get(shot_id, [])
            latest = attempts[-1] if attempts else {}
            if item.state == "done":
                latest = next(a for a in reversed(attempts) if a["state"] == "done")
            task_id = latest.get("task_id", "-")
            amount = spent(attempts)
            if any(not a.get("verified") for a in attempts):
                amount += " [unverified price]"
            print(f"{shot_id:<10} {item.state:<17} {amount:<33} {task_id}")
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        return _dispatch(args)
    except (ValueError, OSError, RuntimeError, csv.Error) as exc:
        print(f"adshot: {exc}", file=sys.stderr)
    except EOFError:
        print("adshot: Confirmation requires interactive input.", file=sys.stderr)
    except KeyboardInterrupt:
        print("adshot: Interrupted. Use adshot status to inspect saved tasks.", file=sys.stderr)
        return 130
    return 1


if __name__ == "__main__":
    sys.exit(main())
