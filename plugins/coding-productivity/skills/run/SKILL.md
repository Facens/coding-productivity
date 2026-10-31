---
name: coding-productivity:run
description: 'Run the full pipeline: extract, score (if enabled), analyze, and report. Defaults to last month. Use with a date range like "January 2026" or "2025-06 to 2025-12".'
argument-hint: '[date range, e.g. "January 2026", "last 3 months", "2025-06 to 2025-12"]'
---

# Run Full Pipeline

Run extract -> score (if enabled) -> dedupe -> analyze -> report in sequence for a given time period.

## Step 1: Parse Date Range

The user may provide a date range as an argument. Parse it into `--since` and `--until` dates (YYYY-MM-DD format).

Examples:
- `January 2026` -> `--since 2026-01-01 --until 2026-02-01`
- `Feb 2026` -> `--since 2026-02-01 --until 2026-03-01`
- `2025-06 to 2025-12` -> `--since 2025-06-01 --until 2025-12-31`
- `last 3 months` -> compute from today's date
- `Q1 2026` -> `--since 2026-01-01 --until 2026-04-01`
- No argument -> default to last calendar month

If the date range is ambiguous, ask the user to clarify.

## Step 2: Verify Setup

Read `.coding-productivity.env`. If missing, tell the user:
> Run `/coding-productivity:setup` first to configure the plugin.

Then stop.

Check `SCORING_ENABLED` to decide whether to include the scoring step.

## Step 3: Extract

Find the plugin scripts directory:
```bash
find ~/.claude/plugins -path '*/coding-productivity/scripts/extract_github.py' 2>/dev/null | head -1 | xargs dirname
```

Determine platform from config (`PLATFORM` key).

Extraction is **incremental by default** -- the scripts start each repo from its own latest stored commit (a repo whose last run was interrupted resumes that run's window) and only fetch newer data. When the user provides a date range, pass `--since`/`--until` to scope the extraction.

For GitHub:
```bash
{scripts_dir}/../.coding-productivity/.venv/bin/python {scripts_dir}/extract_github.py --config .coding-productivity.env [--since {since}] [--until {until}]
```

For GitLab:
```bash
{scripts_dir}/../.coding-productivity/.venv/bin/python {scripts_dir}/extract_gitlab.py --config .coding-productivity.env [--since {since}] [--until {until}]
```

If the venv doesn't exist, run `python3 {scripts_dir}/setup_env.py` first.

Show extraction progress to the user.

With an explicit `REPOS`, the extract scripts print `Warning:` lines about active repos in the same org or group that are not in `REPOS`, and about configured repos that are archived. Relay them. They only warn: what was extracted does not change. Offer `/coding-productivity:setup` to update `REPOS`.

## Step 4: Score (if enabled)

If `SCORING_ENABLED=true` in config:

```bash
{venv_python} {scripts_dir}/score_commits.py --config .coding-productivity.env --workers 10
```

Show scoring progress. This may take several minutes for large datasets.

The model is `SCORING_MODEL` from the config; the script refuses to append scores from a different model than the active series. If it exits with that error, do not edit the config to get around it: point the user to `/coding-productivity:calibrate` and `/coding-productivity:rebase`.

If scoring is disabled, skip this step and tell the user:
> Scoring is disabled. Run `/coding-productivity:setup` to enable it, or use `/coding-productivity:analyze` for raw metrics.

## Step 5: Detect Duplicate Work

Run after scoring, so analysis excludes work the commits table counts more than once (merge, squash, promotion and copied commits, moved code):

```bash
{venv_python} {scripts_dir}/dedupe.py --config .coding-productivity.env
```

It compares every commit with all the others, so it always covers the whole dataset, not just the requested range. Show its output (excluded commits per rule, files with moved code). If it notes that merged MRs/PRs carry no merge/squash sha, pass that on: the squash and promotion rules need merge requests extracted with v0.14 or later (see `/coding-productivity:dedupe`).

## Step 6: Analyze

Run all analysis queries:

```bash
{venv_python} {scripts_dir}/analyze.py --config .coding-productivity.env --query monthly_trends --since {since} --until {until}
{venv_python} {scripts_dir}/analyze.py --config .coding-productivity.env --query author_productivity --since {since} --until {until}
{venv_python} {scripts_dir}/analyze.py --config .coding-productivity.env --query category_distribution --since {since} --until {until}
{venv_python} {scripts_dir}/analyze.py --config .coding-productivity.env --query merge_velocity --since {since} --until {until}
```

Display all results to the user as markdown tables, with the `excluded: ...` line the script prints for the range.

Keep the `identity:` lines too. They list the identities `analyze` joined into one person (stored names and emails, with commit counts) and the non-person accounts (such as `root`) it left out, so a reader can review them. With anonymization on the lines show pseudonyms, not names: to check a merge, use `/coding-productivity:developers`, which resolves pseudonyms locally. To undo one, add its two emails to `IDENTITY_NO_MERGE` in `.coding-productivity.env` (`email1:email2`), or set `IDENTITY_AUTO_MERGE=false`. Developer counts (`unique_authors`) count people, not emails.

If an analysis exits with an error that the date range mixes scoring models, stop and explain it (a trend across two models is a trend in the instrument). Offer a narrower range, or `/coding-productivity:calibrate` then `/coding-productivity:rebase`.

## Step 7: Summary

After all steps complete, display:

```
Pipeline complete for {date_range}.

Extraction:  {commit_count} commits from {repo_count} repos
Scoring:     {scored_count} commits scored (or "Skipped -- not enabled")
Duplicates:  {excluded_count} commits excluded, {moved_files} files with moved code
Identities:  {merged_count} merged
Analysis:    4 reports generated

Next steps:
- /coding-productivity:report {date_range} -- Generate executive summary document
- /coding-productivity:developers -- Review and clean up developer identities
```
