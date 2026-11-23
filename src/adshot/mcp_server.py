"""MCP server: drive an adshot project from any MCP client.

Every tool runs the adshot command line in a child process, so the server
shares the CLI's quoting, budget checks, and no-double-charge manifest.
Rendering starts in the background because a shot takes minutes; the saved
task IDs let a later call, or a later server, resume the same jobs.
"""

import csv
import io
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

try:
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ModuleNotFoundError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer
    from mcp.server.fastmcp.exceptions import ToolError

from .config import load_config
from .storyboard import FIELDS

INSTRUCTIONS = """adshot turns a product photo and a brief into a short ad, one shot at a time.

Order of work: init_project -> set_storyboard -> estimate -> show the user the
total and wait for their approval -> render(approved_usd=that total) -> status
until every shot is done -> stitch.

Rules:
- Never call render before the user has approved the estimate you showed them.
- To redo a shot, change only its row with set_storyboard, then estimate and
  render again. Unchanged finished shots are skipped and cost nothing.
- The video key is read from the ADSHOT_API_KEY environment variable of this
  server. Never ask the user to paste a key into the conversation.
"""

server = MCPServer("adshot", instructions=INSTRUCTIONS)



class AdshotError(ToolError, ValueError):
    """A message meant for the agent; the client shows it instead of a crash notice."""


LOG_NAME = "mcp-render.log"
_renders: dict[str, subprocess.Popen] = {}
_lock = threading.RLock()  # mcp 2.x runs tools on worker threads


def _project(project_dir: str, *, must_exist: bool = True) -> Path:
    root = Path(project_dir).expanduser()
    if not root.is_absolute():
        raise AdshotError("project_dir must be an absolute path.")
    root = root.resolve()
    if must_exist and not (root / "adshot.toml").is_file():
        raise AdshotError(f"No adshot.toml in {root}. Call init_project first.")
    return root


def _command(*args: str) -> list[str]:
    return [sys.executable, "-m", "adshot", *args]


def _environment() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def _cli(root: Path, *args: str) -> str:
    done = subprocess.run(_command(*args), cwd=root, env=_environment(), text=True,
                          encoding="utf-8", errors="replace", capture_output=True,
                          stdin=subprocess.DEVNULL, timeout=600)
    output = (done.stdout + done.stderr).strip()
    if done.returncode:
        raise AdshotError(output or f"adshot exited with status {done.returncode}.")
    return output


def _render_state(root: Path) -> str:
    process = _renders.get(str(root))
    if process is None:
        return "no render started by this server"
    code = process.poll()
    if code is None:
        return "render in progress"
    return "last render finished" if code == 0 else f"last render ended with errors (exit {code})"


def _log_tail(root: Path, lines: int = 20) -> str:
    path = root / ".adshot" / LOG_NAME
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace")
    return "\n".join(text.splitlines()[-lines:])


def _replace(path: Path, data: bytes) -> None:
    stage = path.with_name(path.name + ".part")
    stage.write_bytes(data)
    os.replace(stage, path)


def _spawn(root: Path, args: list[str]) -> subprocess.Popen:
    private = root / ".adshot"
    private.mkdir(exist_ok=True)
    with (private / LOG_NAME).open("a", encoding="utf-8") as log:
        log.write(time.strftime("\n== render %Y-%m-%d %H:%M:%S ==\n"))
        log.flush()
        return subprocess.Popen(_command(*args), cwd=root, env=_environment(),
                                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)


@server.tool()
def init_project(project_dir: str) -> str:
    """Create a new adshot project folder with a starter storyboard and settings.

    project_dir: absolute path of a folder that does not hold an adshot project yet.
    Costs nothing and needs no key.
    """
    root = _project(project_dir, must_exist=False)
    root.parent.mkdir(parents=True, exist_ok=True)
    _cli(root.parent, "init", str(root))
    return (f"Created {root}. It holds adshot.toml (provider, model, prices, budget) and a "
            "four-shot starter storyboard. Put the product image in this folder, then call "
            "set_storyboard.")


@server.tool()
def get_storyboard(project_dir: str) -> list[dict]:
    """Return the storyboard rows of a project, in cut order."""
    root = _project(project_dir)
    with (root / "storyboard.csv").open(encoding="utf-8-sig", newline="") as stream:
        return [dict(row) for row in csv.DictReader(stream)]


