# Changelog

## 0.15.0

Identity resolution at analysis time, a threshold for "active" developers, a repo coverage check and a README "Limits" section. On a real dataset (pseudonymized) 47 developers were counted where the true number was 44: two people committed from two emails each, and a non-person account called "Root" was counted. Until now only the interactive `developers` skill merged identities, so `run` and `analyze` never fixed this.

### Added

- `scripts/lib/identity.py`: `analyze` resolves stored `(author_name, author_email)` pairs into people. Two emails used with one author name are one person, and one email used with two names is one person (union-find over stored values, so it works on pseudonymized data). Generic names (`GENERIC_NAMES` in `lib/dedup.py`) never link anything; with anonymization on, the hashed form of each generic name is compared too. `IDENTITY_MERGES` is applied first.
- Config `IDENTITY_AUTO_MERGE` (default `true`; `false` leaves only `IDENTITY_MERGES`), `IDENTITY_NO_MERGE` (`email1:email2` pairs never joined; real emails or the stored values) and `MIN_ACTIVE_COMMITS` (default 1). `validate` rejects `MIN_ACTIVE_COMMITS` below 1 and malformed `IDENTITY_NO_MERGE`.
- `analyze` prints `identity:` lines: the automatic merges it applied (stored names and emails, commit counts) and the non-person accounts it excluded, with the `IDENTITY_NO_MERGE` line that undoes a merge. They go to stderr for json and csv output.
- `MIN_ACTIVE_COMMITS` above 1 adds `active_developers` and `productivity_per_active_developer` (`lines_per_active_developer` when nothing is scored) to monthly trends: people with at least that many commits in the month, and their productivity per person. The columns appear only above 1, so default output is unchanged.
- `lib/coverage.py`: with an explicit `REPOS`, `extract_github.py` and `extract_gitlab.py` list the org (GitHub owners in `REPOS`) or group (the group that directly holds each configured project, subgroups included) once and warn about active repos not in `REPOS` and configured repos that are archived. Warning only; extraction is unchanged and `--repo` / `--project` skip it. `GitHubClient.list_repos(org, include_archived=False)` and the GitLab project listing and resolution now carry an `archived` flag.
- README "Limits" section (deleted lines score zero, low weight of config, docs and translations with the real caps, productivity = score x lines, anyone with a commit counts, identity matching, merge flows comparable since 0.14.0), and sections on developer identities and repo coverage.

### Changed

- Non-person accounts (every stored name generic, such as `root`) are removed from every `analyze` query through the excluded-developers filter, commits and productivity included, as bots are. Bots were already dropped at extraction; this covers what is already stored. A person who has used a generic name on an email that also carries a real name is not excluded.
- `unique_authors` (monthly trends, period comparison) counts people, not emails. The author table has one row per person, under its most-used stored name and email, and is sorted and limited by person. Identities are resolved over the whole dataset (REPOS and `EXCLUDED_DEVELOPERS` applied, dates not), so one person keeps one identity across periods.
- With anonymization on, the merge report shows pseudonyms, not names, and says so; `/coding-productivity:developers` resolves them locally. `run` now relays the repo coverage warnings and the `identity:` lines, and its summary shows the number of merged identities.
- `period_comparison` keys the per-period people counts by the period label, not by row position, so the result no longer depends on the order the database returns the two rows. The SQL `unique_authors` columns are `NULL` placeholders filled per person afterwards.
- The `IDENTITY_NO_MERGE` example in the report pairs two emails that differ by more than case.
- The excluded-developers clause is NULL-safe: commits with no author email are no longer dropped whenever `EXCLUDED_DEVELOPERS` is set.

### Upgrade notes

- Developer counts and the author table can go down after upgrading. That is the fix, not a regression; re-read published figures. Set `IDENTITY_AUTO_MERGE=false` to get only explicit merges and the non-person exclusion.
- Two different people who share an author name are merged until you list their emails in `IDENTITY_NO_MERGE`. The same goes for a shared non-generic email (team mailbox, CI address).
- `validate` references produced before 0.15.0 counted emails in `unique_authors`; it now counts people. Regenerate the reference, or set `IDENTITY_AUTO_MERGE=false` to compare.
- New datasets still store accounts such as `root`; they are excluded at analysis time.

## 0.14.0

