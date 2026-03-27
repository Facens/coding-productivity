# coding-productivity

Measure the impact of AI coding tools (Copilot, Cursor, Claude Code) on your team's productivity. Extract commits from GitHub or GitLab, optionally score them with AI, and analyze before/after trends — all from within Claude Code.

## Install

```bash
claude plugin install coding-productivity
```

## Quick Start

```
/coding-productivity:setup      # Configure platform, repos, storage
/coding-productivity:extract    # Pull commits, diffs, and PRs
/coding-productivity:analyze    # View productivity trends and metrics
```

Optional:
```
/coding-productivity:score      # AI-score commits via Claude Haiku
/coding-productivity:report     # Generate executive summary markdown
```

## Features

- **Local-first** — DuckDB by default, zero cloud setup. BigQuery available as upgrade.
- **GitHub + GitLab** — Supports both platforms, including self-hosted GitLab.
- **AI scoring (optional)** — Claude Haiku evaluates each commit's intellectual value (0.0-1.0).
- **Identity management** — Bot detection, duplicate identity merging, developer exclusions.
- **Anonymization** — HMAC-SHA256 hashing of developer identities, on by default.
- **Resumable** — Extraction checkpoints, rate-limit handling with pause/resume.
- **Re-runnable setup** — Add repos, toggle features, change config at any time.

## Skills

| Skill | Description |
|-------|-------------|
| `setup` | Interactive configuration wizard |
| `extract` | Run extraction pipeline (GitHub/GitLab) |
| `score` | AI-powered commit scoring via Claude Haiku |
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

### Anonymization

Developer identities are HMAC-SHA256 hashed by default. The pseudonymization key is stored in your config file — **back it up**. Losing the key means existing hashes become permanently unresolvable.

A local reverse mapping file (`.coding-productivity/developer_mapping.json`) allows the plugin to display human-readable names in the developer roster while keeping stored data anonymized. This file is PII and is never committed to version control.

## Requirements

- Claude Code (CLI or IDE extension)
- Python 3.10+
- API token for your Git platform (GitHub PAT or GitLab PAT)
- Anthropic API key (only if AI scoring is enabled)

## Security Notes

- Config file is created with restricted permissions (`chmod 600`)
- Do not place the project in a cloud-synced directory if anonymization matters
- The pseudonymization key and reverse mapping file together can deanonymize the dataset — store backups separately
- Use fine-grained tokens with minimum required scopes (GitHub: Contents + PRs read; GitLab: `read_api`)
- Commit diffs are sent to the Anthropic API when scoring is enabled — review their data usage policy for proprietary code

## License

[CC BY-NC 4.0](LICENSE) — Free for non-commercial use. Commercial use is prohibited.
