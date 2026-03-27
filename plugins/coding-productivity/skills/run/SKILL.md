---
name: coding-productivity:run
description: 'Run the full pipeline: extract, score (if enabled), analyze, and report. Defaults to last month. Use with a date range like "January 2026" or "2025-06 to 2025-12".'
argument-hint: '[date range, e.g. "January 2026", "last 3 months", "2025-06 to 2025-12"]'
---

# Run Full Pipeline

Run extract → score (if enabled) → analyze → report in sequence for a given time period.

## Step 1: Parse Date Range

The user may provide a date range as an argument. Parse it into `--since` and `--until` dates (YYYY-MM-DD format).

Examples:
- `January 2026` → `--since 2026-01-01 --until 2026-02-01`
- `Feb 2026` → `--since 2026-02-01 --until 2026-03-01`
- `2025-06 to 2025-12` → `--since 2025-06-01 --until 2025-12-31`
- `last 3 months` → compute from today's date
- `Q1 2026` → `--since 2026-01-01 --until 2026-04-01`
- No argument → default to last calendar month

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

Determine platform from config (`PLATFORM` key). Run via Bash:

For GitHub:
```bash
{scripts_dir}/../.coding-productivity/.venv/bin/python {scripts_dir}/extract_github.py --config .coding-productivity.env --since {since} --until {until}
```

For GitLab:
```bash
{scripts_dir}/../.coding-productivity/.venv/bin/python {scripts_dir}/extract_gitlab.py --config .coding-productivity.env --since {since} --until {until}
```

If the venv doesn't exist, run `python3 {scripts_dir}/setup_env.py` first.

Show extraction progress to the user.

## Step 4: Score (if enabled)

If `SCORING_ENABLED=true` in config:

```bash
{venv_python} {scripts_dir}/score_commits.py --config .coding-productivity.env --workers 10
```

Show scoring progress. This may take several minutes for large datasets.

If scoring is disabled, skip this step and tell the user:
> Scoring is disabled. Run `/coding-productivity:setup` to enable it, or use `/coding-productivity:analyze` for raw metrics.

## Step 5: Analyze

Run all analysis queries:

```bash
{venv_python} {scripts_dir}/analyze.py --config .coding-productivity.env --query monthly_trends --since {since} --until {until}
{venv_python} {scripts_dir}/analyze.py --config .coding-productivity.env --query author_productivity --since {since} --until {until}
{venv_python} {scripts_dir}/analyze.py --config .coding-productivity.env --query category_distribution --since {since} --until {until}
{venv_python} {scripts_dir}/analyze.py --config .coding-productivity.env --query merge_velocity --since {since} --until {until}
```

Display all results to the user as markdown tables.

## Step 6: Summary

After all steps complete, display:

```
Pipeline complete for {date_range}.

Extraction:  {commit_count} commits from {repo_count} repos
Scoring:     {scored_count} commits scored (or "Skipped — not enabled")
Analysis:    4 reports generated

Next steps:
- /coding-productivity:report {date_range} — Generate executive summary document
- /coding-productivity:developers — Review and clean up developer identities
```
