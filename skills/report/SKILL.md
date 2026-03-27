---
name: report
description: 'Generate a comprehensive executive summary of coding productivity analysis in markdown. Use to create a shareable report document.'
---

# Generate Executive Productivity Report

This skill runs all analysis queries and composes a comprehensive markdown report document.

**Important:** Use `AskUserQuestion` for every interactive prompt. Never assume answers or skip steps. Apply the `style-andrea` skill when composing the written sections of the report.

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
try:
    mrs = st.count('merge_requests')
except:
    mrs = 0
print(f'COMMITS:{commits}')
print(f'SCORES:{scores}')
print(f'MRS:{mrs}')
st.close()
"
```

- If `COMMITS:0`, tell the user: "No commit data found. Run `/coding-productivity:extract` first." and stop.
- Note the counts for the data coverage section of the report.

## Step 2: Ask for Date Range

Ask the user:
> What date range should the report cover?
>
> Enter start and end dates (YYYY-MM-DD), e.g. `2025-01-01 to 2025-12-31`
>
> Or type "all" to use the full dataset range.

If the user says "all", determine the range by running via Bash:
```
python3.14 -c "
import sys; sys.path.insert(0, 'scripts')
from lib.config import Config
from lib.storage import get_storage
cfg = Config()
st = get_storage(cfg)
rows = st.query('SELECT MIN(authored_date) AS min_d, MAX(authored_date) AS max_d FROM commits')
print(f'MIN:{rows[0][\"min_d\"]}')
print(f'MAX:{rows[0][\"max_d\"]}')
st.close()
"
```

## Step 3: Ask for Before/After Comparison (Optional)

Ask the user:
> Would you like to include a before/after period comparison (e.g. pre/post tool adoption)?
>
> 1. Yes
> 2. No, skip it

If yes, ask for baseline and comparison date ranges (same format as the analyze skill Step 3, option 2).

## Step 4: Run All Analyses

Run each analysis via Bash, capturing the JSON output for programmatic use:

```
python3.14 scripts/analyze.py --config .coding-productivity.env \
    --query monthly_trends --since <start> --until <end> --format json
```

```
python3.14 scripts/analyze.py --config .coding-productivity.env \
    --query author_productivity --since <start> --until <end> --format json
```

```
python3.14 scripts/analyze.py --config .coding-productivity.env \
    --query merge_velocity --since <start> --until <end> --format json
```

```
python3.14 scripts/analyze.py --config .coding-productivity.env \
    --query category_distribution --since <start> --until <end> --format json
```

```
python3.14 scripts/analyze.py --config .coding-productivity.env \
    --query code_efficiency --since <start> --until <end> --format json
```

If the user opted for a before/after comparison:
```
python3.14 scripts/analyze.py --config .coding-productivity.env \
    --query period_comparison \
    --baseline-start <b_start> --baseline-end <b_end> \
    --comparison-start <c_start> --comparison-end <c_end> --format json
```

## Step 5: Compose the Report

Using the JSON results from Step 4, compose a markdown document with the following structure. Apply Andrea's writing style for the narrative sections: clear, concise, no filler, direct.

```markdown
# Coding Productivity Report

**Period:** <start> to <end>
**Generated:** <today's date>

---

## Methodology

Brief explanation of how the dataset was built and what the metrics mean:
- Commits and merge requests were extracted from the configured repositories via the platform API.
- **Total Productivity** is a composite score derived from code additions, deletions, and change complexity.
- **Weighted Score** adjusts productivity by commit category (feature, bugfix, refactor, etc.).
- **Merge Velocity** measures how quickly merge requests move from creation to merge.
- **Code Efficiency** compares additions in all commits versus additions in merged commits only.

If scoring data is unavailable, note: "AI-powered scoring was not enabled for this dataset. Score-dependent metrics (weighted score, category distribution) are omitted."

---

## Key Findings

Analyze the data and write 3-5 key takeaways. Each should be a single sentence with a supporting data point. Examples of what to look for:
- Month-over-month trends (increasing/decreasing commits, productivity)
- Top contributors and their share of total productivity
- Merge velocity trends (improving or degrading)
- Category balance (is most work features? bugfixes? refactoring?)
- Efficiency ratio changes over time

---

## Monthly Trends

<markdown table from monthly_trends results>

---

## Before/After Comparison

_Only include this section if the user provided comparison periods._

<markdown table from period_comparison results>

Write 1-2 sentences interpreting the % change row.

---

## Top Contributors

<markdown table from author_productivity results, top 10>

---

## Merge Velocity

<markdown table from merge_velocity results>

Write 1-2 sentences summarizing average time-to-merge and the proportion merged within 7 days.

---

## Category Distribution

_Omit this section if scoring data is unavailable._

<markdown table from category_distribution results>

---

## Code Efficiency

<markdown table from code_efficiency results>

---

## Data Coverage and Limitations

- **Commits analyzed:** <count>
- **Merge requests analyzed:** <count>
- **Scoring data:** Available / Not available (<count> scored commits)
- **Repositories:** <number> configured
- Limitations: commit counts reflect all branches (cross-branch deduplication applied). Merge velocity is based on MR created_at, not first commit date. Productivity scores require AI scoring to be enabled and run separately.
```

## Step 6: Save the Report

Determine today's date and save the report to the project root:

```
coding-productivity-report-YYYY-MM-DD.md
```

Use the Write tool to create the file.

## Step 7: Display the Report

Display the full report content inline so the user can review it immediately.

Tell the user:
> Report saved to `coding-productivity-report-YYYY-MM-DD.md`.
