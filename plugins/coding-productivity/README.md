# coding-productivity

Measure the impact of AI coding tools (Copilot, Cursor, Claude Code) on your team's productivity. Extract commits from GitHub or GitLab, optionally score them with AI, and analyze before/after trends — all from within Claude Code.

## Components

| Type | Count |
|------|-------|
| Skills | 12 |
| Python modules | 12 |
| MCP Servers | 0 |

## Install

In Claude Code:
```
/plugin marketplace add Facens/coding-productivity
/plugin install coding-productivity
```

## Quick Start

```
/coding-productivity:setup                      # Configure platform, repos, storage
/coding-productivity:run                        # Last month (default)
/coding-productivity:run March 2026             # Specific month
/coding-productivity:run Q1 2026                # Quarter
/coding-productivity:run 2025-06 to 2025-12     # Custom range
/coding-productivity:run last 3 months          # Relative
```

`run` extracts → scores (if enabled) → detects duplicate work → analyzes in one step. Default is last calendar month.

Or run each step individually:
```
/coding-productivity:extract    # Pull new commits (incremental by default)
/coding-productivity:score      # AI-score commits with SCORING_MODEL (default: Claude Haiku 5.5)
/coding-productivity:dedupe     # Find work counted more than once (merges, squashes, copies, moved code)
/coding-productivity:analyze    # View productivity trends and metrics
/coding-productivity:report     # Generate executive summary markdown
```

## Features

- **Local-first** — DuckDB by default, zero cloud setup. BigQuery available as upgrade.
- **GitHub + GitLab** — Supports both platforms, including self-hosted GitLab.
- **AI scoring (optional)** — a Claude model you choose (`SCORING_MODEL`) evaluates each commit's intellectual value (0.0-1.0). Every score records the model and prompt that produced it.
- **Duplicate-work detection** — merge, squash, promotion and copied commits are excluded and moved code is discounted, so totals and trends count each piece of work once.
- **Identity management** — Bot detection, duplicate identity merging, developer exclusions.
- **Anonymization** — HMAC-SHA256 hashing of developer identities, on by default.
- **Incremental by default** — Re-runs fetch only new commits since the last extraction. Use `--full` to force a complete re-extraction.
- **Resumable** — Extraction checkpoints, rate-limit handling with pause/resume.
- **Re-runnable setup** — Add repos, toggle features, change config at any time.

## Skills

| Skill | Description |
|-------|-------------|
| `setup` | Interactive configuration wizard |
| `run` | Full pipeline: extract → score → dedupe → analyze. Accepts date range (e.g., `run January 2026`) |
| `extract` | Pull commits, diffs, PRs. Accepts date range or repo slug |
| `score` | AI-powered commit scoring with `SCORING_MODEL` |
| `dedupe` | Detect duplicate work (merge, squash, promotion, copy, moved code) so `analyze` counts it once |
| `calibrate` | Compare a candidate scoring model with the current one (COMPARABLE or REBASE REQUIRED) |
| `rebase` | Rescore all commits with one model into a new versioned series (Batch API) and switch to it |
| `analyze` | Interactive productivity analysis |
| `report` | Generate executive summary in markdown |
| `connect` | Connect to existing BigQuery dataset (read-only) |
| `developers` | Developer roster: bots, dedup, exclusions |
| `validate` | Compare results against a reference dataset |

## Configuration

All config lives in `.coding-productivity.env` (created by setup, gitignored automatically). See [references/example.env](references/example.env) for all options.

### Storage Backends

| Backend | When to use | Setup |
|---------|-------------|-------|
| DuckDB (default) | Individual developers, small teams | None — works out of the box |
| BigQuery | Shared team access, large-scale analysis | Requires GCP project + service account |

### Scoring model changes

The scoring model is a measuring instrument: **one series = one model**. A newer model can change the scale of the scores, and the shift differs by period, so a series that mixes two models shows a trend in the instrument rather than in productivity. No single conversion factor repairs that. The plugin therefore never switches models for you:

- `SCORING_MODEL` is explicit (setup writes `claude-haiku-5-5`). A newer Haiku only produces a WARNING.
- Every score row records `scorer_model` (what the API returned) and `prompt_version` (a hash of prompt, model, thinking config, effort and the scoring post-processing rules).
- `analyze` refuses date ranges that contain more than one recorded model or `prompt_version`, and refuses recorded-plus-unrecorded (legacy) scores unless you pass `--allow-legacy-null`. `score` refuses to append scores from another model, or from another `prompt_version` unless you pass `--allow-prompt-change`. To continue a series whose scores carry no recorded model, declare it once with `score --legacy-model <model>` (it must equal `SCORING_MODEL`).

To change models:

1. `/coding-productivity:calibrate --candidate <model>` rescores a stratified sample with the current and the candidate model, no writes to the score tables. It compares the candidate with the current model's own test-retest noise and says **COMPARABLE** or **REBASE REQUIRED**, with the token use and the estimated cost of a full rebase.
2. Set `SCORING_MODEL` to the candidate, then `/coding-productivity:rebase`. It rescores all commits through the Message Batches API (50% of standard prices) into new tables `commit_scores_v{N}` / `file_scores_v{N}`, retries failures synchronously, reports per-month counts, and only then switches the active series (recorded in the `scoring_meta` table). The old tables are untouched.

Batches can take up to 24 hours; `rebase` is resumable and reports `still processing` until they finish.

### Duplicate work

