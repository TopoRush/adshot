---
name: adshot
description: Make or revise a short product ad from a product image, brief, or reference ad using adshot. Draft a CSV storyboard, quote the video cost, render approved shots, and stitch the reviewed cuts.
---

# Produce a shot-by-shot ad

1. **Inspect the source.** Read the brief and view the product image. Identify the pack shape, finish, label position, cap, and scale cues that must survive every cut. For a reference ad, inspect its pacing, framing, and transitions. Translate those observations into shot directions. The video endpoint accepts a still first frame.
2. **Set up the project.** For a new project, run `adshot init <dir>` and enter that directory. For a revision, open its existing `storyboard.csv`, `adshot.toml`, and `adshot status`. Keep the existing shot IDs. Read the provider, model, resolution, rate verification flags, and per-run budget.
3. **Write the storyboard.** Use the requested shot count and total duration. Four shots totaling 16 seconds are a practical default. Seedance accepts 4–15 whole seconds per shot; use 4–5 seconds for ad pacing and check the sum. Write UTF-8 CSV with the header below. Use a CSV writer for prompts containing commas. An agent can draft every row directly. If the user has authorized external chat planning, `adshot plan --image product.jpg --brief "..." --shots 4 --seconds 16` can generate a draft and replace the project CSV.

   ```csv
   shot_id,prompt,seconds,image,model,resolution,ratio,notes
   ```

   Give each shot one legible action. Specify framing, light direction, and one camera move or hold. For example, “Track along the beveled shoulder as a narrow reflection crosses the amber glass.” Anchor each product shot to the same bottle shape, cap, and label placement. Use a matching project image or HTTPS URL in `image` when a first frame helps. Keep captions, prices, and headlines in editorial notes for later compositing. Avoid asking the model to generate lettering. Leave optional overrides blank unless the shot needs them.

4. **Get approval for the cost.** Run `adshot estimate`. Tell the user which shots will submit, which will resume or skip, the total new USD, and which rates are unverified. Ask them to confirm that estimate. If it exceeds the budget, revise the model, duration, resolution, or selected shots with the user and estimate again.
5. **Render the approved selection.** Run `adshot run --yes` inside the project. For a partial approval, use the same IDs with both `adshot estimate --only S03,S04` and `adshot run --only S03,S04 --yes`. If a new submission uses an unverified rate, include `--accept-unverified-prices` once the user explicitly accepts that rate. Supply a user-approved cap with `--budget` when applicable. Read keys from the environment and keep them out of messages, files, and command arguments.
6. **Review and revise.** Open each completed MP4. Check silhouette and label placement against the source, motion continuity across the cut, framing, and whether the intended action finishes within the allotted time. Edit only the rows with a specific defect. Run `adshot estimate` and `adshot run` again with approval for the changed spend. Prior successful takes remain in `shots/` as numbered versions.
7. **Stitch and inspect.** Once `adshot status` shows the current rows as done, run `adshot stitch --out ad.mp4`. Play the export and check the opening, cut order, total running time, and closing hold. Return the path to the final MP4 and identify any shot still awaiting review.

## Recovery

- **Missing key or unsupported settings.** Ask the user to set `ADSHOT_API_KEY` in the environment. For a separate chat account use `ADSHOT_PLAN_KEY`. Mini and Fast support up to 720p. Pro with a first frame needs `adaptive`. Re-estimate after a settings change.
- **Interrupted run with a task ID.** Re-run the command to poll and download the existing task. Keep the original provider selected until recovery finishes.
- **Unrecoverable task or expired download.** Check the last polling status and provider history. If the task cannot finish, or a completed shot has lost both its local video and its usable download URL, ask the user to confirm abandoning it. Run `adshot abandon S03 --confirm RETRY` with that confirmation, then quote the replacement.
- **Uncertain submission.** Show the affected shot and ask the user to check provider history. Use `adshot run --reconcile S03` with the confirmed matching ID. A fresh charge requires the user's explicit decision to type `RETRY S03`. Leave the shot on hold if the outcome is unclear.
- **Terminal provider failure.** Inspect the task in the provider dashboard, correct the row if needed, and quote a new attempt. Preserve the manifest and prior MP4s.
- **Stitch failure.** Verify that every row is done and FFmpeg is on PATH. Fix the local export problem and rerun `stitch`; it spends no video credits.