Duplicate-work detection. On real data (a GitLab org and a GitHub org, all branches) the same work was counted several times and the duplicate share grew over time, inflating trends: a board-level "+70%" became "+60%" once duplicates were removed. Five rules, validated by hand on that data, now find them.

### Added

- `dedupe` skill and `scripts/dedupe.py`, added to the `run` pipeline after scoring. Rules: **merge** (two or more parents, second parent in the table, whatever the title), **squash** (single-parent squash/merge commit of a merged MR/PR whose branch commits cover at least 80% of its lines), **promotion** (merge/squash commit of an MR/PR from a long-lived source branch such as staging or develop), **copy** (same project, author, title and per-file changed-line content as an earlier commit; a title plus line-count signature gave 477 false positives, so content is used) and **moved code** (added lines of at least 25 characters that first appeared in a different file; lines found in 20 or more files are boilerplate). The first four exclude commits, the fifth discounts files.
- Tables `duplicate_commits(commit_sha, why, rules_version)`, `file_moved_share(commit_sha, file_path, qualifying_lines, moved_lines, moved_share, rules_version)` (rows for files with moved lines only) and `dedupe_runs` (completion marker of the last run). All three are rebuilt wholesale on every run; `rules_version` is `DEDUPE_RULES_VERSION` in `scripts/lib/duplicate_work.py`, where all thresholds live.
- `merge_requests.merge_commit_sha` and `squash_commit_sha` (nullable), filled by `extract_github.py` (`merge_commit_sha`) and `extract_gitlab.py` (`merge_commit_sha`, `squash_commit_sha`) for merged MRs only. `create_tables` adds them to existing DuckDB and BigQuery tables.
- `Storage.iter_query` (rows streamed page by page) and `Storage.replace_table` (replace a table's contents: one transaction on DuckDB, a `WRITE_TRUNCATE` load job on BigQuery).
- `analyze.py --require-dedupe`.

### Changed

- `analyze.py` excludes `duplicate_commits` from every commit-based query and, in every productivity metric, recomputes commit productivity from the active series' file scores times `1 - moved_share` (for files with `moved_share >= 0.5`), after deduplicating file rows per `(commit_sha, file_path)`. A commit with no file rows keeps its stored total. Unscored lines-changed metrics get the same exclusion and discount; `code_efficiency` gets the exclusion only. `merge_velocity` is unchanged.
- `analyze.py` prints `excluded: N commits (merge a / squash b / promotion c / copy d), moved-code discount X% of productivity` for the selected range (stdout for tables, stderr for json and csv), warns when `dedupe` has never run or is out of date, and proceeds without exclusions.
- Merge requests are matched to their commits by `(project, iid)` where project is the GitLab project id or the GitHub `owner/name` from `web_url`, never the short name alone (iids restart in every project). A `merge_commit_sha` that is one of the MR's own commits (a fast-forward head) is not treated as a duplicate.
- Implemented in Python on top of `Storage`, not per-backend SQL, so DuckDB and BigQuery behave the same and the rules are testable offline. `diffs` is streamed twice (fingerprints are 64-bit ints) and never loaded whole.

### Fixed

- GitHub extractor: the PR dedup query and the `mr_id` lookup filtered `merge_requests` on `project_path`, a column that does not exist, and the error was swallowed. PRs were never deduplicated and `mr_commits.mr_id` was always empty. Both now use the real columns (`mr_id` comes from the PR row itself), and query errors are no longer swallowed.
- Re-extraction no longer double-inserts merge requests on either platform (GitLab never deduplicated at all). An MR is stored again only when it is new, changed (`updated_at` or `state`) or has gained a merge/squash sha its stored row lacks; GitLab MR-commit links already stored are not added again. Rows are still never updated: a changed MR is a newer version. Every reader takes the version with the latest `extracted_at` per `mr_id` (`lib/merge_requests.py`): `merge_velocity` and `code_efficiency` in `analyze.py` (the latter joined `merge_requests` to commits, so duplicate MR rows multiplied its additions), and `dedupe`, so stores that were already duplicated read correctly.
- GitLab extraction with `--full` and no `--since`/`--until` compared a string with None (`since` and `until` are now optional bounds, and so is `until` on an incremental run, which failed the same way).
- Promotion rule: a squash commit from a branch named like a long-lived one (a fork PR from `main`, a GitLab fork MR from `master`) was excluded even when none of its own commits were stored, so the only copy of that work was dropped. Promotion now needs at least one of the MR's own commits in the table.
- Squash rule: merges of the base branch into the MR no longer count towards the 80% coverage, so a merge-from-base can't stand in for missing branch commits.
- GitLab extractor writes an MR's commit links before the MR row, at both flush sites. A failed link insert used to leave the MR stored without links, and the next run skipped it as unchanged.
- GitHub extractor fetches a PR's commits whenever it stores the PR (new, or changed since it was first extracted open, so its commit list may have grown) and writes the missing links before the PR row. A failed fetch or link insert leaves the PR unstored, so the next run retries it; links are matched case-insensitively on the repo. PR and link lookups are scoped by the PR's URL, so two same-named repos of different owners no longer share PRs or links (matched exactly on owner/name: `_` in a repo name is not a wildcard).
- `analyze`'s stale-dedupe check compared a row count with the distinct-sha count `dedupe` records, so a store holding a sha twice warned on every run. Both sides now count distinct shas.
- `extract` offers `/coding-productivity:dedupe` in its next-step menu, and `dedupe` asks via `AskUserQuestion` before re-extracting.

### Upgrade notes

- Run `/coding-productivity:dedupe` (or `scripts/dedupe.py`) once, then after every extraction. Until then `analyze` warns and numbers are unchanged.
- Merge requests extracted before 0.14 have NULL shas, so the squash and promotion rules find nothing for them until the period is extracted again (`extract --since ... --until ...`, plus `--full` on GitHub). Re-extraction now stores such an MR once more, with its sha, and nothing else; the merge, copy and moved-code rules work on existing data.
- Trends computed before and after the first dedupe run are not comparable; recompute any figure you published.

## 0.13.0

The scoring model is now a measuring instrument: one series = one model. Model upgrades used to change the score scale silently (the scorer auto-discovered "the latest Haiku" at run time; on 150 real commits Haiku 5.5 scored 0.74x Haiku 4.5, and the ratio differed by period), so trends that crossed a model change were inflated. Switching models now means a full rescore (rebase), never splicing.

### Added

- `calibrate` skill and `scripts/calibrate.py`: rescore a deterministic, month-stratified sample with the current and a candidate model (no writes to score tables), report mean weighted_score and total_productivity ratios, Pearson, Spearman and median absolute difference overall and per quarter, and give a verdict (COMPARABLE or REBASE REQUIRED) against the test-retest noise band. Logs tokens and estimates the cost of a full rebase.
- `rebase` skill and `scripts/rebase.py`: rescore all commits through the Message Batches API into versioned tables `commit_scores_v{N}` / `file_scores_v{N}` (chunked by bytes, resumable, `custom_id` index map in a state file), retry failures through the synchronous scorer, report per-month counts, and only then switch the active series. Old tables are never modified.
- Provenance columns `scorer_model` (the `response.model` the API returned) and `prompt_version` (12 hex of sha256 over system prompt, user-prompt template, model, thinking config, effort and the post-processing logic: score caps, auto-score patterns, diff and message cut lengths, valid categories, `SCORING_LOGIC_VERSION`; `max_tokens` is not part of it because raising it only rescues truncated answers) on `commit_scores` and `file_scores`.
- `scoring_meta` table: append-only log of scoring series; the active series is the latest `active` row (the unversioned tables are the legacy series 1).
- Config: `SCORING_MODEL` (required when `SCORING_ENABLED=true`), `SCORING_EFFORT` (default `medium`), `SCORING_MAX_TOKENS` (default 16000).
- `analyze.py` refuses a date range that contains more than one distinct `scorer_model`, or NULL plus non-NULL unless `--allow-legacy-null` is passed.

### Changed

- The scorer no longer auto-discovers a model. It uses `SCORING_MODEL` and prints a WARNING (never a switch) when a newer Haiku exists, pointing at `calibrate`.
- Requests to non-`claude-haiku-4*` models send `thinking={"type": "adaptive"}` and `output_config={"effort": ...}` explicitly; Haiku 4.x gets neither. The answer is read from the first `text` block (5.x models return a thinking block first, which crashed `content[0].text`).
- `stop_reason` of `refusal` or `max_tokens` counts as a failed attempt and is retried; failures are logged per month at the end of a run. So does an answer from a model other than the configured one (`model_mismatch`).
- Model output is validated: `files` must be a list of objects with a string `path` and a finite numeric `score` (clamped to 0-1); `flags` is flattened to strings. A violation is `invalid_json` (retried). One malformed batch result no longer stops the rest of a batch.
- Only models known to accept adaptive thinking plus `effort` (Haiku 5.x, Sonnet 4.6/5.x, Opus 4.6+/5.x, Fable, Mythos) or known to take neither (Haiku 4.x) are accepted; any other `SCORING_MODEL` is a startup error instead of 400s mid-rebase.
- A missing `references/commit_scoring_prompt.md` is an error; there is no built-in fallback prompt.
- `score_commits.py` refuses to append scores from a model other than the active series' model, and from a different `prompt_version` unless `--allow-prompt-change` is passed. Continuing a series whose scores carry no recorded model needs `--legacy-model MODEL` once (it must equal `SCORING_MODEL`); that is recorded as a v1 `scoring_meta` row so the strict check applies afterwards.
- `analyze.py` also refuses ranges with more than one `prompt_version`, and treats a missing `prompt_version` as legacy.
- Declaring a legacy series records the run's `prompt_version` in the v1 `scoring_meta` row, so the `--allow-prompt-change` gate applies to it from the next run.
- A file path listed twice in the model's answer keeps only its first valid entry (counted with the other dropped entries), so a commit's `total_lines` is the sum of its distinct changed files and file rows are unique per `(commit_sha, file_path)`. Real data: about 0.8% of file rows on Haiku 4.5 and 1.4% on Haiku 5.5 were repeats, which counted those files' lines twice and biased the series by model. This changes scoring logic: `SCORING_LOGIC_VERSION` is now 2, so `prompt_version` changes and existing series need `--allow-prompt-change` or a rebase.
- A malformed file entry in the model's answer (null or missing score, non-object, bad path) is dropped and counted; the rest of the commit is kept. The commit is rejected (retried) only if `files` itself is malformed or no valid entry remains.
- `commit_scores.diff_chars` (nullable, added by the same migration) records the per-file diff cap actually sent: retries send 2,000 or 1,000 characters under the same `prompt_version`, so the level is recorded rather than hashed.
- `rebase`: an API refusal for billing or credit reasons aborts at once without bisecting or marking commits failed (re-run to resume); HTTP 413 is treated like 400 for bisection; the 10-in-a-row abort message mentions billing and quota.
- `analyze.py`, `score`, `report` and `analyze` skills read the active series' tables.
- `create_tables` migrates existing DuckDB and BigQuery tables by adding missing columns (old rows keep NULL). BigQuery queries now resolve unqualified table names against the configured dataset, translate the plugin's `$name` placeholders to `@name` query parameters (typed; lists become array parameters), and streaming inserts retry with backoff (2 to 120 s, about 4 minutes in total) while a just-created table or column is not yet visible. Date-range parameters are still sent as STRING, and the analysis SQL still uses DuckDB functions, so BigQuery remains unverified end to end.
- `scoring_meta` lookups treat only "table does not exist" as "legacy series"; any other error (network, quota, permissions) propagates instead of silently pointing writes at the legacy tables.
- `rebase`: results are written and the state saved every 25 commits in the synchronous fallback (a killed run keeps its work and the next run converges); file rows are written before the commit row, which is the completion marker (a crash leaves the commit unscored, not half-visible; orphan file rows from such a crash are possible and are not read by analysis); a batch the API rejects (400) is bisected, the offending requests go to the synchronous path, and ten rejections in a row abort with the API's message.
- Shared scoring logic moved to `scripts/lib/scoring.py`; the Anthropic SDK (`anthropic>=1.13`) is imported lazily.

### Upgrade notes

- Set `SCORING_MODEL` in `.coding-productivity.env`. For existing scores, use the model that produced them (they carry no recorded model); `/coding-productivity:setup` asks.
- Scores written before this version have NULL `scorer_model`. The first run after upgrading appends rows with a recorded model, so analysis of a range that spans both needs `--allow-legacy-null` (if the model is the same) or a rebase.

## 0.1.0 (Unreleased)

Initial release.

### Skills

- `setup` - Interactive configuration wizard
- `extract` - Run extraction pipeline (GitHub/GitLab)
- `score` - AI-powered commit scoring via Claude Haiku
- `analyze` - Interactive productivity analysis
- `report` - Generate executive summary markdown
- `connect` - Connect to existing BigQuery dataset (read-only)
- `developers` - Developer roster management (bots, dedup, exclusions)
- `validate` - Compare results against reference dataset