The same work shows up in the commits table more than once, and the duplicate share is not constant: on one real dataset (a GitLab org and a GitHub org, all branches extracted) it grew over time, so it also inflated trends. A board-level "+70%" became "+60%" once duplicates were removed. `dedupe` finds them with five rules, all validated by hand on that data, and rebuilds three tables wholesale on every run: `duplicate_commits(commit_sha, why, rules_version)`, `file_moved_share(commit_sha, file_path, qualifying_lines, moved_lines, moved_share, rules_version)` (one row per file with moved lines) and `dedupe_runs`, the completion marker that tells "never ran" from "ran, found nothing". When several rules match a commit it is recorded under the first, in this order:

1. **merge**: any commit with two or more parents whose second parent is in the commits table, whatever its title. Hosting platforms often write custom titles ("release: staging → main (#734)"), so a title filter on "Merge " misses them. Its changes are already counted through the commits it merged.
2. **squash**: the squash or merge commit of a merged MR/PR that has exactly one parent, when the MR's own branch commits (from `mr_commits`, without the squash commit itself) are in the table and their lines cover at least 80% of the squash commit's lines. Below 80% the branch is only partly extracted, so the squash commit is kept. GitLab: `squash_commit_sha`; GitHub: `merge_commit_sha`, which is the squash commit for squash merges.
3. **promotion**: the merge or squash commit of a merged MR/PR whose source branch is long-lived (`main`, `master`, `develop`, `dev`, `development`, `staging`, `production`, `stage`, `prod`, `release/*`, `support/*`, case-insensitive), such as staging into main. Its content was already counted on the source branch. A `merge_commit_sha` that is one of the MR's own commits (a fast-forward head) is genuine work stored once and is never flagged by rules 2 or 3.
4. **copy**: same project, same author email (lowercased), same title and identical changed lines in every file as an earlier commit: a cherry-pick, rebase or re-push. The earliest `(committed_date, sha)` is kept. Identity is the content of the `+`/`-` lines per file (context lines and hunk headers ignored, so shifted line numbers still match); a file with no diff text falls back to its `additions:deletions`. A looser signature (title plus per-file line counts) produced 477 false positives on the same data, for instance a weekly "Added changelog" commit that adds one line to `CHANGELOG.md`.
5. **moved code** (a discount on files, not an exclusion): added lines that already appeared earlier in a different file, whether in another repo or at another path in the same repo. That covers code imported from a proof-of-concept repo, renames git did not detect, and release syncs between sibling repos. Added lines of at least 25 characters (trimmed) are fingerprinted; a line found in 20 or more distinct files is boilerplate and ignored. For each file in a commit, `moved_share` is the share of its distinct fingerprints whose first occurrence is in a different commit and a different file. `analyze` multiplies a file's productivity by `1 - moved_share` when `moved_share` is at least 0.5, and leaves it alone below that. On the same data a commit importing a slimmed service from a proof-of-concept repo dropped from 10,524 to 3,320, while genuine rebuilds kept about 100%.

`analyze` excludes `duplicate_commits` from every commit-based metric, recomputes commit productivity from the file scores times the moved-code factor (file rows deduplicated per `(commit_sha, file_path)` first; a commit with no file rows keeps its stored total), and prints a line of the form `excluded: N commits (merge a / squash b / promotion c / copy d), moved-code discount X% of productivity` for the selected range. If `dedupe` has never run it warns and analyzes without exclusions; `--require-dedupe` turns that into an error. It also warns when the commits table changed since the last run.

Rules 2 and 3 need the merge or squash commit of each merged MR/PR, which `extract` stores from v0.14 (`merge_requests.merge_commit_sha`, `squash_commit_sha`). Data extracted earlier has NULL there, so those two rules find nothing for it until the period is extracted again (`extract --since ... --until ...`, plus `--full` on GitHub); `dedupe` says how many merged MRs/PRs lack the sha. Rules 1, 4 and 5 need no re-extraction.

The constants (80% coverage, 25 characters, 20 files, 0.5 share, branch pattern) and `DEDUPE_RULES_VERSION` live in `scripts/lib/duplicate_work.py`. The version is recorded in both result tables; change any constant and bump it.

### Anonymization

Developer identities are HMAC-SHA256 hashed by default. The pseudonymization key is stored in your config file — **back it up**. Losing the key means existing hashes become permanently unresolvable.

A local reverse mapping file (`.coding-productivity/developer_mapping.json`) allows the plugin to display human-readable names in the developer roster while keeping stored data anonymized. This file is PII and is never committed to version control.

## Requirements

- Claude Code (CLI or IDE extension)
- Python 3.10+
- API token for your Git platform (or use existing `gh`/`glab` CLI authentication)
- Anthropic API key (only if AI scoring is enabled)

## Security Notes

- Config file is created with restricted permissions (`chmod 600`)
- Do not place the project in a cloud-synced directory if anonymization matters
- The pseudonymization key and reverse mapping file together can deanonymize the dataset — store backups separately
- Use fine-grained tokens with minimum required scopes (GitHub: Contents + PRs read; GitLab: `read_api`)
- Commit diffs are sent to the Anthropic API when scoring is enabled — review their data usage policy for proprietary code

## Known Issues

- **DuckDB version lock-in**: DuckDB's storage format changes between minor versions. The plugin pins `duckdb~=1.2.0`. If you upgrade DuckDB and the database fails to open, delete `.coding-productivity/data.duckdb` and re-extract.
- **Large repos (100K+ commits)**: In-memory SHA deduplication may use significant memory. For very large repos, consider extracting in date-range batches.
- **Self-hosted GitLab with internal CAs**: Set `GITLAB_CA_BUNDLE` in config to your CA bundle path. The plugin never disables TLS verification.
- **Cloud-synced directories**: If your project directory is synced (Dropbox, OneDrive, iCloud), the config file and mapping file will be synced too. Move to a non-synced directory if anonymization matters.

## License

[CC BY-NC 4.0](LICENSE) — Free for non-commercial use. Commercial use is prohibited.
