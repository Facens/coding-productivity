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
- **Identity management** — Bot detection, automatic merging of identities that share a name or an email, duplicate identity merging, developer exclusions.
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

### Developer identities

A developer count that depends on how many addresses someone commits from is wrong. On one real dataset two people committed from two emails each and a non-person account called "Root" was counted: 47 developers where the true number was 44. `analyze` now resolves identities itself, on every run, without touching stored data:

- Two emails used with the same author name are one person, and one email used with two names is one person (a union over the stored names and emails, case-insensitive). It works on anonymized data, because the same name always hashes to the same value.
- Generic names (`admin`, `root`, `system`, `ci`, `bot`, `noreply`, `unknown`... see `GENERIC_NAMES` in `scripts/lib/dedup.py`) never link anything, raw or hashed.
- An account whose every name is generic is not a person. It is left out of every count and total, as bots are (bots are dropped at extraction; this covers what is already stored).
- `IDENTITY_MERGES` (`alias:canonical` emails) is applied first. `IDENTITY_AUTO_MERGE=false` switches the automatic step off, `IDENTITY_NO_MERGE=email1:email2,...` lists pairs that must never be joined.
- Identities are resolved over the whole dataset (REPOS and `EXCLUDED_DEVELOPERS` applied, dates not), so one person keeps one identity across the baseline and the comparison period.

`unique_authors` in monthly trends and period comparison, and the per-author table, count and group people. The author table shows each person under their most-used stored name and email. `analyze` prints the automatic merges it applied (stored names and emails, with commit counts) and the accounts it excluded, so you can review them. With anonymization on the report shows pseudonyms, not names, so a wrong merge is not readable from it: to check one, run `/coding-productivity:developers`, which resolves pseudonyms locally. Two different people with the same name are the typical false positive. Undo one by adding the two emails to `IDENTITY_NO_MERGE`.

`MIN_ACTIVE_COMMITS` (default 1) is the number of commits a person needs in a month to count as an active developer. At the default nothing changes. Above 1, monthly trends gain two columns, `active_developers` and `productivity_per_active_developer` (the productivity of those developers divided by their number; lines changed when nothing is scored). `unique_authors` stays as it was, so both views are visible. The columns appear only above 1 so the default output and every existing number stay as they were.

### Commit date

`DATE_FIELD` chooses the date that puts a commit in a month and inside `--since` / `--until`: `committed_date` (default) or `authored_date`. A commit written in January and rebased or cherry-picked in February belongs to February by commit date, so the month is when the change reached the branch. Authored date follows the author's clock and can land far from the merge. The same field is used for monthly trends, period comparison, the author table, categories, code efficiency and the single-model check. Merge velocity buckets by the MR's `created_at`. A commit with no stored `committed_date` is bucketed by its authored date, so nothing drops out.

The default changed in 0.16.0 (it was the authored date). For repos with rebases and cherry-picks, monthly numbers move by a few percent; set `DATE_FIELD=authored_date` to reproduce older figures.

### Repo coverage

With an explicit `REPOS` list, `extract` makes one listing call per GitHub organization (the owners in `REPOS`) or GitLab group (the group that directly holds each configured project, subgroups included) and warns about active, non-archived repos that are not in `REPOS`, and about configured repos that are now archived. It only warns: what is extracted does not change. Not run with `--repo` / `--project`.

### Anonymization

Developer identities are HMAC-SHA256 hashed by default. The pseudonymization key is stored in your config file — **back it up**. Losing the key means existing hashes become permanently unresolvable.

`PSEUDONYMIZATION_KEY_FORMAT` says how the key string becomes the HMAC key: `text` (default) uses the UTF-8 bytes of the string as written, `hex` decodes it with `bytes.fromhex` (a 64-character hex key becomes 32 raw bytes). The same 64 characters give two different keys, so hashes only match data produced with the same format. Use `hex` when you load data hashed by another pipeline that decoded a hex key, otherwise leave it alone: changing it on a dataset that already holds hashes breaks every lookup, like changing the key. An invalid hex key is reported by `validate` and stops extraction and analysis with an error that does not print the key.

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

## Limits

What the numbers do and do not say:

- **Deleted lines score zero.** Productivity is the sum over files of `score x max(additions, 1)`. A file with only deletions counts as one line. Cleanup, dead-code removal and refactors that mostly delete look low-value.
- **Config, docs and translations are weighted low on purpose.** `.md` files are capped at 0.15 in code (the scoring prompt allows 0.15-0.30 and the code cap wins), JSON files over 500 lines (except `package.json`) at 0.05, locale and translation files (`locales/*.yml`, `i18n/*`, `translations/*`) are scored 0.05 without an API call, and lockfiles, minified, generated and build output score 0.0. Work that is mostly docs, config or translation counts for little.
- **Productivity is score x lines, and size is only handled by the scoring prompt.** The prompt asks for more scrutiny as a change grows (typically 0.2-0.5 for 501-1000 lines, 0.1-0.3 above 1000), but nothing in code limits how many lines a commit can contribute. A very large diff that the model scores generously inflates the total.
- **Anyone with a commit in a month counts as a developer.** A single commit makes someone one of the developers of that month. Raise `MIN_ACTIVE_COMMITS` to see productivity per developer among people with at least that many commits.
- **Identities are matched by name and email.** Two people with the same author name are merged unless you list them in `IDENTITY_NO_MERGE`; one person who commits under different names and different emails is not.
- **A shared email joins everyone who used it.** A non-generic email that several people commit from (a team mailbox, a shared machine config, a CI address with `user.name` set to the triggering person) puts all of them into one person. Review the merge list and use `IDENTITY_NO_MERGE`.
- **Merge, squash, promotion and copied commits are removed since 0.14.0.** Repos that squash into main and repos that merge PR to staging to main are therefore comparable. Before 0.14.0 one change could be counted up to four times, so a repo with more merge steps looked more productive. Data extracted before 0.14.0 needs `dedupe` (and a re-extraction of merge requests for the squash and promotion rules) to benefit.

## Known Issues

- **DuckDB version lock-in**: DuckDB's storage format changes between minor versions. The plugin pins `duckdb~=1.2.0`. If you upgrade DuckDB and the database fails to open, delete `.coding-productivity/data.duckdb` and re-extract.
- **Large repos (100K+ commits)**: In-memory SHA deduplication may use significant memory. For very large repos, consider extracting in date-range batches.
- **Self-hosted GitLab with internal CAs**: Set `GITLAB_CA_BUNDLE` in config to your CA bundle path. The plugin never disables TLS verification.
- **Cloud-synced directories**: If your project directory is synced (Dropbox, OneDrive, iCloud), the config file and mapping file will be synced too. Move to a non-synced directory if anonymization matters.

## License

[CC BY-NC 4.0](LICENSE) — Free for non-commercial use. Commercial use is prohibited.
