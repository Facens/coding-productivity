---
name: coding-productivity:analyze
description: 'Run interactive productivity analysis on extracted data. Use after extraction to view monthly trends, before/after comparisons, per-author metrics, merge velocity, and category breakdowns.'
argument-hint: '[query type: trends, comparison, authors, velocity, categories, efficiency]'
---

# Analyze Productivity Data

This skill runs interactive analysis queries against the local dataset and presents results as formatted tables.

**Important:** Use `AskUserQuestion` for every interactive prompt. Never assume answers or skip steps.

## Locate the Project Root

The project root is the directory containing `.coding-productivity.env`. All paths below are relative to this root.

## Step 1: Check Data Exists

Run via Bash:
```
python3.14 -c "
import sys; sys.path.insert(0, 'scripts')
from lib.config import Config
from lib.storage import get_storage
cfg = Config()
st = get_storage(cfg)
commits = st.count('commits')
try:
    scores = st.count('commit_scores')
except:
    scores = 0
print(f'COMMITS:{commits}')
print(f'SCORES:{scores}')
st.close()
"
```

- If `COMMITS:0`, tell the user: "No commit data found. Run `/coding-productivity:extract` first to pull data." and stop.
- Parse the counts. If `SCORES:0`, note that AI scoring data is unavailable and only raw metrics will be shown.

## Step 2: Ask What Analysis to Run

Ask the user:
> What analysis would you like to run?
>
> 1. Monthly Trends -- commit count, productivity, avg score, unique authors by month
> 2. Period Comparison -- before/after metrics with % change (e.g. pre/post AI adoption)
> 3. Author Productivity -- per-author: commits, total productivity, avg score, top category
> 4. Merge Velocity -- monthly: MRs, merge rate, avg time to merge, velocity at 7d/30d
> 5. Category Distribution -- commit categories, % of total, avg score
> 6. Code Efficiency -- monthly: total additions vs merged additions, efficiency ratio
> 7. Run all analyses

## Step 3: Collect Date Parameters

For options 1, 3, 4, 5, 6, or 7: ask the user for a date range:
> What date range should the analysis cover?
>
> Enter start and end dates (YYYY-MM-DD), e.g. `2025-01-01 to 2025-12-31`

For option 2 (Period Comparison): ask for two date ranges:
> Enter the **baseline** period (the "before" window):
> Start date (YYYY-MM-DD):
> End date (YYYY-MM-DD):
>
> Enter the **comparison** period (the "after" window):
> Start date (YYYY-MM-DD):
> End date (YYYY-MM-DD):

## Step 4: Run the Analysis

Build and run the appropriate command via Bash. Use `python3.14` to execute the script.

For standard queries (monthly_trends, author_productivity, merge_velocity, category_distribution, code_efficiency):
```
python3.14 scripts/analyze.py --config .coding-productivity.env \
    --query <query_name> --since <start> --until <end> --format table
```

For period_comparison:
```
python3.14 scripts/analyze.py --config .coding-productivity.env \
    --query period_comparison \
    --baseline-start <b_start> --baseline-end <b_end> \
    --comparison-start <c_start> --comparison-end <c_end> --format table
```

For "all":
```
python3.14 scripts/analyze.py --config .coding-productivity.env \
    --query all --since <start> --until <end> --format table
```

## Step 5: Display Results

Present the script output directly. It is already formatted as markdown tables.

If no scoring data exists and the user ran a score-dependent query (category_distribution), explain:
> Category distribution requires AI scoring data. Run `/coding-productivity:score` first to generate commit scores.

## Step 6: Offer Follow-up

Ask the user:
> Would you like to:
>
> 1. Run another analysis
> 2. Generate a full executive report (`/coding-productivity:report`)
> 3. Done

If option 1, return to Step 2. If option 2, tell the user to run `/coding-productivity:report`.
