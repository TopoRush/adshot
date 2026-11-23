# adshot

[![Six product ads made with adshot: skincare, jewelry, coffee, pet, candle, earbuds](docs/examples.jpg)](https://toporush.com/adshot/#work)

*Six 16-second ads made with adshot, about $1.25 of model cost each. [Watch them](https://toporush.com/adshot/#work). Brands are fictional.*

Turn a product photograph and a brief into a shot list, see the estimated video cost, and render a short ad. Each row in the storyboard has a stable ID. Edit the camera move in S03 and the next run prices and renders S03 while reusing the other shots.

**See it in action:** a real Claude Code session that turns one product photo into a 16-second ad for $1.58 in model cost, including one redo: <https://toporush.com/adshot/>

**Get a key:** adshot defaults to [OpenLux](https://www.openlux.ai), one key for Seedance and other video models. Any compatible endpoint works; see [Providers, models, and prices](#providers-models-and-prices).

## Try it offline

Run these commands from the repository with Python 3.10 or newer.

```sh
pip install -e .
adshot init demo
adshot estimate
cd demo
adshot status
```

The starter project contains four 4-second perfume shots, totaling 16 seconds. With the default Fast model, the example estimates **345,600 tokens and $1.2476** at the verified OpenLux rate of $3.61 per million tokens. You can edit the CSV and repeat the estimate without a key. Running `estimate` in the parent directory selects a project only when exactly one visible child contains an `adshot.toml`. Run all production commands inside the project. [Perfume storyboard](examples/perfume/storyboard.csv)

To use the agent skill, point your coding agent at this repository's `SKILL.md` and give it the brief and image path. The wheel also installs the skill under `share/adshot/SKILL.md` in the Python environment.

## Use it from an MCP client

adshot also runs as an MCP server, so Claude Desktop, Cursor, and other MCP clients can make the ad without a terminal. Add this to the client's MCP configuration:

```json
{
  "mcpServers": {
    "adshot": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/TopoRush/adshot", "adshot", "mcp"],
      "env": { "ADSHOT_API_KEY": "your video provider key" }
    }
  }
}
```

The server offers seven tools: `init_project`, `get_storyboard`, `set_storyboard`, `estimate`, `render`, `status`, and `stitch`. They run the same commands described below, on a project folder you name by absolute path.

- `estimate` and `set_storyboard` return the price and spend nothing. They work without a key.
- `render` takes `approved_usd` and refuses to start if the current estimate is higher. Pass the total the user agreed to. It can never exceed `budget_usd` in `adshot.toml`, and no tool edits that file.
- `render` returns at once and works in the background. `status` shows progress. If the client closes mid-render, the saved task IDs let the next `render` resume the same jobs. A shot interrupted in the instant of submission is held as `needs_reconcile` and is not sent again.
- Resolving an uncertain submission (`run --reconcile`) and `abandon` stay on the command line, because each needs a person to check the provider's task history first.

Stitching needs FFmpeg on PATH, as in the command-line flow.

<!-- mcp-name: io.github.TopoRush/adshot -->

## Make an ad

1. Put a product image in the project. Write the rows yourself or have Claude Code, Codex, or OpenCode follow the agent instructions. For a reference ad, inspect the cuts and movement, then describe the shot sequence in the CSV. Use a product photo or an extracted still as the first frame where appropriate. [Agent skill](SKILL.md)
2. Optionally, use the vision planner below. It sends the image and brief to the configured chat endpoint and replaces `storyboard.csv` after validating the reply. Save a copy first if you want to keep an earlier draft. The chat provider bills this call separately from the video estimate.
3. Review the estimate and the rates in `adshot.toml`. Provide `ADSHOT_API_KEY` through your shell environment or secret manager when ready to render.
4. Review the MP4s in `shots/`. Edit the rows that need a new take, estimate again, and stitch the approved shots.

```sh
adshot plan --image product.jpg --brief "A warm, understated launch film for an amber perfume." --shots 4 --seconds 16
adshot estimate
adshot run --budget 5
adshot status

# After editing the two rows
adshot estimate --only S03,S04
adshot run --only S03,S04
adshot stitch --out ad.mp4
```

`run` prints a quote and asks for confirmation. Use `--yes` after approving the quoted spend in an agent session. `--budget` overrides the project cap for this invocation. The cap applies to estimated **new video submissions** in that invocation. Provider rounding, adaptive dimensions, actual token usage, and unverified prices can affect the invoice.

If any new submission uses an unverified rate, review that rate and add `--accept-unverified-prices`. This approval is required even with `--yes`. Resuming or skipping a shot adds zero new spend.

Stitching requires an FFmpeg executable on PATH with H.264 and AAC encoders. It fits each shot onto the first shot's canvas, preserves proportions, pads silent sections and exports a 24 fps MP4 in storyboard order. An encoder error preserves the previous export.

## Storyboard fields

Use UTF-8 CSV and quote any field containing commas, newlines, or quotation marks.

| Column | Meaning |
| --- | --- |
| `shot_id` | Stable, unique file stem such as `S03`. Use letters, digits, underscores, and hyphens. |
| `prompt` | A visible action, camera movement, and product details to preserve. |
| `seconds` | Positive whole seconds of output. Seedance accepts 4–15 seconds per shot; the planner drafts 4–6. |
| `image` | Optional local JPEG, PNG, WebP, GIF, or HTTPS first-frame URL. Relative paths resolve from the project. |
| `model`, `resolution`, `ratio` | Optional per-shot overrides. Empty fields inherit the project settings. |
| `notes` | Editorial notes. Changing notes or reordering rows adds no render cost. |

The signature covers the prompt, duration, first-frame content hash or URL, effective model, resolution, and ratio. Use a new URL when replacing the contents of a remote image at the same address. A successful revision moves the old MP4 to `shots/S03.v1.mp4`, then `.v2.mp4` and so on.

## Providers, models, and prices

adshot defaults to OpenLux because one key covers several Chinese video models. The maintainers may receive a referral share from OpenLux for accounts opened through links in this repository. This never changes adshot's pricing logic or behavior, and any compatible provider works with one config line.

With the generated `base_url = ""`, changing one line to `provider = "ark"` selects Volcano Ark official. Alternatively, set `base_url` to your compatible service's HTTPS origin. Video and planner base URLs allow HTTP only on `localhost` and `127.0.0.1` for local testing. The video client appends `/api/v3/contents/generations/tasks`, reads `ADSHOT_API_KEY` from the environment, and uses Bearer authentication. Generation uses silent video and disables the watermark flag.

| Provider | Default API origin |
| --- | --- |
| OpenLux | `https://api.openlux.ai` |
| Volcano Ark | `https://ark.cn-beijing.volces.com` |

| Model ID | Use | Default USD per million tokens |
| --- | --- | ---: |
| `doubao-seedance-2-0-mini-260615` | Draft, up to 720p | 2.41, measured on OpenLux |
| `doubao-seedance-2-0-fast-260128` | Default, up to 720p | 3.61, measured on OpenLux |
| `doubao-seedance-2-0-260128` | Standard, supports 1080p | 8.00, unverified placeholder |
| `doubao-seedance-2-5-260628` | Pro, first-frame shots require `ratio = "adaptive"` | 12.00, unverified placeholder |

Choose a model with the `model` line in `adshot.toml`. Each price entry has an editable `usd_per_million` and `verified` flag. Set the entry's `base_url` to the endpoint whose rate you confirmed. A provider change makes the shipped OpenLux measurement unverified at the new endpoint. For a custom model, add a price entry under its exact model ID.

The video estimate uses `(input_video_seconds + output_seconds) × width × height × 24 / 1024`. Text and still-image inputs have zero input video seconds. The reference-ad workflow uses the source as creative guidance. Adaptive estimates use the larger supported pixel area for the selected resolution.

On November 19, 2026, an OpenLux Mini text-only call at 480p, 9:16, and four seconds reported **40,594 tokens**, cost **$0.098**, and took about **140 seconds**. Using the 496 × 864 pixel bucket, adshot estimates **40,176 tokens** and **$0.0968**.

On November 19, 2026, four OpenLux Fast shots at 720p and 9:16, each four seconds long with a first-frame image, used **87,300 tokens per shot**. The **$1.26** total charge gives a rounded rate of **$3.61 per million tokens**.

The planner uses `plan_model` (default `gpt-4o-mini`). Its default `plan_base_url` is `{base_url}/v1` for OpenLux and `{base_url}/api/v3` for Ark. Set `plan_base_url` explicitly to use another vision-capable chat service. The planner appends `/chat/completions` and reads `ADSHOT_PLAN_KEY` first, then `ADSHOT_API_KEY`. The video client reads only `ADSHOT_API_KEY`.

## Resume and reconcile

Keep `manifest.json`, `.adshot/`, and `shots/` with the project. Use a local filesystem shared by the runners. Each shot has an exclusive claim, and the manifest records submission intent before the paid POST. The provider task ID is the next durable update after a successful response. Different processes can work on the same project with the configured concurrency limit.

| Status | What the next run does |
| --- | --- |
| `pending` | Quotes a new submission. |
| `done` | Skips the matching downloaded video at zero new cost. |
| `changed` | Quotes a replacement and keeps the previous video until the new download is ready. |
| `running` | Resumes the saved task or yields to its active owner. A missing local download with a saved ID can be recovered the same way. |
| `needs_reconcile` | Holds the shot for a provider-history check. |
| `failed` | The provider reported failure, cancellation, or expiry, or you abandoned the attempt. A subsequent run can quote a new attempt. |

After an interrupted poll or download, run `adshot run` again. It polls the original task and downloads the temporary video URL immediately on success. If the brief changed while a task was running, adshot recovers that version first. The next estimate prices your edit. Restore the original endpoint and its key before resuming a task from a different provider.

Ctrl+C stops new submissions. Requests already in flight can still incur a charge, and adshot saves any task IDs they return. Task polling retries network errors and HTTP 5xx after 1, 3, and 9 seconds. Each paid POST is sent once.

On a polling timeout, adshot prints the last provider status and keeps the task ID. Check the task in provider history. If it cannot finish, or if a completed shot's local file is missing and its download URL has expired, run `adshot abandon S03` and type `RETRY` (or use `--confirm RETRY`). This marks the attempt failed locally and preserves its task ID and recorded usage. The provider may still bill the original task. Review `adshot estimate --only S03` before submitting a replacement.

If a submit might have reached the provider but the receipt was lost, check the provider's task history and reconcile the affected shot.

```sh
adshot run --reconcile S03
```

Paste the matching task ID to recover it with zero new submissions. If you have checked the provider's history and authorize another charge, type `RETRY S03` at the prompt. adshot shows the new quote and enforces the cap. Even `--yes` requires an explicit reconciliation decision. If the provider history is inconclusive, leave the shot on hold.

`status` calculates spend from provider-reported usage and the rate saved for each attempt. It retains earlier revisions, marks unverified rates, and shows `+ unknown` when the provider has supplied no token count. The provider's billing record is authoritative.

## Development

Install pytest in your development environment, then run `pytest`. HTTP tests use mocked responses. The process tests terminate an actual worker in the uncertain submission window and run competing workers against a local receipt store. Installing FFmpeg enables an additional offline encode/decode test.

Licensed under the Apache License, Version 2.0. [License text](LICENSE)
