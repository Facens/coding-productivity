# coding-productivity

Measure the impact of AI coding tools (Copilot, Cursor, Claude Code) on your team's productivity. Extract commits from GitHub or GitLab, optionally score them with AI, and analyze before/after trends — all from within Claude Code.

## Install

In Claude Code:
```
/plugin marketplace add Facens/coding-productivity
/plugin install coding-productivity
```

## Quick Start

```
/coding-productivity:setup              # Configure platform, repos, storage
/coding-productivity:run March 2026     # Extract → score → analyze in one step
```

Or run each step individually:
```
/coding-productivity:extract    # Pull commits, diffs, and PRs (incremental by default)
/coding-productivity:score      # AI-score commits via Claude Haiku
/coding-productivity:analyze    # View productivity trends and metrics
/coding-productivity:report     # Generate executive summary markdown
```

See [plugins/coding-productivity/README.md](plugins/coding-productivity/README.md) for full documentation.

## License

[CC BY-NC 4.0](plugins/coding-productivity/LICENSE) — Free for non-commercial use.
