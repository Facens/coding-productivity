---
name: coding-productivity:dedupe
description: 'Detect duplicate work in the extracted commits (merge commits, squash and promotion commits, cherry-picked copies, moved code) so analysis stops counting it twice. Run after extraction and scoring, and again whenever new commits arrive. Rebuilds the duplicate_commits and file_moved_share tables.'
argument-hint: '[--dry-run]'
---

# Detect Duplicate Work

The same work is often in the commits table more than once: a merge commit repeats the commits it merged, a squash commit repeats its branch, a staging-to-main promotion repeats everything already on staging, a cherry-pick repeats its original, and code imported from another repo or moved to another path is counted again as new. Left in, it inflates totals, and the share grows over time, so it inflates trends too.

This skill finds it with five rules (see the README, "Duplicate work") and records the result. `/coding-productivity:analyze` then excludes the duplicate commits and discounts moved code. Nothing is deleted: `commits`, `diffs` and the score tables are untouched, and the result tables are rebuilt from scratch on every run.

## Locate the Project Root

The project root is the directory containing `.coding-productivity.env`. All paths below are relative to this root. If a `.coding-productivity/.venv` is missing, run `python3 scripts/setup_env.py` first.

## Step 1: Check Data Exists

Run via Bash:
```
python3.14 -c "
import sys; sys.path.insert(0, 'scripts')
from lib.config import Config
from lib.storage import get_storage
cfg = Config()
st = get_storage(cfg)
print(f'COMMITS:{st.count(\"commits\")}')
st.close()
"
```

If `COMMITS:0`, tell the user: "No commit data found. Run `/coding-productivity:extract` first." and stop.

## Step 2: Run

```
python3.14 scripts/dedupe.py --config .coding-productivity.env [--dry-run]
```

`--dry-run` computes and reports without writing. The run reads every commit, merge request and diff in storage, so the rules can compare each commit with all the others. It is not incremental; a run over a large dataset can take several minutes. Show the progress output as it prints.

## Step 3: Read the Result

The script prints the number of excluded commits per rule and the number of files with moved code. Show it to the user.

If it prints a note that merged MRs/PRs carry no merge/squash commit sha, the data was extracted before v0.14: the squash and promotion rules cannot see those MRs yet, and the merge, copy and moved-code rules are unaffected. Tell the user, and offer to re-extract the period so the shas are stored (merge requests are fetched again with `extract --since <date> --until <date>`, plus `--full` on GitHub), then run this skill again. Ask via `AskUserQuestion`:

> 1. Re-extract now
> 2. Continue with merge, copy and moved-code detection only

## Step 4: Next Steps

Tell the user:
> Duplicate-work detection is recorded. `/coding-productivity:analyze` now excludes {N} duplicate commits and applies the moved-code discount, and prints an `excluded:` line for the selected range. Run this skill again after every extraction: `analyze` warns when the commits table changed since the last run.
