# coding-productivity

Measure the impact of AI coding tools (Copilot, Cursor, Claude Code) on your team's productivity. Extract commits from GitHub or GitLab, optionally score them with AI, and analyze before/after trends — all from within Claude Code.

## Components

| Type | Count |
|------|-------|
| Skills | 8 |
| Python modules | 11 |
| MCP Servers | 0 |

## Install

```bash
# Add the marketplace and install
claude marketplace add https://github.com/Facens/coding-productivity.git
claude plugin install coding-productivity
```

Or install from a local clone:
```bash
git clone https://github.com/Facens/coding-productivity.git
claude plugin install ./coding-productivity
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

## Known Issues

- **DuckDB version lock-in**: DuckDB's storage format changes between minor versions. The plugin pins `duckdb~=1.2.0`. If you upgrade DuckDB and the database fails to open, delete `.coding-productivity/data.duckdb` and re-extract.
- **Large repos (100K+ commits)**: In-memory SHA deduplication may use significant memory. For very large repos, consider extracting in date-range batches.
- **Self-hosted GitLab with internal CAs**: Set `GITLAB_CA_BUNDLE` in config to your CA bundle path. The plugin never disables TLS verification.
- **Cloud-synced directories**: If your project directory is synced (Dropbox, OneDrive, iCloud), the config file and mapping file will be synced too. Move to a non-synced directory if anonymization matters.

## License

[CC BY-NC 4.0](LICENSE) — Free for non-commercial use. Commercial use is prohibited.
