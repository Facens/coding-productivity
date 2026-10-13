# coding-productivity

Measure the impact of AI coding tools (Copilot, Cursor, Claude Code) on your team's productivity. Extract commits from GitHub or GitLab, optionally score them with AI, and analyze before/after trends — all from within Claude Code.

## Components

| Type | Count |
|------|-------|
| Skills | 11 |
| Python modules | 11 |
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

`run` extracts → scores (if enabled) → analyzes in one step. Default is last calendar month.

Or run each step individually:
```
/coding-productivity:extract    # Pull new commits (incremental by default)
/coding-productivity:score      # AI-score commits with SCORING_MODEL (default: Claude Haiku 5.5)
/coding-productivity:analyze    # View productivity trends and metrics
/coding-productivity:report     # Generate executive summary markdown
```

## Features

- **Local-first** — DuckDB by default, zero cloud setup. BigQuery available as upgrade.
- **GitHub + GitLab** — Supports both platforms, including self-hosted GitLab.
- **AI scoring (optional)** — a Claude model you choose (`SCORING_MODEL`) evaluates each commit's intellectual value (0.0-1.0). Every score records the model and prompt that produced it.
- **Identity management** — Bot detection, duplicate identity merging, developer exclusions.
- **Anonymization** — HMAC-SHA256 hashing of developer identities, on by default.
- **Incremental by default** — Re-runs fetch only new commits since the last extraction. Use `--full` to force a complete re-extraction.
- **Resumable** — Extraction checkpoints, rate-limit handling with pause/resume.
- **Re-runnable setup** — Add repos, toggle features, change config at any time.

## Skills

| Skill | Description |
|-------|-------------|
| `setup` | Interactive configuration wizard |
| `run` | Full pipeline: extract → score → analyze. Accepts date range (e.g., `run January 2026`) |
| `extract` | Pull commits, diffs, PRs. Accepts date range or repo slug |
| `score` | AI-powered commit scoring with `SCORING_MODEL` |
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