@server.tool()
def set_storyboard(project_dir: str, shots: list[dict]) -> str:
    """Replace the storyboard, then price it. Nothing is rendered or charged.

    shots: one object per shot, in cut order. Keys:
      shot_id  required, stable name such as "S01". Keep IDs when revising.
      prompt   required. One visible action, the framing, and one camera move.
      seconds  required, whole seconds. Seedance accepts 4 to 15 per shot.
      image    optional first frame: a file name inside the project, or an HTTPS URL.
      model, resolution, ratio   optional overrides of the project settings.
      notes    optional editorial notes. Changing notes costs nothing.
    Send every shot each time. A row you leave unchanged keeps its finished video.
    If a row is invalid the previous storyboard is restored and the error is returned.
    """
    root = _project(project_dir)
    if not shots:
        raise AdshotError("shots must contain at least one shot.")
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=FIELDS)
    writer.writeheader()
    for index, shot in enumerate(shots, start=1):
        if not isinstance(shot, dict):
            raise AdshotError(f"Shot {index} must be an object.")
        unknown = set(shot).difference(FIELDS)
        if unknown:
            raise AdshotError(f"Shot {index} has unknown keys: {', '.join(sorted(unknown))}.")
        writer.writerow({name: "" if shot.get(name) is None else str(shot[name])
                         for name in FIELDS})
    path = root / "storyboard.csv"
    with _lock:
        previous = path.read_bytes() if path.is_file() else None
        _replace(path, buffer.getvalue().encode("utf-8"))
        try:
            table = _cli(root, "estimate")
        except BaseException:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                _replace(path, previous)
            raise
    return f"Saved {len(shots)} shots.\n{table}"


@server.tool()
def estimate(project_dir: str, only: str = "") -> str:
    """Price the storyboard without spending anything.

    only: optional comma-separated shot IDs, for example "S03,S04".
    Returns one line per shot (state, action, new tokens, new USD, model) and
    "Total new spend". Shots already rendered and unchanged show action "skip" and $0.
    Show the total to the user and get their approval before calling render.
    """
    root = _project(project_dir)
    args = ["estimate"] + (["--only", only] if only else [])
    return _cli(root, *args)


@server.tool()
def render(project_dir: str, approved_usd: float, only: str = "",
           accept_unverified_prices: bool = False) -> str:
    """Start rendering in the background. This spends money at the video provider.

    approved_usd: the amount the user approved after seeing estimate. The run is
      refused if the current estimate is higher, so pass the "Total new spend" figure
      the user agreed to, not a larger round number. It cannot exceed budget_usd in
      the project's adshot.toml; only the user raises that cap, by editing the file.
    only: optional comma-separated shot IDs. Use the same IDs you estimated.
    accept_unverified_prices: set true only if the user accepted a rate marked
      "[unverified price]" in the estimate.
    Returns at once. Call status every 30 to 60 seconds until no shot is running.
    A shot that was already submitted is resumed, never submitted twice.
    Needs ADSHOT_API_KEY in this server's environment.
    """
    root = _project(project_dir)
    if not os.environ.get("ADSHOT_API_KEY", "").strip():
        raise AdshotError("ADSHOT_API_KEY is not set in the MCP server's environment. "
                         "Add it to the server entry in your MCP client configuration.")
    limit = load_config(root).budget_usd
    if not approved_usd <= limit:
        raise AdshotError(f"approved_usd {approved_usd} is above budget_usd {limit} in adshot.toml. "
                          "Only the user can raise that cap, by editing the file.")
    selection = ["--only", only] if only else []
    # The estimate prints four decimals; allow for that rounding so the figure
    # the user saw is accepted as the cap.
    cap = float(approved_usd) + 0.00005
    args = ["run", "--yes", "--budget", f"{cap:.6f}", *selection]
    if accept_unverified_prices:
        args.append("--accept-unverified-prices")
    with _lock:
        if _render_state(root) == "render in progress":
            raise AdshotError("A render is already in progress for this project. Call status.")
        quote = _cli(root, "estimate", *selection)
        process = _spawn(root, args)
        _renders[str(root)] = process
    for _ in range(10):  # a refused run exits within moments; report it now
        if process.poll() is not None:
            break
        time.sleep(0.5)
    if process.poll() not in (None, 0):
        raise AdshotError(_log_tail(root) or "The render could not start.")
    return f"Render started.\n{quote}\nCall status to follow progress."


@server.tool()
def status(project_dir: str) -> str:
    """Show each shot's state, the spend so far, and the progress of the last render.

    States: pending (never rendered), running, done, changed (edited since its
    last render), failed, needs_reconcile (submission outcome unknown; use the
    adshot command line to resolve it, so the shot is not charged twice).
    Finished videos are in the project's shots/ folder as <shot_id>.mp4.
    """
    root = _project(project_dir)
    parts = [_cli(root, "status"), f"Render: {_render_state(root)}"]
    tail = _log_tail(root)
    if tail:
        parts.append("Last render output:\n" + tail)
    return "\n\n".join(parts)


@server.tool()
def stitch(project_dir: str, out: str = "ad.mp4") -> str:
    """Join the finished shots, in storyboard order, into one MP4. Costs nothing.

    out: output file name inside the project.
    Every shot must be done. Needs FFmpeg on PATH with H.264 and AAC encoders.
    """
    root = _project(project_dir)
    name = Path(out)
    if name.name != out or name.suffix.lower() != ".mp4":
        raise AdshotError("out must be an .mp4 file name inside the project.")
    return _cli(root, "stitch", "--out", out)


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
