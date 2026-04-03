# coding-productivity Plugin Development Guide

## Overview

This plugin measures AI coding tool impact through commit-level extraction, optional AI scoring, and productivity analysis. It supports GitHub and GitLab codebases with a local-first storage model (DuckDB) and optional BigQuery backend.

## Directory Structure

```
skills/          # User-facing skills (SKILL.md files)
scripts/         # Python backend scripts
scripts/lib/     # Shared Python modules
references/      # Bundled assets (scoring prompt, example config)
```

## Conventions

### Skills

- Each skill lives in `skills/<name>/SKILL.md`
- YAML frontmatter required: `name` (lowercase-with-hyphens), `description` (what + when to use)
- Use imperative form in instructions
- Use `AskUserQuestion` (Claude Code) or equivalent for interactive prompts, with fallback to numbered options in chat
- All skills must run `scripts/setup_env.py` to ensure the Python venv exists before calling any Python script

### Python Scripts

- Python 3.10+ required (scripts discover the best available interpreter)
- All dependencies managed via `scripts/requirements.txt` and a project-local venv at `.coding-productivity/.venv/`
- Use `scripts/lib/config.py` to load `.coding-productivity.env`
- Use `scripts/lib/storage.py` for all database access (never import DuckDB or BigQuery directly)
- All SQL queries use parameterized values via `storage.query(sql, params)` — never interpolate user input
- Use `print(..., flush=True)` for real-time progress output
- API tokens must never appear in logs or output — mask as `token[:4] + "****"`

### Config

- All config lives in `.coding-productivity.env` (project root)
- Created with `chmod 600` — skills warn if permissions are too open
- `.coding-productivity/` directory holds data, checkpoints, venv, and mapping files
- Both `.coding-productivity.env` and `.coding-productivity/` are gitignored

### Security

- `developer_mapping.json` is PII — it deanonymizes the dataset. Created with `chmod 600`, gitignored.
- Anonymization settings and pseudonymization salt are locked once data exists (`storage.count("commits") > 0`)
- Self-hosted GitLab connections always verify TLS. Custom CA bundles supported via `GITLAB_CA_BUNDLE` config.

### Vendor Neutrality

- The plugin contains zero references to any specific organization, company, or team
- All org-specific configuration is in `.coding-productivity.env` (user-provided)
- Before any release, run: `grep -ri "iubenda\|team.blue\|team-blue" .` and verify zero matches
