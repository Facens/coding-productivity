---
name: extract
description: 'Extract commits, diffs, and merge/pull requests from configured GitHub or GitLab repositories. Use after setup to populate the local database, or re-run to fetch new data.'
---

# Extract Repository Data

This skill runs the extraction pipeline to pull commits, diffs, and merge/pull requests from the configured repositories into storage.

**Important:** Use `AskUserQuestion` for every interactive prompt. Never assume answers or skip steps.

## Locate the Project Root

The project root is the directory containing `.coding-productivity.env`. All paths below are relative to this root.

---

## Step 1: Verify Configuration Exists

Use the Read tool to attempt reading `.coding-productivity.env` in the project root.

If the file does not exist or is empty:
> No configuration found. Run `/coding-productivity:setup` first to configure the plugin.

Stop here. Do not proceed.

---

## Step 2: Check Storage Mode

Read the `STORAGE_MODE` value from `.coding-productivity.env`.

If `STORAGE_MODE=readonly`:
> **Error:** Cannot extract into a read-only dataset. Run `/coding-productivity:setup` to configure a writable backend.

Stop here. Do not proceed.

---

## Step 3: Ensure Python Environment

Check if `scripts/.venv` exists. If it does not, run via Bash:
```
python3.14 scripts/setup_env.py
```

Report the output. If it fails, show the error and suggest troubleshooting steps.

---

## Step 4: Determine Platform

Read the `PLATFORM` value from `.coding-productivity.env`. It will be either `github` or `gitlab`.

---

## Step 5: Run Extraction

Based on the platform, run the appropriate extraction script via Bash.

**GitHub:**
```
scripts/.venv/bin/python3.14 scripts/extract_github.py --config .coding-productivity.env
```

**GitLab:**
```
scripts/.venv/bin/python3.14 scripts/extract_gitlab.py --config .coding-productivity.env
```

The scripts print progress to stdout. Display this output to the user as it runs. If the script exits with a non-zero code, display the error and stop.

---

## Step 6: Bot Detection

After extraction completes, run bot detection on all developers found in storage. Run via Bash:

```
scripts/.venv/bin/python3.14 -c "
from scripts.lib.config import Config
from scripts.lib.storage import get_storage
from scripts.lib.bots import detect_bots, load_overrides

cfg = Config()
load_overrides(cfg.BOT_OVERRIDES)
storage = get_storage(cfg)

# Get all unique developers
rows = storage.query('SELECT DISTINCT author_name, author_email FROM commits')
developers = [{'name': r['author_name'], 'email': r['author_email']} for r in rows]

# Detect bots
bots = detect_bots(developers)

# Check which are already excluded
excluded = set(cfg.EXCLUDED_DEVELOPERS)
new_bots = [b for b in bots if b['email'] not in excluded and b['name'] not in excluded]

if new_bots:
    print('NEW_BOTS_FOUND')
    for b in new_bots:
        print(f\"  {b['name']} <{b['email']}>\")
else:
    print('NO_NEW_BOTS')

storage.close()
"
```

### If New Bots Are Found

Present the detected bots and ask:
> The following accounts were detected as bots:
>
> 1. dependabot[bot] <49699333+dependabot[bot]@users.noreply.github.com>
> 2. github-actions[bot] <action@github.com>
> ...
>
> Would you like to exclude them from analysis?
>
> 1. Yes, exclude all listed bots
> 2. Let me select which ones to exclude
> 3. No, do not exclude any

If "Yes" or selective exclusion: update `EXCLUDED_DEVELOPERS` in `.coding-productivity.env` by appending the selected bot names/emails (comma-separated) to the existing value. Preserve any entries already present.

After updating, set permissions via Bash:
```
chmod 600 .coding-productivity.env
```

### If No New Bots Found

Display:
> No new bot accounts detected.

---

## Step 7: Show Extraction Summary

Run via Bash to gather summary statistics:

```
scripts/.venv/bin/python3.14 -c "
from scripts.lib.config import Config
from scripts.lib.storage import get_storage

cfg = Config()
storage = get_storage(cfg)

commit_count = storage.count('commits')
rows = storage.query('SELECT MIN(committed_date) AS earliest, MAX(committed_date) AS latest FROM commits')
r = rows[0] if rows else {}

print(f'Repos extracted: {len(cfg.REPOS)}')
print(f'Total commits:   {commit_count}')
print(f'Date range:      {r.get(\"earliest\", \"N/A\")} to {r.get(\"latest\", \"N/A\")}')

storage.close()
"
```

Display:
```
=== Extraction Complete ===

Repos extracted: 5
Total commits:   8,432
Date range:      2023-06-01 to 2025-03-25

===============================
```

---

## Step 8: Offer Next Steps

Check the `SCORING_ENABLED` value from `.coding-productivity.env`.

If `SCORING_ENABLED=true`:
> What would you like to do next?
>
> 1. Run /coding-productivity:score to add AI-powered analysis
> 2. Run /coding-productivity:analyze to view productivity metrics
> 3. Nothing for now

If `SCORING_ENABLED` is false or not set:
> What would you like to do next?
>
> 1. Run /coding-productivity:analyze to view productivity metrics
> 2. Nothing for now

If the user chooses an option, tell them to run the corresponding command.
