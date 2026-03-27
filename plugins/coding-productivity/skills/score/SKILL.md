---
name: coding-productivity:score
description: 'Score commits using Claude Haiku to measure intellectual value and categorize changes. Use after extraction to add AI-powered productivity metrics. Requires an Anthropic API key.'
---

# Score Commits

This skill runs AI-powered commit scoring using Claude Haiku. It evaluates each file in every commit on a 0.0-1.0 scale measuring intellectual effort, then writes results to the `commit_scores` and `file_scores` tables.

**Important:** Use `AskUserQuestion` for every interactive prompt. Never assume answers or skip steps.

## Locate the Project Root

The project root is the directory containing `.coding-productivity.env`. All paths below are relative to this root.

## Step 1: Check Scoring is Enabled

Read `.coding-productivity.env` and check the `SCORING_ENABLED` value.

**If `SCORING_ENABLED` is not `true`:**

Tell the user:

> Commit scoring is currently disabled in your configuration. Scoring uses the Anthropic API (Claude Haiku) to evaluate each commit's intellectual value on a 0.0-1.0 scale.

Ask via `AskUserQuestion`:

> Would you like to enable AI scoring?
>
> 1. Yes, enable scoring (requires an Anthropic API key)
> 2. No, skip scoring for now

If "Yes": tell the user to run `/coding-productivity:setup` and select the AI Scoring section to configure their API key and enable scoring. Then stop.

If "No": stop and display:

> Scoring skipped. Run this skill again any time to enable it.

**If `SCORING_ENABLED=true` but `ANTHROPIC_API_KEY` is not set:** tell the user their API key is missing and suggest running `/coding-productivity:setup` to configure it. Stop.

## Step 2: Count Unscored Commits

Run via Bash to check how many commits need scoring:

```
python3.14 -c "
import sys; sys.path.insert(0, 'scripts')
from lib.config import Config
from lib.storage import get_storage
cfg = Config()
st = get_storage(cfg)
total = st.count('commits')
scored = st.count('commit_scores')
unscored = total - scored
print(f'TOTAL={total}')
print(f'SCORED={scored}')
print(f'UNSCORED={unscored}')
st.close()
"
```

Parse the output. If `UNSCORED` is 0, display:

> All {TOTAL} commits are already scored. Nothing to do.

And stop.

If `TOTAL` is 0, display:

> No commits found in storage. Run `/coding-productivity:extract` first to pull commit data.

And stop.

Otherwise, display:

> Found **{UNSCORED}** unscored commits (out of {TOTAL} total, {SCORED} already scored).

## Step 3: Data Classification Notice

Check whether the file `.coding-productivity/.scoring_consent` exists.

**If it does NOT exist**, display this notice:

> **Data Classification Notice**
>
> Commit scoring sends file diffs and commit metadata to the Anthropic API (Claude Haiku) for evaluation. The data is processed according to Anthropic's API Terms of Service and is NOT used for model training.
>
> **What is sent:** file paths, diff content (truncated to ~10 KB per file), commit titles and messages, line-change counts.
>
> **What is NOT sent:** API tokens, credentials, secrets, full file contents (only diff hunks), developer names or emails.

Ask via `AskUserQuestion`:

> Proceed with scoring?
>
> 1. Yes, start scoring
> 2. No, cancel

If "No", stop.

**If the consent file already exists**, skip this notice and proceed directly.

## Step 4: Run Scoring

Run the scoring engine via Bash:

```
cd <project_root> && python3.14 scripts/score_commits.py --config .coding-productivity.env --workers 10 --batch-size 50
```

Display the output to the user as it streams. The script prints a progress line:

```
Scoring commits... 150/1,203 (12%) | ~8 min remaining
```

Wait for completion. If the script exits with an error, display the error and suggest troubleshooting:
- **Rate limit errors**: suggest reducing `--workers` to 3-5
- **Authentication errors**: suggest checking the API key via `/coding-productivity:setup`
- **No commits to score**: this is normal if all commits were already scored

## Step 5: Show Summary

After the script completes, display its summary output which includes:
- Total commits scored
- Average weighted score
- Total productivity points
- Category breakdown (feature, bugfix, refactor, etc.)

If the summary is not printed by the script (e.g., the output was too long), query the results directly:

```
python3.14 -c "
import sys; sys.path.insert(0, 'scripts')
from lib.config import Config
from lib.storage import get_storage
cfg = Config()
st = get_storage(cfg)
scored = st.count('commit_scores')
rows = st.query('SELECT overall_category, COUNT(*) as cnt, AVG(weighted_score) as avg_score FROM commit_scores GROUP BY overall_category ORDER BY cnt DESC')
print(f'Total scored commits: {scored}')
for r in rows:
    print(f\"  {r['overall_category']:<16} {r['cnt']:>5}  avg={r['avg_score']:.2f}\")
avg = st.query('SELECT AVG(weighted_score) as avg FROM commit_scores')
print(f\"Overall average score: {avg[0]['avg']:.2f}\")
st.close()
"
```

## Step 6: Offer Next Step

Ask via `AskUserQuestion`:

> Scoring complete. What would you like to do next?
>
> 1. Run analysis to generate insights (`/coding-productivity:analyze`)
> 2. View developer breakdown (`/coding-productivity:developers`)
> 3. Done for now

If option 1: tell the user to run `/coding-productivity:analyze`.
If option 2: tell the user to run `/coding-productivity:developers`.
If option 3: display "Scoring results saved. You can run analysis any time with `/coding-productivity:analyze`." and stop.
